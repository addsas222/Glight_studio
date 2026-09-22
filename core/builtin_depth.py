# -*- coding: utf-8 -*-
"""软件内置本地深度引擎：ONNX Runtime + MiDaS-small / Depth Anything V2。

- 零部署：随软件捆绑 onnxruntime 运行时；模型文件可一键下载或手动离线导入。
- 完全离线；未安装 onnxruntime 或模型缺失时抛出异常，由后端链自动降级。
- 支持多模型家族，按 ONNX 签名自动识别（见 preprocess.model_family）：
    MiDaS-small（约 64MB，历史默认）与 Depth Anything V2 Small（约 94MB，
    即 ControlNet 的深度预处理器）。两者输出都是视差，值越大越近，
    与本项目“白=近”约定一致。
- 本模块只保留**通用骨架**（下载 / 校验 / 会话缓存 / MiDaS 推理）；
  各家族的预处理差异在 core/preprocess.py。
"""
from __future__ import annotations

import os
import tempfile
import threading
import urllib.request

import numpy as np
from PIL import Image

from .paths import subdir

MIDAS_SMALL_URL = (
    "https://github.com/isl-org/MiDaS/releases/download/v2_1/"
    "model-small.onnx")


def model_url() -> str:
    """模型下载地址，可用环境变量 HLS_MODEL_URL 覆盖。

    默认地址在 GitHub Releases；国内网络经常连不上（curl/urllib 直接超时），
    因此允许指向任意可达镜像。也可完全跳过下载：手动把 model-small.onnx
    放进 ~/.horizon_light_studio/models/ 即可。
    """
    return os.environ.get("HLS_MODEL_URL", "").strip() or MIDAS_SMALL_URL


MODEL_FILENAME = "model-small.onnx"
MODELS_DIR_DEFAULT = subdir("models")

# 仅当模型声明的是动态尺寸时使用的兜底分辨率
_FALLBACK_INPUT = 384
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)

_session_cache = {}
# 按模型路径分锁：加载 A（约 1.9s，上百 MB）不该挡住 B。同一路径只允许一个
# 线程真正构建会话，其余线程在锁上等，醒来后复用已建好的会话。
_session_locks = {}
_session_locks_guard = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(path)
        if lock is None:
            lock = _session_locks[path] = threading.Lock()
        return lock


def default_model_path() -> str:
    return os.path.join(MODELS_DIR_DEFAULT, MODEL_FILENAME)


def is_model_ready(model_path: str = "") -> bool:
    p = model_path or default_model_path()
    return os.path.isfile(p) and os.path.getsize(p) > 1_000_000


def download_file(url: str, dst: str, progress=None, timeout: float = 30.0,
                  min_bytes: int = 1_000_000) -> str:
    """把 url 分块流式下载到 dst（临时文件唯一 + 超时 + 失败清理）。

    所有模型（MiDaS / Depth Anything / MoGe）共用这一份实现：
      - 临时文件名必须**每次唯一**：本函数既被 .bat/.sh 直接调用，也在
        FastAPI 线程池里并发调用（端点无需 async）。固定名 dst+".part"
        会让两个并发下载同时写同一文件，先完成者 os.replace 移走后，
        后者会抛 FileNotFoundError。
      - 必须带超时：模型动辄上百 MB，GitHub/HF 在目标网络里经常传到一半
        停滞；没有超时会把请求永久挂住（界面只能重启）。
      - 中断抛异常时清掉半成品，避免下次被 is_model_ready 误判为「已就绪」。
        但临时名唯一（并发安全所需），所以进程被**硬杀**留下的 `.part`
        孤儿要靠启动清理回收（见 webui/api.py 的 _cleanup_model_dir）。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), suffix=".part")
    try:
        req = urllib.request.Request(url)
        # 先接管 fd 再发请求：若 urlopen 抛错，with 仍会关闭 fd
        # （反之 fd 会泄漏，并在 Windows 上让 os.remove 失败留下垃圾文件）。
        with os.fdopen(fd, "wb") as f:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        size = os.path.getsize(tmp)
        if size < min_bytes:
            raise RuntimeError("下载内容过小，疑似失败的响应")
        if total and size != total:
            # 服务器提前断流时 http.client 未必抛错，这里补一道校验，
            # 否则 134MB 的模型可能只写了一半却被认作完成。
            raise RuntimeError(f"下载不完整：{size}/{total} 字节")
        os.replace(tmp, dst)
    except Exception:
        # 失败时清掉半成品，避免下次被误判为「已就绪」
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def download_model(model_path: str = "", progress=None,
                   timeout: float = 30.0) -> str:
    """下载 MiDaS-small ONNX 模型（也可由用户手动放入该路径）。

    已存在且完整时**直接返回，不重复下载**：模型约 64MB，
    部署脚本每次运行都重下既慢，又会在网络受限的机器上直接失败
    （即使模型早就准备好了）。
    """
    dst = model_path or default_model_path()
    if is_model_ready(dst):
        return dst
    if os.path.isfile(dst):
        # 文件存在但明显不完整（例如上次下载中断留下的残file）→ 重下
        try:
            os.remove(dst)
        except OSError:
            pass
    return download_file(model_url(), dst, progress, timeout, 1_000_000)


def _file_stamp(path: str) -> tuple:
    """文件身份标记（大小 + 修改时间），用于让会话缓存自动失效。

    只按路径缓存会话是不够的：用户导入/重新下载模型时会**就地替换**同名文件，
    旧的 InferenceSession 会一直被复用，界面显示「模型已导入」但引擎仍在用旧模型
    （直到重启进程）。把大小与 mtime 纳入键，替换后自然重建。
    """
    try:
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime_ns))
    except OSError:
        return (0, 0)


def _get_session(model_path: str = ""):
    """取（必要时构建）ONNX 推理会话。多线程安全。

    双层检查 + 按路径加锁：启动预热线程与首个请求可能同时索要同一个模型，
    没有锁时两边都会看到空缓存、各自加载一遍（94MB / 134MB 各读两次，
    白白多花约 2s 与一倍的瞬时内存）。加锁后只有先到的那个真正构建。
    锁内**重新**取一次 stamp：等锁期间文件可能被导入替换，必须按最新身份
    建键，否则会把新文件的会话挂在旧键上、之后一直复用错模型。
    """
    import onnxruntime as ort  # 延迟导入：未安装时让后端链降级
    path = model_path or default_model_path()
    key = (path, _file_stamp(path))
    hit = _session_cache.get(key)
    if hit is not None:
        return hit                      # 快路径：命中就不进锁，推理线程不排队
    with _path_lock(path):
        key = (path, _file_stamp(path))         # 等锁期间文件可能已被替换
        hit = _session_cache.get(key)
        if hit is not None:
            return hit
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        try:
            if "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, "CUDAExecutionProvider")
        except Exception:
            pass
        sess = ort.InferenceSession(path, so, providers=providers)
        # 同一路径的旧条目已失效（文件被替换），清掉避免无限增长
        for k in [k for k in _session_cache if k[0] == path]:
            _session_cache.pop(k, None)
        _session_cache[key] = sess
        return sess


def session_cached(model_path: str = "") -> bool:
    """该模型是否已有可复用的会话 —— **不触发加载**。

    /api/state 是前端首屏的热路径，而真正的就绪判断必须加载一次会话
    （约 1.9s）。启动预热线程正在做同一件事，首屏若也去加载，两者只会
    互相等待、白等一遍。首屏先用这里做快速判断，预热完成后答案自然精确。
    """
    path = model_path or default_model_path()
    return (path, _file_stamp(path)) in _session_cache


def clear_session_cache(model_path: str = "") -> None:
    """丢弃指定模型（或全部）的 ONNX 会话缓存。"""
    if not model_path:
        _session_cache.clear()
        return
    for k in [k for k in _session_cache if k[0] == model_path]:
        _session_cache.pop(k, None)


def validate_model(path: str) -> str:
    """真正校验模型可用：尝试加载推理会话。

    仅按文件大小判断（is_model_ready）无法识别「大于 1MB 但不是合法 ONNX」
    的文件，会让界面误报导入成功、之后推理时才静默降级。这里实际加载一次，
    失败时抛异常（由调用方决定是否删除该文件）。

    **不要**在这里清缓存：会话按 (路径, 大小+mtime) 缓存，文件被替换时
    自动得到新键、自动重建，本就无需手工失效。而且本函数是 /api/state 的
    热路径（每次轮询都会走），清缓存会让 66MB 模型每次都被重新加载，
    把毫秒级的接口拖到 200ms 以上。
    """
    _get_session(path)                 # 加载失败会抛 ort 异常
    return path


def _input_spec(sess) -> tuple:
    """探测模型的输入名与输入分辨率。

    官方 MiDaS-small.onnx 导出的输入名为 "0"、尺寸为 256x256；不同导出
    版本可能是 "input" / 384x384 或动态尺寸。因此必须按模型声明取值，
    不能硬编码，否则会抛 "Required inputs are missing from input feed"。
    """
    inp = sess.get_inputs()[0]
    name = inp.name
    shape = list(inp.shape)
    h = shape[2] if len(shape) > 2 and isinstance(shape[2], int) and shape[2] > 0 \
        else _FALLBACK_INPUT
    w = shape[3] if len(shape) > 3 and isinstance(shape[3], int) and shape[3] > 0 \
        else h
    return name, int(h), int(w)


def _midas_depth(sess, rgb: np.ndarray) -> np.ndarray:
    """MiDaS 家族推理：固定/动态输入尺寸 + ImageNet 归一化。"""
    in_name, in_h, in_w = _input_spec(sess)

    h, w = rgb.shape[:2]
    img = Image.fromarray(rgb).resize((in_w, in_h), Image.BICUBIC)
    x = np.asarray(img, np.float32) / 255.0
    x = (x - _IMAGENET_MEAN) / _IMAGENET_STD
    x = x.transpose(2, 0, 1)[None]  # 1x3xHxW

    out = np.asarray(sess.run(None, {in_name: x})[0], np.float32)
    disp = out.reshape(out.shape[-2], out.shape[-1])

    lo, hi = np.percentile(disp, 1), np.percentile(disp, 99)
    disp = np.clip((disp - lo) / max(hi - lo, 1e-6), 0, 1)
    d = np.asarray(Image.fromarray((disp * 255).astype(np.uint8)).resize(
        (w, h), Image.BICUBIC), np.float32) / 255.0
    return d


def builtin_depth(rgb: np.ndarray, model_path: str = "",
                  timeout: float = 3.0, log=None) -> np.ndarray:
    """主入口：返回 0~1 深度图（大=近）。任何失败抛异常触发降级。

    按 ONNX 会话签名自动识别模型家族（MiDaS-small / Depth Anything V2），
    用户只需替换模型文件、无需改配置或代码；分派逻辑在 preprocess.depth_map。
    """
    # 延迟导入：preprocess 在模块层反向依赖本模块的通用骨架
    from .preprocess import depth_map, preferred_depth_model
    path = preferred_depth_model(model_path)
    if not is_model_ready(path):
        raise RuntimeError("内置模型未就绪（请到 设置→AI后端→模型管理 下载或导入）")
    return depth_map(rgb, path, log)
