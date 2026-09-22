# -*- coding: utf-8 -*-
"""WebUI 后端：把 core 引擎包装成纯本地 HTTP API，并托管构建好的前端静态资源。

启动：
  python -m webui.api --host 127.0.0.1 --port 8756

设计要点：
  - 只监听本机、只允许 localhost 来源的浏览器请求（无远程来源、无 CORS 通配）。
  - 图像只存在于内存注册表（最多 8 张），不写用户目录以外的文件。
  - 前端为构建期产物：若 webui/frontend/dist 缺失，返回内联提示页而非报错。
  - 渲染逻辑全部复用 core/，本模块不复制任何算法。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.ai_backend import AIBackendConfig, encode_png
from core.cache import AICache, CACHE_DIR_DEFAULT
from core.pipeline import MIN_SIZE, MAX_SIZE, Pipeline
from core.paths import subdir
from core.presets import PRESET_NAMES, get_preset
from core.reference import analyze_reference, apply_reference_offset
from core.render import depth_to_height, height_to_normal, render
from core.types import Light, RenderParams
from core.video import (HARD_MAX_DURATION, HARD_MAX_SIDE, REC_MAX_DURATION,
                        REC_MAX_SIDE, VIDEO_EXTS, VideoInfo, _Cancelled,
                        has_ffmpeg, iter_frames, process_video, probe,
                        read_frame, select_keyframes)

APP_VERSION = "1.0.0"
HIGH_RES_SIDE = 1600          # 超过该边长即视为高分辨率
MAX_IMAGES = 8                # 内存图像注册表上限
# 模型目录里「够旧」才回收的临时文件阈值（秒）。下载也可能把 mkstemp 的
# .part 放在同一目录，阈值避免启动清理打断别的进程正在进行的下载。
_TEMP_ORPHAN_MIN_AGE = 600.0
BACKENDS = ("builtin", "local", "cloud", "simulate")

log = logging.getLogger("webui")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

_DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "frontend", "dist")
_INDEX_FILE = os.path.join(_DIST_DIR, "index.html")

MAX_VIDEOS = 4                # 视频注册表上限（临时文件随淘汰删除）
_SNIFF_BYTES = 64 * 1024      # 统一入口用于识别媒体类型的前置字节数
_VIDEO_DIR = subdir("video")
_VIDEO_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}


def _cleanup_video_dir() -> None:
    """启动时清掉上次会话残留的临时文件与导出产物。"""
    for d in (_VIDEO_DIR, subdir("exports")):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError as e:
                log.warning("清理残留文件失败 %s：%s", p, e)


def _cleanup_model_dir() -> None:
    """清掉模型目录里的孤儿临时文件。

    临时文件名是**唯一**的（mkstemp），这是并发安全的必要代价；副作用是
    进程被硬杀（关窗 / 任务管理器 / taskkill，下载 64MB 传到一半时最常见）
    留下的残file不会被下一次尝试覆盖 —— 旧的固定名 `dst + ".part"` 是自愈的，
    唯一名不是。所以要靠启动清理回收，否则模型目录会堆积多个 64MB 孤儿。
    模型就绪判定只看 `dst` 本身，删这些无风险。

    **只回收「够旧」的临时文件**：下载也把 mkstemp 的 .part 放在同一个目录，
    而 `setup_deployment.sh`（文档里的主安装路径）可能在另一个进程里正下载。
    若不加时间判断，启动清理会删掉**别人正在写的** .part ——
    POSIX 上 unlink 会成功，下载器随后的 os.replace 就抛 FileNotFoundError，
    脚本报「模型下载失败」而网络其实没问题（Windows 上 remove 会直接失败，
    仅一条 warning，无害）。按 10 分钟阈值判定，既不影响回收真正的孤儿
    （它们不会被覆盖，晚一轮启动再收也无妨），又不会打断进行中的下载。
    """
    from core.builtin_depth import default_model_path
    from core.preprocess import normal_model_path
    # 深度与法线模型可能被用户配置到不同目录，两边都要清
    dirs = {os.path.dirname(_config.builtin_model_path or default_model_path()),
            os.path.dirname(_config.builtin_normal_model_path or normal_model_path())}
    now = time.time()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not (name.endswith(".part") or name.endswith(".importing")):
                continue
            p = os.path.join(d, name)
            try:
                age = now - os.stat(p).st_mtime
            except OSError:
                continue                  # 读不到 mtime（正在被替换等）→ 不碰
            if age < _TEMP_ORPHAN_MIN_AGE:
                log.info("跳过可能仍在写入的临时文件（%.0fs）：%s", age, p)
                continue
            try:
                os.remove(p)
                log.info("清理模型目录残留临时文件：%s", p)
            except OSError as e:
                log.warning("清理残留模型临时文件失败 %s：%s", p, e)


# 启动预热的可见状态，随 /api/state 返回。前端目前还没消费它（HANDOFF §7 #6），
# 先把状态暴露出来，界面想显示「引擎加载中」时不用再改后端。
_warmup: Dict[str, Any] = {"state": "idle", "seconds": 0.0,
                           "depth": False, "normal": False, "detail": ""}


# 预热推迟的秒数。ONNX Runtime 构建会话时会**长时间持有 GIL**（实测：MoGe
# 加载 1.09s 期间，另一个线程的 10ms 睡眠被拉长到 0.937s），所以预热一旦开始，
# 整个服务（连不碰模型的 /api/themes 也一样）会被冻住约 1.3s。这件事躲不掉，
# 只能选时机：推迟到首屏画完、用户还在挑图的空档里做，别冻住首屏。
WARMUP_DELAY = 3.0


def _warmup_models(delay: float = 0.0) -> None:
    """把深度/法线模型各加载一次，替首个请求付掉约 1.9s 的会话构建。

    不预热时这笔开销落在**用户第一次分析**上（约 4.8s 而不是 3.0s）。预热
    仍要走 allow_load=False 那套：/api/state 报 model_ready 时**不**触发加载，
    否则首屏和预热线程会撞在一起等同一个锁，白等一遍。

    加载本身按 (路径, 大小+mtime) 缓存，所以这次预热不会白费：之后所有请求
    都直接复用同一个会话；结论还会写进 _depth_ready_memo / _normal_ready_memo，
    首屏与后续轮询都直接命中。

    失败不抛：模型缺失/损坏时状态记为 missing，界面照旧显示「未就绪」，
    与不预热的行为一致。
    """
    if delay > 0:
        _warmup.update(state="scheduled", seconds=0.0)
        time.sleep(delay)
    t0 = time.time()
    _warmup.update(state="warming")
    depth = normal = False
    detail = ""
    try:
        depth = model_ready(_config.builtin_model_path)
        if not depth:
            detail = "深度模型未就绪"
    except Exception as e:
        detail = f"深度模型预热失败：{e}"
    try:
        normal = normal_model_ready(_config.builtin_normal_model_path)
        if not normal:
            detail = (detail + "；" if detail else "") + "法线模型未装"
    except Exception as e:
        detail = (detail + "；" if detail else "") + f"法线模型预热失败：{e}"
    _warmup.update(state="ready" if depth else "missing",
                   seconds=round(time.time() - t0, 2),
                   depth=depth, normal=normal, detail=detail)
    log.info("模型预热完成（%.2fs）：深度=%s 法线=%s", _warmup["seconds"],
             depth, normal)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _cleanup_video_dir()
    _cleanup_model_dir()
    # 后台线程预热，不阻塞启动；推迟 WARMUP_DELAY 秒是为了不冻住首屏（见上）
    threading.Thread(target=_warmup_models, args=(WARMUP_DELAY,),
                     name="hls-warmup", daemon=True).start()
    yield


app = FastAPI(title="凌日光影棚 WebUI", version=APP_VERSION,
              lifespan=_lifespan)

# ---------------------------------------------------------------- 状态与注册表

_lock = threading.RLock()
_images: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
# _state["params"]：界面当前的渲染参数，作为工作流/自动打光的基准。
# 写入点：/api/render（每次渲染都记录）与 /api/autolight 的 apply=true。
# 注意这是**进程内存**状态：重启服务即回到默认预设（开发期 --reload 亦然），
# 需要持久保存请用 /api/settings 或工作流 JSON。
_state: Dict[str, Any] = {"high_res_hint": False,
                          "params": get_preset("标准棚拍").to_dict()}
_config: AIBackendConfig = AIBackendConfig.load()


def _current_params() -> RenderParams:
    """界面当前的渲染参数（作为工作流/自动打光的基准）。"""
    try:
        return _params_from_dict(_state.get("params"))
    except ApiError:
        return get_preset("标准棚拍")


def _set_current_params(params: RenderParams) -> None:
    _state["params"] = params.to_dict()


def _masked_settings(cfg: AIBackendConfig) -> dict:
    """API Key 永不出网：只暴露是否已配置。"""
    s = dict(cfg.__dict__)
    key = s.pop("cloud_api_key", "")
    s["cloud_api_key_set"] = bool(key)
    return s


def _new_entry(rgb: np.ndarray, filename: str) -> Dict[str, Any]:
    h, w = rgb.shape[:2]
    entry = {
        "id": uuid.uuid4().hex,
        "rgb": rgb,
        "depth": None,
        "normal": None,
        "backend": "",
        "from_cache": False,
        "filename": filename or "image.png",
        "width": int(w),
        "height": int(h),
        "high_res_hint": False,
    }
    with _lock:
        _images[entry["id"]] = entry
        while len(_images) > MAX_IMAGES:
            _images.popitem(last=False)
        _refresh_high_res(entry)
    return entry


def _live_entry(image_id: str) -> Dict[str, Any]:
    with _lock:
        entry = _images.get(image_id)
        if entry is None:
            raise ApiError(404, "unknown_image_id",
                           "图片不存在或已被淘汰，请重新上传")
        _images.move_to_end(image_id)
    return entry


def _refresh_high_res(entry: Dict[str, Any]) -> None:
    entry["high_res_hint"] = bool(entry["height"] > HIGH_RES_SIDE
                                  or entry["width"] > HIGH_RES_SIDE) \
        and not _config.cloud_enabled
    _state["high_res_hint"] = entry["high_res_hint"]


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail: str = ""):
        super().__init__(detail or error)
        self.status = status
        self.error = error
        self.detail = detail or error


# 就绪结论缓存：键为 (模型路径, 大小+mtime)，值为该文件版本的真实结论。
# 只有**真正加载过**的判断（启动预热 / 导入校验 / 分析推理）会写入。
# /api/state 走 allow_load=False 的快路径时先查这里：命中即精确答案，
# 未命中且会话也没建好才退回文件级判断。没有这层缓存的话，「加载失败」的
# 结论就存不下来，坏模型会在首屏快路径上被永久报成「已就绪」。
_depth_ready_memo: Dict[tuple, bool] = {}
_normal_ready_memo: Dict[tuple, bool] = {}


def _model_stamp(path: str) -> tuple:
    """模型文件身份（路径 + 大小 + mtime），文件被替换后自然换键。"""
    from core.builtin_depth import _file_stamp
    return (path, _file_stamp(path))


def model_ready(model_path: str = "", allow_load: bool = True) -> bool:
    """深度模型是否**真的可用**（不只是大小够、也不是法线模型）。

    仅用 is_model_ready（只看 >1MB）会让「大于 1MB 但不是合法 ONNX」的文件
    显示为「已就绪」，而推理时才静默降级 —— 界面在撒谎。这里实际尝试加载
    推理会话；加载结果按 (路径, 大小+mtime) 缓存，因此首次之后几乎零成本。
    另外要排除「把法线模型填进深度栏」：那种文件能加载，但推理必然失败。

    allow_load=False 供 /api/state 这类热路径用：会话还没建好时只按文件级
    回答，不在这里阻塞（见 session_cached 的说明）。真正的加载由启动预热
    或首次推理完成，之后本函数自动给出精确答案。
    """
    from core.builtin_depth import (_get_session, is_model_ready,
                                    session_cached, validate_model)
    from core.preprocess import model_family, preferred_depth_model
    p = preferred_depth_model(model_path)
    if not is_model_ready(p):
        return False
    key = _model_stamp(p)
    memo = _depth_ready_memo.get(key)
    if memo is not None:
        return memo
    if not allow_load and not session_cached(p):
        return True     # 文件级就绪（预热尚无结论，精确答案稍后写入 memo）
    try:
        validate_model(p)          # 加载失败（非法 ONNX）会在这里抛
        ok = model_family(_get_session(p)) != "moge_normal"
    except Exception:
        ok = False
    _depth_ready_memo[key] = ok
    return ok


def normal_model_ready(model_path: str = "", allow_load: bool = True) -> bool:
    """法线模型是否真的可用（可选增强，缺省时法线由深度几何派生）。

    与 model_ready 同样要实际加载，并额外要求带 `normal` 输出：
    用户很容易把深度模型误填到法线栏，那种会话能加载、但推理时才报错。
    allow_load=False 的含义同 model_ready。
    """
    from core.builtin_depth import _get_session, is_model_ready, session_cached
    from core.preprocess import normal_model_path
    p = model_path or normal_model_path()
    if not is_model_ready(p):
        return False
    key = _model_stamp(p)
    memo = _normal_ready_memo.get(key)
    if memo is not None:
        return memo
    if not allow_load and not session_cached(p):
        return True     # 同上：文件级就绪，等预热给精确结论
    try:
        ok = "normal" in {o.name for o in _get_session(p).get_outputs()}
    except Exception:
        ok = False
    _normal_ready_memo[key] = ok
    return ok


# ---------------------------------------------------------------- 编解码工具

def _png_b64(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode_image_bytes(data: bytes) -> np.ndarray:
    try:
        img = Image.open(io.BytesIO(data))
        # 透明底压到白底。用 PIL 的 alpha_composite（C 实现）而不是 numpy 浮点混合：
        # 后者在 4096px 时会同时 materialize 数个 4096×4096×3 的 float32 数组
        # （每个约 200MB，瞬时峰值 ~600MB），这里峰值只是几份 uint8 图。
        if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            rgb = np.asarray(Image.alpha_composite(bg, img).convert("RGB"))
        else:
            rgb = np.asarray(img.convert("RGB"))
    except Exception as e:
        raise ApiError(400, "bad_image", f"无法解析图片：{e}") from e
    h, w = rgb.shape[:2]
    if not (MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE <= h <= MAX_SIZE):
        raise ApiError(400, "bad_size",
                       f"图片尺寸 {w}x{h} 超出支持范围（{MIN_SIZE}~{MAX_SIZE}px）")
    return rgb


def _b64_png_to_rgb(s: str) -> np.ndarray:
    if not s:
        raise ApiError(400, "missing_reference", "缺少参考图数据")
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception as e:
        raise ApiError(400, "bad_reference", f"参考图 base64 解码失败：{e}") from e
    return _decode_image_bytes(raw)


def _colorize_depth(depth: np.ndarray) -> np.ndarray:
    """深度（0~1，大=近）→ 彩色可视化。"""
    cmap = np.array([[5, 8, 48], [12, 88, 196], [26, 196, 178],
                     [242, 226, 92], [255, 255, 255]], np.float32)
    d = np.clip(np.nan_to_num(depth.astype(np.float32)), 0.0, 1.0)
    x = d * (len(cmap) - 1)
    i0 = np.floor(x).astype(np.int32)
    i1 = np.minimum(i0 + 1, len(cmap) - 1)
    f = (x - i0)[..., None]
    return (cmap[i0] * (1.0 - f) + cmap[i1] * f).astype(np.uint8)


def _params_from_dict(d: Any) -> RenderParams:
    """把前端 JSON 转成 RenderParams；未知字段忽略，类型错误 → 400。"""
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise ApiError(400, "bad_params", "params 必须是对象")
    lights_raw = d.get("lights", None)
    lights: Optional[List[Light]] = None
    if lights_raw is not None:
        if not isinstance(lights_raw, list):
            raise ApiError(400, "bad_params", "lights 必须是数组")
        lights = []
        for item in lights_raw:
            if not isinstance(item, dict):
                raise ApiError(400, "bad_params", "light 必须是对象")
            try:
                lights.append(Light.from_dict(item))
            except (TypeError, ValueError) as e:
                raise ApiError(400, "bad_params", f"light 字段非法：{e}") from e
    kw: Dict[str, Any] = {}
    for key in ("shadow_mode", "lighting_mode"):
        if key in d:
            if d[key] is not None and not isinstance(d[key], str):
                raise ApiError(400, "bad_params", f"{key} 必须是字符串")
            kw[key] = d[key]
    if "reference_offset" in d:
        if d["reference_offset"] is not None and \
                not isinstance(d["reference_offset"], dict):
            raise ApiError(400, "bad_params", "reference_offset 必须是对象")
        kw["reference_offset"] = d["reference_offset"]
    for key in ("ambient_intensity", "ambient_kelvin", "shadow_strength",
                "specular_strength", "tone_preserve", "exposure",
                "preview_size", "lighting_size"):
        if key in d:
            v = d[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ApiError(400, "bad_params", f"{key} 必须是数字")
            kw[key] = float(v) if key in (
                "ambient_intensity", "ambient_kelvin", "shadow_strength",
                "specular_strength", "tone_preserve", "exposure") else int(v)
    if kw.get("lighting_mode") not in (None, "linear", "hq"):
        raise ApiError(400, "bad_params", "lighting_mode 必须是 linear 或 hq")
    if kw.get("shadow_mode") not in (None, "soft", "hard"):
        raise ApiError(400, "bad_params", "shadow_mode 必须是 soft 或 hard")
    p = RenderParams(**kw)
    if lights is not None:
        p.lights = lights
    return p


# ---------------------------------------------------------------- 引擎调用

def _analyze(entry: Dict[str, Any]) -> dict:
    pipe = Pipeline(_config, log.info)
    res = pipe.pre.get_depth(entry["rgb"], encode_png(entry["rgb"]))
    entry["depth"] = res["depth"]
    entry["normal"] = res.get("normal")
    entry["backend"] = res.get("backend", "")
    entry["from_cache"] = bool(res.get("from_cache"))
    return res


def _render(entry: Dict[str, Any], params: RenderParams
            ) -> Tuple[np.ndarray, str, bool, float]:
    """复用已缓存深度；深度缺失时走 Pipeline（自身获取深度并回填缓存）。"""
    if entry.get("depth") is None:
        t0 = time.time()
        res = Pipeline(_config, log.info).run(entry["rgb"], params)
        entry["depth"] = res["depth"]
        entry["normal"] = res.get("normal")
        entry["backend"] = res.get("backend", "")
        entry["from_cache"] = bool(res.get("from_cache"))
        return (res["image"], res["backend"],
                bool(res.get("from_cache")), float(res.get("elapsed",
                                                           time.time() - t0)))
    t0 = time.time()
    img = render(entry["rgb"], entry["depth"], params,
                 normal=entry.get("normal"))
    return (img, entry.get("backend") or "cached",
            bool(entry.get("from_cache", True)), time.time() - t0)


def _normal_map(entry: Dict[str, Any]) -> Optional[np.ndarray]:
    n = entry.get("normal")
    if n is not None and getattr(n, "size", 0):
        return n
    d = entry.get("depth")
    if d is None:
        return None
    return height_to_normal(depth_to_height(d))


def _pick_from_normal(w: int, h: int, x: int, y: int,
                      nmap: Optional[np.ndarray]) -> dict:
    """点击坐标 + 法线图 → 光源方向与表面法线（图片与视频共用）。"""
    # 点击位置 → 指向光源的方向（图像平面内归一化，z 固定朝外）
    u = (x / max(w - 1, 1)) * 2.0 - 1.0
    v = (y / max(h - 1, 1)) * 2.0 - 1.0
    dz = 0.7
    n = float(np.sqrt(u * u + v * v + dz * dz)) or 1.0
    out = {"dx": float(u / n), "dy": float(v / n), "dz": float(dz / n)}
    if nmap is None:
        out.update({"nx": 0.0, "ny": 0.0, "nz": 1.0})
        return out
    nh, nw = nmap.shape[:2]
    iy = int(round(y * (nh - 1) / max(h - 1, 1)))
    ix = int(round(x * (nw - 1) / max(w - 1, 1)))
    vec = np.asarray(nmap[max(0, min(iy, nh - 1)), max(0, min(ix, nw - 1))],
                     np.float32).reshape(-1)[:3]
    nrm = float(np.linalg.norm(vec)) or 1.0
    out.update({"nx": float(vec[0] / nrm), "ny": float(vec[1] / nrm),
                "nz": float(vec[2] / nrm)})
    return out


# ---------------------------------------------------------------- 视频注册表与任务

_videos: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_jobs: Dict[str, Dict[str, Any]] = {}


def _video_busy(video_id: str) -> bool:
    """该视频是否有正在运行的任务。

    淘汰或删除视频前必须检查：1080p 导出要跑数分钟，worker 仍在
    `iter_frames`/`read_frame` 读源文件；此时删掉目录会让任务中途失败，
    而 `/api/video/process` 的输出就写在 entry["dir"] 里，删了就永久 404。
    """
    with _lock:
        return any(j.get("video_id") == video_id and j.get("state") == "running"
                   for j in _jobs.values())


def _evict_videos_locked() -> None:
    """按 LRU 淘汰，但**跳过有任务在跑**的视频（调用方需持有 _lock）。

    若所有条目都在跑任务，就暂不淘汰：宁可短暂超出上限，也不能删掉
    正在被 worker 读取的源文件。
    """
    if len(_videos) <= MAX_VIDEOS:
        return
    running = {j.get("video_id") for j in _jobs.values()
               if j.get("state") == "running"}
    for vid in list(_videos.keys()):
        if len(_videos) <= MAX_VIDEOS:
            break
        if vid in running:
            continue
        old = _videos.pop(vid)
        shutil.rmtree(old["dir"], ignore_errors=True)
        log.info("视频注册表已满，淘汰 %s", vid)


def _new_video_entry(src: str, name: str, info: VideoInfo) -> Dict[str, Any]:
    """登记一段已落盘的视频；淘汰旧条目时删除其临时文件。"""
    entry = {
        "id": os.path.basename(os.path.dirname(src)),
        "dir": os.path.dirname(src),
        "source_path": src,
        "source_name": name,
        "info": info,
        "output_path": None,
        "out_name": None,
        "job_id": None,
    }
    with _lock:
        _videos[entry["id"]] = entry
        _evict_videos_locked()
    return entry


def _live_video(video_id: str) -> Dict[str, Any]:
    with _lock:
        entry = _videos.get(video_id)
        if entry is None:
            raise ApiError(404, "unknown_video",
                           "视频不存在或已被淘汰，请重新上传")
        _videos.move_to_end(video_id)
    return entry


def _job_public(job: Dict[str, Any]) -> dict:
    return {"job_id": job["id"], "state": job["state"],
            "progress": round(float(job["progress"]), 4),
            "message": job["message"], "result": job["result"],
            "error": job["error"]}


def _video_worker(job_id: str, entry: Dict[str, Any], params: RenderParams,
                  mode: str, keyframe_count: int, smooth: float) -> None:
    """后台线程：跑引擎，把进度/结果写回任务表（异常不抛出）。"""
    job = _jobs[job_id]
    cancel = job["cancel_event"]

    def _progress(p: float, m: str) -> None:
        with _lock:
            j = _jobs.get(job_id)
            if j is None or j["state"] != "running":
                return
            j["progress"] = max(0.0, min(1.0, float(p)))
            j["message"] = str(m)

    ext = os.path.splitext(entry["source_path"])[1].lower() or ".mp4"
    out_name = "result" + ext
    out_path = os.path.join(entry["dir"], out_name)
    try:
        res = process_video(entry["source_path"], out_path, params,
                            config=_config, mode=mode,
                            keyframe_count=keyframe_count, smooth=smooth,
                            progress=_progress, cancel=cancel.is_set)
    except _Cancelled:
        with _lock:
            job["state"] = "cancelled"
            job["message"] = "已取消"
        return
    except Exception as exc:
        log.error("视频处理失败 %s：%s\n%s", job_id, exc, traceback.format_exc())
        with _lock:
            job["state"] = "error"
            job["message"] = "处理失败"
            job["error"] = str(exc)[:300] or exc.__class__.__name__
        return

    with _lock:
        entry["output_path"] = out_path
        entry["out_name"] = out_name
        job["state"] = "done"
        job["progress"] = 1.0
        job["message"] = "完成"
        job["result"] = dict(res, download_name=out_name)


# ---------------------------------------------------------------- 请求模型

class ImageIdBody(BaseModel):
    image_id: str


class RenderBody(BaseModel):
    image_id: str
    params: Dict[str, Any] = {}


class PickBody(BaseModel):
    image_id: str
    x: int
    y: int


class ReferenceBody(BaseModel):
    image_id: str
    params: Dict[str, Any] = {}
    reference_png_b64: str


class ExportBody(BaseModel):
    image_id: str
    params: Dict[str, Any] = {}
    format: str = "png"


class VideoIdBody(BaseModel):
    video_id: str


class VideoFrameBody(BaseModel):
    video_id: str
    index: int


class VideoKeyframesBody(BaseModel):
    video_id: str
    count: int = 8


class VideoPickBody(BaseModel):
    video_id: str
    index: int
    x: int
    y: int


class VideoProcessBody(BaseModel):
    video_id: str
    params: Dict[str, Any] = {}
    mode: str = "keyframe"
    keyframe_count: int = 8
    smooth: float = 0.6


# ---------------------------------------------------------------- API 路由

@app.get("/api/state")
def api_state() -> dict:
    cfg = _config
    return {
        "presets": [{"name": n, "params": get_preset(n).to_dict()}
                    for n in PRESET_NAMES],
        "preset_names": list(PRESET_NAMES),
        "settings": _masked_settings(cfg),
        "cache_dir": CACHE_DIR_DEFAULT,
        # allow_load=False：这两个字段是首屏热路径，不能在这里等模型加载
        # （见 model_ready 的说明）。预热线程负责真正加载，warmup 字段
        # 告诉前端当前是「加载中」还是已有精确结论。
        "model_ready": model_ready(cfg.builtin_model_path, allow_load=False),
        "normal_model_ready": normal_model_ready(cfg.builtin_normal_model_path,
                                                 allow_load=False),
        # 启动预热进度（idle/warming/ready/missing）。首屏拿到 warming 说明
        # 后台还在加载模型，界面可据此显示「引擎加载中…」而不是谎报未就绪。
        "warmup": dict(_warmup),
        "version": APP_VERSION,
        "high_res_hint": bool(_state.get("high_res_hint", False)),
        "ffmpeg": has_ffmpeg(),
        "video_exts": list(VIDEO_EXTS),
        "video_limits": {
            "recommend_max_side": REC_MAX_SIDE,
            "recommend_max_duration": REC_MAX_DURATION,
            "hard_max_side": HARD_MAX_SIDE,
            "hard_max_duration": HARD_MAX_DURATION,
        },
        # 界面当前参数（工作流/自动打光的基准，可被 /api/autolight 的 apply 更新）
        "params": _state.get("params"),
        "themes": _themes_brief(),
    }


def _themes_brief() -> dict:
    """主题清单（失败不影响 /api/state）。current 为已持久化的选择。"""
    try:
        from core.theme import DEFAULT_THEME, list_themes
        return {"default": DEFAULT_THEME, "current": _config.theme or DEFAULT_THEME,
                "themes": list_themes()}
    except Exception:
        return {"default": "blue", "current": "blue", "themes": []}


def _do_image_upload(data: bytes, filename: str = "") -> dict:
    """图片上传核心（供 /api/image 与统一入口 /api/media 共用）。"""
    if not data:
        raise ApiError(400, "empty_body", "请求体为空")
    rgb = _decode_image_bytes(data)
    stem = os.path.basename(filename) if filename else ""
    entry = _new_entry(rgb, stem or "image.png")
    h, w = rgb.shape[:2]
    return {"image_id": entry["id"], "width": int(w), "height": int(h),
            "original_png_b64": _png_b64(rgb),
            "high_res_hint": entry["high_res_hint"]}


@app.post("/api/image")
async def api_image(request: Request) -> dict:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ctype and ctype not in ("image/png", "image/jpeg", "image/jpg"):
        raise ApiError(400, "bad_content_type",
                       "请以 image/png 或 image/jpeg 提交原始图片字节")
    data = await request.body()
    # 图像解码 + 整图 PNG 重编码 + base64 都是 CPU 密集的同步活：
    # 4096px 时 _decode_image_bytes 会临时 materialize 数个 4096²×3 的 float32
    # 数组（每个约 200MB），_png_b64 还要做整图压缩编码。直接在事件循环里跑会让
    # 整个服务停摆数秒（界面自身的 /api/state 轮询也会卡住），因此移入线程。
    return await asyncio.to_thread(_do_image_upload, data,
                                   request.headers.get("x-filename") or "")


@app.post("/api/analyze")
def api_analyze(body: ImageIdBody) -> dict:
    entry = _live_entry(body.image_id)
    res = _analyze(entry)
    normal = entry.get("normal")
    normal_b64 = None
    if normal is not None and getattr(normal, "size", 0):
        n = np.clip(normal[..., :3], -1.0, 1.0)
        normal_b64 = _png_b64(((n * 0.5 + 0.5) * 255).astype(np.uint8))
    _refresh_high_res(entry)
    return {"backend": entry["backend"],
            "from_cache": bool(entry["from_cache"]),
            "depth_png_b64": _png_b64((np.clip(entry["depth"], 0, 1) * 255
                                       ).astype(np.uint8)),
            "normal_png_b64": normal_b64}


@app.post("/api/render")
def api_render(body: RenderBody) -> dict:
    entry = _live_entry(body.image_id)
    params = _params_from_dict(body.params)
    # 渲染即「当前参数」的权威信号：界面每次改参数都会触发渲染，
    # 因此这里记录后，专业模式/自动打光的基准才是用户手上的真实灯组，
    # 而不是启动时的默认预设。
    _set_current_params(params)
    img, backend, from_cache, elapsed = _render(entry, params)
    h, w = img.shape[:2]
    _refresh_high_res(entry)
    return {"png_b64": _png_b64(img), "elapsed": round(float(elapsed), 3),
            "backend": backend, "from_cache": from_cache,
            "width": int(w), "height": int(h),
            "high_res_hint": entry["high_res_hint"]}


@app.post("/api/pick")
def api_pick(body: PickBody) -> dict:
    entry = _live_entry(body.image_id)
    w, h = entry["width"], entry["height"]
    if not (0 <= body.x < w and 0 <= body.y < h):
        raise ApiError(400, "bad_point", f"坐标 ({body.x},{body.y}) 超出图片范围")
    return _pick_from_normal(w, h, body.x, body.y, _normal_map(entry))


@app.post("/api/reference")
def api_reference(body: ReferenceBody) -> dict:
    entry = _live_entry(body.image_id)
    params = _params_from_dict(body.params)
    ref_rgb = _b64_png_to_rgb(body.reference_png_b64)
    features = analyze_reference(ref_rgb)
    out = apply_reference_offset(params, features)
    return {"params": out.to_dict(),
            "offset": out.reference_offset or {
                "d_angle_deg": 0.0, "d_kelvin": 0.0, "d_intensity": 0.0}}


@app.post("/api/export")
def api_export(body: ExportBody) -> dict:
    entry = _live_entry(body.image_id)
    fmt = (body.format or "png").lower()
    if fmt not in ("png", "jpeg", "jpg"):
        raise ApiError(400, "bad_format", "format 必须是 png 或 jpeg")
    params = _params_from_dict(body.params)
    img, _backend, _fc, _elapsed = _render(entry, params)
    buf = io.BytesIO()
    if fmt == "png":
        Image.fromarray(img).save(buf, format="PNG")
        filename = "result.png"
    else:
        Image.fromarray(img).save(buf, format="JPEG", quality=95,
                                  subsampling=0)
        filename = "result.jpg"
    return {"png_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "filename": filename}


@app.post("/api/depth-preview")
def api_depth_preview(body: ImageIdBody) -> dict:
    entry = _live_entry(body.image_id)
    if entry.get("depth") is None:
        _analyze(entry)
    return {"depth_png_b64": _png_b64(_colorize_depth(entry["depth"]))}


@app.post("/api/settings")
async def api_settings(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as e:
        raise ApiError(400, "bad_json", f"请求体不是合法 JSON：{e}") from e
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "请求体必须是 JSON 对象")

    fields = set(AIBackendConfig().__dict__.keys())
    unknown = [k for k in body if k not in fields]
    if unknown:
        raise ApiError(400, "bad_settings",
                       f"未知设置项：{', '.join(sorted(unknown))}")
    if "order" in body:
        order = body["order"]
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            raise ApiError(400, "bad_settings", "order 必须是字符串数组")
        if any(b not in BACKENDS for b in order):
            raise ApiError(400, "bad_settings",
                           f"order 仅支持：{', '.join(BACKENDS)}")
    for key in ("builtin_enabled", "local_enabled", "cloud_enabled",
                "preview_only"):
        if key in body and not isinstance(body[key], bool):
            raise ApiError(400, "bad_settings", f"{key} 必须是布尔值")
    if "timeout" in body:
        t = body["timeout"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            raise ApiError(400, "bad_settings", "timeout 必须是正数")
    if "cloud_api_key" in body and not isinstance(body["cloud_api_key"], str):
        raise ApiError(400, "bad_settings", "cloud_api_key 必须是字符串")

    global _config
    with _lock:
        cfg = _config
        for key, value in body.items():
            if key == "cloud_api_key":
                continue          # 空字符串 = 保留原 Key
            setattr(cfg, key, value)
        key = body.get("cloud_api_key", "")
        if key.strip():
            cfg.cloud_api_key = key
        cfg.save()
        _config = cfg
        for entry in _images.values():
            _refresh_high_res(entry)
    return {"ok": True, "settings": _masked_settings(_config)}


@app.post("/api/cache/clear")
def api_cache_clear() -> dict:
    return {"removed": int(AICache().clear())}


# ---------------------------------------------------------------- 视频路由

async def _do_video_upload(stream, filename: str = "", ext_hint: str = "") -> dict:
    """视频上传核心（供 /api/video 与统一入口 /api/media 共用）。

    ext_hint：由调用方（如魔数嗅探）推断出的扩展名，用于补上缺失的文件名。
    """
    name = os.path.basename(filename) if filename else ""
    ext = os.path.splitext(name)[1].lower() or ext_hint
    if ext not in VIDEO_EXTS:
        raise ApiError(400, "bad_extension",
                       f"不支持的视频格式 {ext or '(无扩展名)'}；"
                       f"仅支持：{', '.join(VIDEO_EXTS)}")
    if not name:
        name = "video" + ext
    if not has_ffmpeg():
        raise ApiError(503, "ffmpeg_missing",
                       "未找到可用的 ffmpeg，无法处理视频；"
                       "请安装 imageio-ffmpeg 或用 HLS_FFMPEG 指定路径")

    video_id = uuid.uuid4().hex
    work_dir = os.path.join(_VIDEO_DIR, video_id)
    source = os.path.join(work_dir, "source" + ext)
    os.makedirs(work_dir, exist_ok=True)
    size = 0
    try:
        with open(source, "wb") as f:
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            else:
                data = stream if isinstance(stream, (bytes, bytearray)) else stream.read()
                if data:
                    f.write(data)
                    size = len(data)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ApiError(400, "bad_upload", f"视频上传失败：{e}") from e
    if size == 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ApiError(400, "empty_body", "请求体为空")

    try:
        # probe 会启动 ffmpeg 子进程解析容器头，是阻塞调用；放进线程执行，
        # 否则上传大视频期间整个服务（含 /api/state 轮询）都会停摆。
        info = await asyncio.to_thread(probe, source)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ApiError(400, "bad_video", f"无法解析视频：{e}") from e

    entry = _new_video_entry(source, name, info)
    return {"video_id": entry["id"], "info": info.to_dict(),
            "hints": info.hints(), "source_name": name}


@app.post("/api/video")
async def api_video_upload(request: Request) -> dict:
    return await _do_video_upload(request.stream(),
                                  request.headers.get("x-filename") or "")


@app.post("/api/video/frame")
def api_video_frame(body: VideoFrameBody) -> dict:
    entry = _live_video(body.video_id)
    info: VideoInfo = entry["info"]
    try:
        rgb = read_frame(entry["source_path"], body.index, info)
    except IndexError as e:
        raise ApiError(400, "bad_frame", str(e)) from e
    h, w = rgb.shape[:2]
    return {"png_b64": _png_b64(rgb), "width": int(w), "height": int(h),
            "index": int(body.index)}


@app.post("/api/video/keyframes")
def api_video_keyframes(body: VideoKeyframesBody) -> dict:
    entry = _live_video(body.video_id)
    if not (1 <= body.count <= 64):
        raise ApiError(400, "bad_params", "count 必须在 1~64 之间")
    keys = select_keyframes(entry["source_path"], entry["info"],
                            count=body.count)
    return {"keyframes": [int(i) for i in keys], "count": len(keys)}


@app.post("/api/video/pick")
def api_video_pick(body: VideoPickBody) -> dict:
    entry = _live_video(body.video_id)
    info: VideoInfo = entry["info"]
    w, h = info.width, info.height
    if not (0 <= body.x < w and 0 <= body.y < h):
        raise ApiError(400, "bad_point", f"坐标 ({body.x},{body.y}) 超出画面范围")
    try:
        rgb = read_frame(entry["source_path"], body.index, info)
    except IndexError as e:
        raise ApiError(400, "bad_frame", str(e)) from e
    res = Pipeline(_config, log.info).pre.get_depth(rgb, encode_png(rgb))
    nmap = res.get("normal")
    if nmap is None or not getattr(nmap, "size", 0):
        nmap = height_to_normal(depth_to_height(res["depth"]))
    return _pick_from_normal(w, h, body.x, body.y, nmap)


@app.post("/api/video/process", status_code=202)
def api_video_process(body: VideoProcessBody) -> dict:
    entry = _live_video(body.video_id)
    if body.mode not in ("keyframe", "perframe"):
        raise ApiError(400, "bad_params", "mode 必须是 keyframe 或 perframe")
    if not (1 <= body.keyframe_count <= 64):
        raise ApiError(400, "bad_params", "keyframe_count 必须在 1~64 之间")
    if not (0.0 <= body.smooth <= 1.0):
        raise ApiError(400, "bad_params", "smooth 必须在 0~1 之间")
    params = _params_from_dict(body.params)

    with _lock:
        cur = _jobs.get(entry.get("job_id") or "")
        if cur is not None and cur["state"] == "running":
            raise ApiError(409, "job_running",
                           "该视频已有处理任务正在进行，请等待完成或先取消")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "id": job_id,
            "video_id": entry["id"],
            "state": "running",
            "progress": 0.0,
            "message": "准备中",
            "result": None,
            "error": None,
            "cancel_event": threading.Event(),
        }
        entry["job_id"] = job_id

    threading.Thread(target=_video_worker,
                     args=(job_id, entry, params, body.mode,
                           int(body.keyframe_count), float(body.smooth)),
                     daemon=True, name=f"video-{job_id[:8]}").start()
    return {"job_id": job_id}


@app.get("/api/video/job/{job_id}")
def api_video_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise ApiError(404, "unknown_job", "任务不存在或已过期")
        return _job_public(job)


@app.post("/api/video/job/{job_id}/cancel")
def api_video_job_cancel(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise ApiError(404, "unknown_job", "任务不存在或已过期")
        if job["state"] == "running":
            job["cancel_event"].set()
            job["message"] = "正在取消…"
        return {"ok": True, "job_id": job_id, "state": job["state"]}


@app.get("/api/video/thumbnails/{video_id}")
def api_video_thumbnails(video_id: str, count: int = 12,
                         width: int = 96) -> dict:
    """生成时间轴缩略图条（**单次顺序解码**，比逐帧 seek 快得多）。"""
    entry = _live_video(video_id)
    info = entry["info"]
    n = max(2, min(int(count), 40))
    w = max(32, min(int(width), 320))
    h = max(1, int(round(w * info.height / max(info.width, 1))))

    step = max(1, info.frame_count // n)
    out: List[str] = []
    idx: List[int] = []
    for k, frame in enumerate(iter_frames(entry["source_path"], info,
                                          scale_to=(w, h), stride=step)):
        if k >= n:
            break
        out.append(_png_b64(frame))
        idx.append(min(k * step, max(info.frame_count - 1, 0)))
    return {"count": len(out), "frames": idx, "width": w, "height": h,
            "png_b64": out}


@app.get("/api/video/result/{video_id}")
def api_video_result(video_id: str) -> FileResponse:
    entry = _live_video(video_id)
    path = entry.get("output_path")
    if not path or not os.path.isfile(path):
        raise ApiError(404, "no_result", "结果尚未生成，请先完成视频处理")
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(path,
                        media_type=_VIDEO_MIME.get(ext,
                                                   "application/octet-stream"),
                        filename=entry.get("out_name") or ("result" + ext))


@app.post("/api/video/delete")
def api_video_delete(body: VideoIdBody) -> dict:
    # 有任务在跑时拒绝删除：worker 正在读源文件，删掉会让任务中途失败，
    # 且 /api/video/process 的成品就在该目录里，删了就永久下载不到。
    if _video_busy(body.video_id):
        raise ApiError(409, "video_busy",
                       "该视频有任务正在运行，请先等待完成或取消后再删除")
    with _lock:
        entry = _videos.pop(body.video_id, None)
    if entry is None:
        raise ApiError(404, "unknown_video", "视频不存在或已被淘汰，请重新上传")
    shutil.rmtree(entry["dir"], ignore_errors=True)
    return {"ok": True}


# ---------------------------------------------------------------- 异常与 CORS

@app.exception_handler(ApiError)
async def _api_error_handler(_req: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.error, "detail": exc.detail},
                        status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_handler(_req: Request,
                              exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"error": "bad_request",
                         "detail": "请求参数校验失败"},
                        status_code=400)


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_req: Request,
                              exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": "http_error", "detail": str(exc.detail)},
                        status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled_handler(_req: Request, exc: Exception) -> JSONResponse:
    log.error("未处理异常：%s\n%s", exc, traceback.format_exc())
    return JSONResponse({"error": "internal_error", "detail": "服务器内部错误"},
                        status_code=500)


_LOCAL_ORIGIN = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")


@app.middleware("http")
async def _local_only(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and not _LOCAL_ORIGIN.match(origin):
        return JSONResponse({"error": "forbidden_origin",
                             "detail": "仅允许本机来源访问"}, status_code=403)
    if request.method == "OPTIONS" and origin:
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Filename",
            "Access-Control-Max-Age": "600",
        })
    response = await call_next(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


# ---------------------------------------------------------------- 主题引擎

@app.get("/api/themes")
def api_themes() -> dict:
    from core.theme import DEFAULT_THEME, list_themes
    return {"default": DEFAULT_THEME, "themes": list_themes()}


@app.get("/api/theme/{theme_id}")
def api_theme(theme_id: str) -> dict:
    from core.theme import css_variables, get_theme
    t = get_theme(theme_id)
    t["css"] = css_variables(theme_id)
    return t


class ThemeBody(BaseModel):
    id: str


@app.post("/api/theme")
def api_theme_set(body: ThemeBody) -> dict:
    """选择并**持久化**主题（写入 config.json，重启后保持）。"""
    from core.theme import THEME_IDS, css_variables, get_theme
    if body.id not in THEME_IDS:
        raise ApiError(400, "unknown_theme",
                       f"未知主题 {body.id}；可用：{', '.join(THEME_IDS)}")
    _config.theme = body.id
    try:
        _config.save()
    except Exception as e:
        raise ApiError(500, "save_failed", f"主题已应用但未能写入配置：{e}")
    t = get_theme(body.id)
    t["css"] = css_variables(body.id)
    return {"ok": True, "theme": t}


# ---------------------------------------------------------------- AI 自动打光

class AutoLightBody(BaseModel):
    text: str = ""
    use_cloud: bool = False
    base_params: Optional[Dict[str, Any]] = None
    apply: bool = False            # True 时把结果直接设为当前参数


@app.post("/api/autolight")
def api_autolight(body: AutoLightBody) -> dict:
    """自然语言 → 光照节点（离线确定性解析；云端可选）。"""
    from core.autolight import rig_from_text, rig_to_nodes, to_render_params

    text = (body.text or "").strip()
    if not text:
        raise ApiError(400, "bad_params", "请填写光照描述文本")
    if len(text) > 500:
        raise ApiError(400, "bad_params", "描述文本过长（最多 500 字）")

    base = _params_from_dict(body.base_params) if body.base_params else None
    rig = rig_from_text(text, base=base, config=_config,
                        use_cloud=bool(body.use_cloud), log=log.info)
    params = to_render_params(rig, base)
    if body.apply:
        _set_current_params(params)
    return {"rig": rig.to_dict(), "params": params.to_dict(),
            "nodes": rig_to_nodes(rig), "note": rig.note,
            "source": rig.source}


# ---------------------------------------------------------------- 插件管理

def _plugins():
    """惰性构造插件注册表（模块缺失时给出可读错误而非 500 堆栈）。"""
    try:
        from core.plugins import PluginRegistry
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "plugins_unavailable", f"插件模块不可用：{e}")
    return PluginRegistry()


@app.get("/api/plugins")
def api_plugins() -> dict:
    reg = _plugins()
    reg.scan()
    return reg.export_state()


@app.post("/api/plugins/install")
async def api_plugins_install(request: Request) -> dict:
    """安装插件：接收插件清单 JSON（数据，不执行任何代码）。"""
    raw = await request.body()
    if not raw:
        raise ApiError(400, "bad_params", "请求体为空：请提交插件清单 JSON")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ApiError(400, "bad_manifest", f"清单不是合法 JSON：{e}")
    for key in ("id", "name", "version", "kind"):
        if not data.get(key):
            raise ApiError(400, "bad_manifest", f"清单缺少字段：{key}")
    reg = _plugins()
    # 契约接口是 install(manifest_json_path)：先落盘再安装（只解析 JSON，不执行代码）
    tmp_dir = reg.plugins_dir if hasattr(reg, "plugins_dir") else None
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=tmp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        m = reg.install(tmp_path)
    except ValueError as e:
        raise ApiError(400, "bad_manifest", str(e))
    except Exception as e:
        raise ApiError(400, "bad_manifest", f"清单不合法：{e}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return {"ok": True, "plugin": m.to_dict(), "state": reg.export_state()}


class PluginToggleBody(BaseModel):
    id: str
    enabled: bool = True


@app.post("/api/plugins/toggle")
def api_plugins_toggle(body: PluginToggleBody) -> dict:
    reg = _plugins()
    reg.scan()
    if not reg.set_enabled(body.id, bool(body.enabled)):
        raise ApiError(404, "unknown_plugin", f"未找到插件 {body.id}")
    return {"ok": True, "state": reg.export_state()}


class PluginRemoveBody(BaseModel):
    id: str


@app.post("/api/plugins/remove")
def api_plugins_remove(body: PluginRemoveBody) -> dict:
    reg = _plugins()
    reg.scan()
    if not reg.remove(body.id):
        raise ApiError(404, "unknown_plugin", f"未找到插件 {body.id}")
    return {"ok": True, "state": reg.export_state()}


# ---------------------------------------------------------------- 专业模式（工作流）

@app.get("/api/workflow/types")
def api_workflow_types() -> dict:
    try:
        from core.workflow import default_workflow, node_types
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "workflow_unavailable", f"工作流模块不可用：{e}")
    return {"node_types": node_types(),
            "default": default_workflow().to_dict()}


class WorkflowBody(BaseModel):
    workflow: Dict[str, Any]


@app.post("/api/workflow/evaluate")
def api_workflow_evaluate(body: WorkflowBody) -> dict:
    """校验并求解节点图 → 渲染参数。"""
    try:
        from core.workflow import Workflow, evaluate, validate
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "workflow_unavailable", f"工作流模块不可用：{e}")
    try:
        wf = Workflow.from_dict(body.workflow or {})
    except Exception as e:
        raise ApiError(400, "bad_workflow", f"工作流数据不合法：{e}")
    problems = validate(wf)
    if problems:
        return {"ok": False, "problems": problems, "params": None}
    try:
        params = evaluate(wf, _current_params())
    except ValueError as e:
        return {"ok": False, "problems": [str(e)], "params": None}
    return {"ok": True, "problems": [], "params": params.to_dict()}


# ---------------------------------------------------------------- 统一媒体入口

_IMAGE_EXTS = (".png", ".jpg", ".jpeg")


@app.post("/api/media")
async def api_media(request: Request) -> dict:
    """统一入口：按文件内容/扩展名自动识别图片或视频，不需要用户先选模式。

    两点设计：
      1. 直接调用两条上传路径的核心函数，**不要求**调用方声明 Content-Type
         或提供 X-Filename（curl --data-binary、fetch(Blob) 都能用）。
      2. **不把整个视频读进内存**：只先取一小段用于嗅探，随后把
         「前缀 + 剩余流」交给视频写入逻辑继续流式落盘。规格允许
         1080p/5 分钟（数百 MB~GB），一次性缓冲会造成明显的内存峰值。
    """
    filename = (request.headers.get("x-filename") or "").strip()
    stream = request.stream()

    prefix = b""
    async for chunk in stream:
        prefix += chunk
        if len(prefix) >= _SNIFF_BYTES:
            break
    if not prefix:
        raise ApiError(400, "bad_params", "请求体为空：请提交图片或视频的原始字节")

    kind, ext_hint = _sniff_media(prefix, filename)
    if kind == "image":
        rest = b""
        async for chunk in stream:
            rest += chunk
        # 同 api_image：解码 + PNG 重编码是重活，移入线程避免阻塞事件循环
        result = await asyncio.to_thread(_do_image_upload, prefix + rest, filename)
        result["kind"] = "image"
        return result
    if kind == "video":
        async def _replay():
            """先吐出已读取的前缀，再继续转发剩余流。"""
            yield prefix
            async for chunk in stream:
                yield chunk
        result = await _do_video_upload(_replay(), filename, ext_hint)
        result["kind"] = "video"
        return result
    raise ApiError(400, "unknown_media",
                   "无法识别的媒体类型：支持 PNG/JPEG 图片与 "
                   + "、".join(VIDEO_EXTS) + " 视频")


def _sniff_media(data: bytes, filename: str) -> Tuple[str, str]:
    """判断媒体类型：魔数优先，扩展名兜底。返回 (kind, 扩展名提示)。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return "image", ""
    if data[4:8] == b"ftyp":                       # MP4/MOV/M4V
        brand = data[8:12]
        ext = {b"qt  ": ".mov", b"M4V ": ".m4v"}.get(brand, ".mp4")
        return "video", ext
    if data[:4] == b"\x1aE\xdf\xa3":               # Matroska / WebM
        ext = ".webm" if b"webm" in data[:4096].lower() else ".mkv"
        return "video", ext
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video", ".avi"
    ext = os.path.splitext(filename)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image", ""
    if ext in VIDEO_EXTS:
        return "video", ext
    return "", ""


# ---------------------------------------------------------------- CUE 预设（带淡变）

def _cues():
    try:
        from core.cues import CueStore
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "cues_unavailable", f"CUE 模块不可用：{e}")
    return CueStore()


@app.get("/api/cues")
def api_cues() -> dict:
    return {"cues": _cues().list()}


class CueSaveBody(BaseModel):
    name: str = ""
    params: Dict[str, Any]
    fade: float = 1.0
    id: str = ""


@app.post("/api/cues/save")
def api_cues_save(body: CueSaveBody) -> dict:
    store = _cues()
    cue = store.save(body.name, _params_from_dict(body.params), body.fade,
                     body.id or "")
    return {"cue": cue.to_dict() if hasattr(cue, "to_dict") else cue,
            "cues": store.list()}


class CueIdBody(BaseModel):
    id: str


@app.post("/api/cues/delete")
def api_cues_delete(body: CueIdBody) -> dict:
    store = _cues()
    if not store.remove(body.id):
        raise ApiError(404, "unknown_cue", f"未找到 CUE {body.id}")
    return {"ok": True, "cues": store.list()}


class CueInterpBody(BaseModel):
    id: str
    params: Dict[str, Any]
    t: float = 0.0


@app.post("/api/cues/interpolate")
def api_cues_interpolate(body: CueInterpBody) -> dict:
    """在「当前参数」与指定 CUE 之间按 t(0~1) 插值（用于淡变预览）。"""
    store = _cues()
    if store.get(body.id) is None:
        raise ApiError(404, "unknown_cue", f"未找到 CUE {body.id}")
    out = store.interpolate(body.id, _params_from_dict(body.params), body.t)
    return {"params": out.to_dict()}


# ---------------------------------------------------------------- 智能拾光

@app.post("/api/harvest")
async def api_harvest(request: Request) -> dict:
    """从参考图提取灯光方案（只返回引擎能真实推导的量）。"""
    try:
        from core.harvest import harvest, harvest_to_params
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "harvest_unavailable", f"智能拾光模块不可用：{e}")
    data = await request.body()
    if not data:
        raise ApiError(400, "empty_body", "请求体为空：请提交参考图字节")
    # 图像解码 + 梯度分析都是 CPU 密集的同步活，放进线程以免阻塞事件循环
    # （本端点是 async def；直接跑会让 /api/state、视频进度轮询等全部停摆）。
    def _work() -> dict:
        rgb = _decode_image_bytes(data)
        res = harvest(rgb)
        params = harvest_to_params(res, _current_params())
        payload = res.to_dict()
        payload["params"] = params.to_dict()
        return payload
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------- 逐帧光绘

def _strokes_module():
    try:
        from core import strokes as mod
    except ImportError as e:                       # pragma: no cover
        raise ApiError(503, "strokes_unavailable", f"光绘模块不可用：{e}")
    return mod


class StrokesPreviewBody(BaseModel):
    video_id: str
    index: int = 0
    strokes: List[Dict[str, Any]] = []


@app.post("/api/strokes/preview")
def api_strokes_preview(body: StrokesPreviewBody) -> dict:
    mod = _strokes_module()
    entry = _live_video(body.video_id)
    info: VideoInfo = entry["info"]
    if not (0 <= body.index < max(info.frame_count, 1)):
        raise ApiError(400, "bad_frame",
                       f"帧序号 {body.index} 超出范围（共 {info.frame_count} 帧）")
    try:
        rgb = read_frame(entry["source_path"], body.index, info)
        strokes = [mod.Stroke.from_dict(s) for s in (body.strokes or [])]
        out = mod.apply_strokes(rgb, strokes)
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(400, "bad_strokes", f"光绘参数不合法：{e}") from e
    return {"png_b64": _png_b64(out), "index": int(body.index),
            "strokes": len(strokes)}


class StrokesExportBody(BaseModel):
    video_id: str
    params: Dict[str, Any]
    track: Dict[str, List[Dict[str, Any]]] = {}


@app.post("/api/strokes/export", status_code=202)
def api_strokes_export(body: StrokesExportBody) -> dict:
    """把「光照渲染 + 逐帧光绘」合成为新视频（后台任务）。"""
    mod = _strokes_module()
    entry = _live_video(body.video_id)
    params = _params_from_dict(body.params)

    with _lock:
        if any(j.get("video_id") == body.video_id and j.get("state") == "running"
               for j in _jobs.values()):
            raise ApiError(409, "job_running", "该视频已有任务在运行，请先等待或取消")

    try:
        track = mod.stroke_track_from_dict(body.track or {})
    except Exception as e:
        raise ApiError(400, "bad_strokes", f"光绘轨道不合法：{e}") from e

    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "video_id": body.video_id, "state": "running",
            "progress": 0.0, "message": "开始导出", "result": None,
            "error": None, "cancel_event": threading.Event(), "kind": "strokes",
        }
    t = threading.Thread(target=_strokes_worker, args=(job_id, entry, params, track),
                         daemon=True)
    t.start()
    return {"job_id": job_id}


def _strokes_output_path(src: str, job_id: str) -> tuple[str, str]:
    """逐帧光绘导出的输出路径与下载名。

    **必须与源视频同格式**（规格要求）：扩展名沿用源文件，不能写死 .mp4，
    否则从 .avi/.webm 源导出会悄悄变成 mp4；WebM 还会因沿用 libx264 直接失败。
    无扩展名时退回 mp4，避免产出无后缀文件。
    输出写到独立的导出目录：视频注册表是 LRU（最多 4 条），淘汰时会 rmtree
    该视频的临时目录，那样已完成的导出会被连带删除而任务 id 仍有效 → 下载 404。
    """
    ext = os.path.splitext(src)[1].lower() or ".mp4"
    out_dir = subdir("exports")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{job_id}{ext}"), f"stroked{ext}"


def _strokes_worker(job_id: str, entry: Dict[str, Any], params: RenderParams,
                    track: Dict[int, list]) -> None:
    """逐帧：渲染光照 → 叠加该帧光绘 → 写入视频。"""
    from core.video import VideoWriter
    from core.render import render as render_frame
    from core.video import _Cancelled

    info: VideoInfo = entry["info"]
    src = entry["source_path"]
    # 输出写到独立的导出目录，**不要**放在该视频的临时目录里：
    # 视频注册表是 LRU（最多 4 条），淘汰时会 rmtree 该目录，
    # 那样一个已完成的光绘导出会在用户再上传几个视频后被连带删除，
    # 而任务 id 仍然有效 → 下载 404。
    out_path, out_name = _strokes_output_path(src, job_id)
    mod = _strokes_module()

    def cancelled() -> bool:
        job = _jobs.get(job_id)
        return bool(job and job["cancel_event"].is_set())

    def progress(p: float, m: str) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["progress"] = float(max(0.0, min(1.0, p)))
                job["message"] = m

    try:
        # 深度按帧获取（与图片流程一致），并逐帧叠加光绘
        from core.ai_backend import AIPreprocessor
        from core.cache import AICache
        pre = AIPreprocessor(_config, AICache())
        written = 0
        with VideoWriter(out_path, info.width, info.height, info.fps,
                         audio_from=src if info.has_audio else None) as w:
            for i, frame in enumerate(iter_frames(src, info)):
                if cancelled():
                    raise _Cancelled()
                depth = pre.get_depth(frame)["depth"]
                lit = render_frame(frame, depth, params, None, None)
                strokes = track.get(i) or []
                if strokes:
                    lit = mod.apply_strokes(lit, strokes)
                w.write(lit)
                written += 1
                if i % 3 == 0 or i == info.frame_count - 1:
                    progress(0.05 + 0.9 * (i + 1) / max(info.frame_count, 1),
                             f"导出 {i + 1}/{info.frame_count}")
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["state"] = "done"
                job["progress"] = 1.0
                job["message"] = "完成"
                job["result"] = {"frames": written, "output": out_path,
                                 "download_name": out_name}
    except _Cancelled:
        try:
            os.remove(out_path)
        except OSError:
            pass
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["state"] = "cancelled"
                job["message"] = "已取消"
    except Exception as e:
        log.exception("逐帧光绘导出失败")
        try:
            os.remove(out_path)
        except OSError:
            pass
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["state"] = "error"
                job["error"] = f"导出失败：{e}"


@app.get("/api/strokes/result/{job_id}")
def api_strokes_result(job_id: str) -> FileResponse:
    """下载逐帧光绘导出的成品视频。"""
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        raise ApiError(404, "unknown_job", "任务不存在或已过期")
    result = job.get("result") or {}
    path = result.get("output")
    if not path or not os.path.isfile(path):
        raise ApiError(404, "no_result", "结果尚未生成，请先完成导出")
    ext = os.path.splitext(path)[1].lower()
    name = result.get("download_name") or ("stroked" + ext)
    return FileResponse(path, media_type=_VIDEO_MIME.get(ext, "video/mp4"),
                        filename=name)


@app.post("/api/shutdown", status_code=202)
def api_shutdown() -> dict:
    """请求退出软件。

    打包版是窗口子系统（无控制台窗口），若没有这个入口，用户只能去任务管理器
    结束进程；而且本地服务会一直占着端口。这里先返回 202 让响应发出去，
    再由后台线程通知 uvicorn 退出。
    """
    server = getattr(app.state, "server", None)
    if server is None:
        raise ApiError(503, "no_server",
                       "当前运行方式下无法从接口退出（例如由外部进程托管）")
    threading.Timer(0.3, lambda: setattr(server, "should_exit", True)).start()
    return {"ok": True, "detail": "正在退出…"}


# ---------------------------------------------------------------- 内置深度模型

def _model_dst(kind: str) -> str:
    """三种模型各自的落盘位置。

      depth  —— MiDaS-small（约 64MB，历史默认路径）
      dav2   —— Depth Anything V2 Small（约 94MB，ControlNet 深度预处理器同源）
      normal —— MoGe-2 法线（约 134MB，ControlNet 法线预处理器的等价替代）

    显式配置过的路径优先（尊重用户选择），否则用标准文件名。dav2 不读
    builtin_model_path：那条配置指向的是「当前在用的深度模型」，覆盖它有
    可能毁掉用户自己放进去的文件。
    """
    from core import preprocess as pp
    from core.builtin_depth import default_model_path
    if kind == "depth":
        return _config.builtin_model_path or default_model_path()
    if kind == "dav2":
        return pp.depth_model_path()
    if kind == "normal":
        return _config.builtin_normal_model_path or pp.normal_model_path()
    raise ApiError(400, "bad_kind", "kind 只能是 depth / dav2 / normal")


def _validate_import(path: str, kind: str) -> None:
    """校验导入的文件确实可用、且与所选类型匹配。

    只判「能加载」不够：MoGe 法线模型和深度模型都能加载，但用途不同，
    误放到另一栏会出现「导入成功、一用就失效」。
    """
    from core.builtin_depth import _get_session, validate_model
    from core.preprocess import model_family
    validate_model(path)
    family = model_family(_get_session(path))
    if kind == "normal" and family != "moge_normal":
        raise RuntimeError("模型没有 normal 输出，不是法线模型")
    if kind == "dav2" and family != "dav2":
        raise RuntimeError("模型不是 Depth Anything V2 家族")
    if kind == "depth" and family == "moge_normal":
        raise RuntimeError("这是法线模型，不能用作深度模型")


# 模型下载进度，随 GET /api/model/progress 返回。
#
# 下载是**同步长请求**（64/94/134MB，网络受限时可能好几分钟），期间界面
# 没有任何反馈，用户只能干等或以为卡死。core.builtin_depth.download_file
# 早就有 progress 回调，这里把它转成可轮询的状态。
#
# 只做一个槽位：界面上按钮在下载期间是禁用的，正常不会并发；真并发了
# 也是后来者覆盖，不会算错已完成字节数之外的任何东西。
_model_dl: Dict[str, Any] = {"active": False, "kind": "", "done": 0, "total": 0,
                             "percent": 0.0}


def _dl_progress(kind: str):
    """把 download_file 的 progress(done, total) 转成 _model_dl 状态。

    total 来自响应的 Content-Length，可能为 0（分块传输）：此时 percent
    保持 0，界面按「已接收 N MB」显示，不能拿 done 冒充总量去算百分比。
    """
    def cb(done: int, total: int) -> None:
        _model_dl.update(active=True, kind=kind, done=done, total=total,
                         percent=round(done * 100.0 / total, 1) if total else 0.0)
    return cb


@app.get("/api/model/progress")
def api_model_progress() -> dict:
    """当前模型下载进度（设置弹窗据此画进度条；无下载时 active=False）。"""
    return dict(_model_dl)


@app.post("/api/model/download")
def api_model_download(kind: str = "depth") -> dict:
    """下载内置模型（幂等；已存在则直接返回）。

    kind=depth（默认）/ dav2 / normal，见 _model_dst。
    下载源可用环境变量 HLS_MODEL_URL（MiDaS）、HLS_DEPTH_MODEL_URL（DAV2）、
    HLS_NORMAL_MODEL_URL（MoGe）指向镜像 —— GitHub / huggingface 在国内
    常不可达，默认源已分别指向可用镜像。
    """
    from core import preprocess as pp
    from core.builtin_depth import (clear_session_cache, download_model,
                                    is_model_ready, model_url)
    dst = _model_dst(kind)
    if is_model_ready(dst):
        return {"ok": True, "already": True, "kind": kind, "path": dst,
                "message": "模型已存在，无需重复下载"}
    if kind == "depth":
        download, source = download_model, model_url()
    elif kind == "dav2":
        download, source = pp.download_depth_model, pp.depth_model_url()
    else:
        download, source = pp.download_normal_model, pp.normal_model_url()
    _model_dl.update(active=True, kind=kind, done=0, total=0, percent=0.0)
    try:
        path = download(dst, progress=_dl_progress(kind))
    except Exception as e:
        _model_dl.update(active=False)
        raise ApiError(502, "download_failed",
                       f"模型下载失败：{e}\n可用环境变量 HLS_MODEL_URL / "
                       f"HLS_DEPTH_MODEL_URL / HLS_NORMAL_MODEL_URL 指向镜像，"
                       f"或手动下载后使用「离线导入模型…」")
    size = os.path.getsize(path)
    _model_dl.update(active=False, done=size, total=size, percent=100.0)
    clear_session_cache(path)   # 文件已替换，旧会话必须失效
    return {"ok": True, "already": False, "kind": kind, "path": path,
            "size_mb": round(size / 1e6, 1),
            "source": source, "message": "模型下载完成"}


@app.post("/api/model/import")
async def api_model_import(request: Request, kind: str = "depth") -> dict:
    """离线导入模型：接收 .onnx 文件字节，落盘到模型目录并生效。

    kind=depth（默认）/ dav2 / normal，决定落盘位置与校验方式。
    """
    from core.builtin_depth import clear_session_cache, is_model_ready
    data = await request.body()
    if not data:
        raise ApiError(400, "empty_body", "请求体为空：请提交 .onnx 文件字节")
    if len(data) < 1024:
        raise ApiError(400, "bad_model", "文件过小，不像是 ONNX 模型")
    # ONNX 是 protobuf：用文件内的魔数/关键字做一次粗校验，避免误传其它文件
    if b"onnx" not in data[:4096].lower() and b"\x08" not in data[:4]:
        raise ApiError(400, "bad_model",
                       "这不像是 ONNX 模型文件（未找到 ONNX 标识）")
    dst = _model_dst(kind)
    # 临时文件名必须**每次唯一**：校验已移入线程，事件循环在校验期间是空闲的，
    # 因此两个并发导入会同时在写。若共用固定名 dst+".importing"，先完成者
    # os.replace 把文件移走后，后者会读到半截文件（误报「不是可用的 ONNX 模型」）
    # 或 replace 抛 FileNotFoundError（500）。用 mkstemp 保证互不干扰。
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), suffix=".importing")
    except OSError as e:
        raise ApiError(500, "write_failed", f"无法在模型目录创建临时文件：{e}")
    try:
        # 64MB 级的写盘同样是阻塞活，一并放进线程，避免占着事件循环
        def _write() -> None:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        await asyncio.to_thread(_write)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ApiError(500, "write_failed", f"写入模型文件失败：{e}")

    # 校验临时会话必须立刻丢弃，否则它会一直占着模型权重内存
    # （校验在临时路径上建过会话，而临时文件随后被 rename 掉）。
    #
    # 必须放入线程执行：validate_model 会构建 ONNX 推理会话（加载 64~134MB 权重，
    # 数百毫秒到数秒），而本端点是 async def —— 直接在事件循环里跑会**阻塞整个服务**，
    # 期间 /api/state、/api/theme、视频进度轮询全部停摆，违背「界面不卡死」的要求。
    try:
        await asyncio.to_thread(_validate_import, tmp, kind)
    except Exception as e:
        clear_session_cache(tmp)
        try:
            os.remove(tmp)
        except OSError:
            pass
        # 不把原始运行时异常直接暴露给用户：里面含内部临时路径（用户从未选择过
        # .importing 这个文件）与用户名，且长文本会被截断。原始异常写入服务端日志，
        # 前端只给可读的中文说明（保留有意义的错误码）。
        log.warning("模型导入校验失败（%s，kind=%s）：%s", tmp, kind, e)
        code = ""
        m = re.search(r"\b([A-Z_]{6,})\b", str(e))
        if m:
            code = f"（{m.group(1)}）"
        if kind == "normal":
            raise ApiError(400, "bad_model",
                           f"该文件不是可用的 ONNX 法线模型{code}。"
                           f"请选择 MoGe-2 法线模型的 .onnx 文件，"
                           f"或用「一键下载模型」自动获取。")
        raise ApiError(400, "bad_model",
                       f"该文件不是可用的 ONNX 深度模型{code}。"
                       f"请选择 MiDaS-small / Depth Anything V2 的 .onnx 文件，"
                       f"或用「一键下载模型」自动获取。")
    clear_session_cache(tmp)

    try:
        os.replace(tmp, dst)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ApiError(500, "write_failed", f"替换模型文件失败：{e}")

    # 就位后清掉目标路径的旧会话（文件已替换，旧会话必须失效）
    clear_session_cache(dst)
    return {"ok": True, "kind": kind, "path": dst,
            "size_mb": round(len(data) / 1e6, 1),
            "message": "模型已导入并生效"}


# ---------------------------------------------------------------- 静态前端

@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH",
                                            "DELETE", "OPTIONS"],
               include_in_schema=False)
def api_not_found(rest: str) -> JSONResponse:
    return JSONResponse({"error": "not_found", "detail": f"未知接口 /api/{rest}"},
                        status_code=404)


_FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>凌日光影棚 · 前端未构建</title>
<style>
 body{background:#14161c;color:#e8eaf0;font-family:system-ui,"Microsoft YaHei",sans-serif;
      margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
 main{max-width:640px;padding:40px}
 h1{font-size:20px;margin:0 0 12px}
 code{background:#20242e;padding:2px 6px;border-radius:4px;color:#ffd479}
 pre{background:#20242e;padding:14px 16px;border-radius:8px;overflow:auto;line-height:1.6}
 p{color:#a9b0c0;line-height:1.7}
</style></head><body><main>
<h1>前端尚未构建</h1>
<p>后端已就绪，但 <code>webui/frontend/dist</code> 不存在。请先在仓库根目录执行构建
（Node 仅用于构建，运行期不需要）：</p>
<pre>cd webui/frontend
npm install
npm run build</pre>
<p>构建完成后刷新本页即可。</p>
</main></body></html>
"""


class _SPAStaticFiles(StaticFiles):
    """dist 静态资源 + 未知路径回落到 index.html（前端路由）。"""

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


if os.path.isfile(_INDEX_FILE):
    app.mount("/", _SPAStaticFiles(directory=_DIST_DIR, html=True),
              name="webui")
else:
    log.warning("未找到 %s，将返回未构建提示页（请先构建前端）", _INDEX_FILE)

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def spa_fallback(full_path: str) -> HTMLResponse:
        return HTMLResponse(_FALLBACK_HTML, status_code=200)


# ---------------------------------------------------------------- 入口

def main(argv: Optional[List[str]] = None) -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m webui.api",
                                     description="凌日光影棚 WebUI 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
