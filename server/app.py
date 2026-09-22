# -*- coding: utf-8 -*-
"""用户自建本地深度/法线服务（FastAPI + ONNX）。

启动：
  uvicorn server.app:app --host 127.0.0.1 --port 8765
  （Windows 用 server\run_server.bat，Linux/macOS 用 server/run_server.sh）

环境变量：
  HLS_MODEL_PATH         深度模型路径。默认按 models/ 目录里实际存在的文件
                         自动选择：depth-anything-v2-small.onnx（优先）→ model-small.onnx
  HLS_NORMAL_MODEL_PATH  可选：法线模型路径，默认 moge2-vits-normal.onnx。
                         未提供时法线由深度几何派生。

支持的模型家族（与桌面软件内置链同源，见 core/preprocess.py）：
  depth  —— Depth Anything V2 Small / MiDaS-small，均输出「白=近」
  normal —— MoGe-2 ViT-S 法线分支（等价于 ControlNet 的 BAE/DSINE 预处理器）

接口：
  POST /analyze  body: PNG/JPEG 图片 → JSON {"depth_base64","normal_base64",...}
  POST /depth    body: PNG/JPEG 图片 → PNG 深度图（白=近）
  POST /normal   body: PNG/JPEG 图片 → PNG 法线图（切线空间，0.5=零）
  GET  /health                       → {"status","depth_model","normal_model","ready"}

设计要点：
  - 本服务不随桌面软件分发，模型权重由用户自行部署，仓库内不含任何权重。
  - 深度模型缺失时**不伪造结果**，一律返回 503，让桌面软件如实降级到
    「仅预览模式」或云端后端；避免把模拟深度错记成「自建服务」结果。
  - 详细接口说明见 docs/API接口文档.md。
"""
from __future__ import annotations

import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image

app = FastAPI(title="Horizon Light Studio 本地深度服务", version="1.2.0")

from core import preprocess
from core.paths import subdir

_MODELS_DIR = subdir("models")


def _pick_model(env_key: str, preferred: list) -> str:
    """环境变量优先；否则取候选列表中第一个磁盘上存在的文件。

    默认值不能写死：用户可能只部署了 MiDaS，也可能只部署了 DAV2，
    写死任一个都会让另一种部署方案莫名 503。
    """
    env = os.environ.get(env_key, "").strip()
    if env:
        return env
    for name in preferred:
        p = os.path.join(_MODELS_DIR, name)
        if os.path.isfile(p):
            return p
    return os.path.join(_MODELS_DIR, preferred[-1])


MODEL_PATH = _pick_model("HLS_MODEL_PATH", [
    preprocess.DEPTH_MODEL_FILENAME, "model-small.onnx"])
NORMAL_MODEL_PATH = _pick_model("HLS_NORMAL_MODEL_PATH", [
    preprocess.NORMAL_MODEL_FILENAME])

_MAX_SIDE = 4096

_sessions: dict = {}


def _session(path: str):
    """按路径缓存 ONNX 会话；未安装 onnxruntime 或文件缺失返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    if path not in _sessions:
        import onnxruntime as ort
        _sessions[path] = ort.InferenceSession(
            path, providers=["CPUExecutionProvider"])
    return _sessions[path]


def _model_ready() -> bool:
    return _session(MODEL_PATH) is not None


def _normal_ready() -> bool:
    """法线模型是否可用于推理：文件存在、能加载、且确实带 normal 输出。

    只判「文件能加载」不够：用户可能把深度模型填进法线栏，
    那种会话能加载但没有 normal 输出，必须在健康检查里就暴露出来。
    """
    sess = _session(NORMAL_MODEL_PATH)
    return sess is not None and "normal" in {
        o.name for o in sess.get_outputs()}


async def _read_image(request: Request) -> np.ndarray:
    """读取请求体为 RGB uint8（PNG/JPEG 均可），透明区按白底合成。"""
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空：请以 image/png 或 image/jpeg 原始字节 POST。")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGBA")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片：{e}")
    a = np.asarray(img)
    alpha = a[..., 3:4].astype(np.float32) / 255.0
    rgb = (a[..., :3].astype(np.float32) * alpha +
           255.0 * (1 - alpha)).astype(np.uint8)
    h, w = rgb.shape[:2]
    if max(h, w) > _MAX_SIDE or min(h, w) < 64:
        raise HTTPException(
            status_code=413,
            detail=f"图片尺寸 {w}x{h} 超出支持范围（短边≥64，长边≤{_MAX_SIDE}）。")
    return rgb


def _estimate_depth(rgb: np.ndarray) -> np.ndarray:
    """深度推理，返回与原图同尺寸的 0~1 深度（白=近）。模型缺失时 503。"""
    sess = _session(MODEL_PATH)
    if sess is None:
        raise HTTPException(
            status_code=503,
            detail=("深度模型未加载。请执行 server/setup_deployment.sh（或 .bat）"
                    "下载模型，或用环境变量 HLS_MODEL_PATH 指定 .onnx 文件后重启服务。"))
    # 与桌面软件内置链共用同一份预处理：模型家族（DAV2 / MiDaS）在内部识别
    return preprocess.depth_map(rgb, MODEL_PATH, sess=sess)


def _derive_normal(depth: np.ndarray) -> np.ndarray:
    """由深度场几何派生切线空间法线（HxWx3，-1~1）。"""
    from core.render import depth_to_height, height_to_normal
    return height_to_normal(depth_to_height(depth))


def _estimate_normal(rgb: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """法线推理：优先法线 ONNX 模型（MoGe-2），否则由深度几何派生。

    MoGe-2 输出在 OpenCV 相机系，z 分量需取反 —— 该细节封装在
    core/preprocess.moge_normal 内，与桌面软件内置链保持一致。
    """
    if not _normal_ready():
        return _derive_normal(depth)
    sess = _session(NORMAL_MODEL_PATH)
    return preprocess.moge_normal(rgb, NORMAL_MODEL_PATH, sess=sess)


def _png_bytes(arr01: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray((np.clip(arr01, 0, 1) * 255).astype(np.uint8)).save(
        buf, format="PNG")
    return buf.getvalue()


def _normal_png_bytes(normal_xyz: np.ndarray) -> bytes:
    """-1~1 法线编码为 0~1 PNG（0.5 为零）。"""
    return _png_bytes(np.clip(normal_xyz, -1, 1) * 0.5 + 0.5)


@app.get("/health")
def health():
    """存活探测。ready=False 表示服务在但模型未就绪（客户端据此继续降级）。"""
    depth_ok = _model_ready()
    normal_ok = _normal_ready()
    return {
        "status": "ok",
        "version": app.version,
        "depth_model": MODEL_PATH if depth_ok else None,
        "normal_model": NORMAL_MODEL_PATH if normal_ok else None,
        "normal_source": "onnx" if normal_ok else "derived-from-depth",
        "ready": depth_ok,
        "message": "就绪" if depth_ok
        else "深度模型未加载：请运行 server/setup_deployment 下载，或设置 HLS_MODEL_PATH",
    }


@app.post("/analyze")
async def analyze(request: Request):
    """深度 + 法线一次返回（桌面软件默认调用本接口）。"""
    rgb = await _read_image(request)
    depth = _estimate_depth(rgb)
    normal = _estimate_normal(rgb, depth)
    return {
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "depth_encoding": "png8: white=near, black=far",
        "normal_encoding": "png8: tangent-space, 0.5=zero",
        "normal_source": "onnx" if _normal_ready() else "derived-from-depth",
        "depth_base64": base64.b64encode(_png_bytes(depth)).decode(),
        "normal_base64": base64.b64encode(_normal_png_bytes(normal)).decode(),
    }


@app.post("/depth")
async def depth(request: Request):
    rgb = await _read_image(request)
    d = _estimate_depth(rgb)
    return Response(_png_bytes(d), media_type="image/png")


@app.post("/normal")
async def normal(request: Request):
    rgb = await _read_image(request)
    n = _estimate_normal(rgb, _estimate_depth(rgb))
    return Response(_normal_png_bytes(n), media_type="image/png")
