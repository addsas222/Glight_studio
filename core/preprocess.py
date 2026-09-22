# -*- coding: utf-8 -*-
"""ControlNet 风格预处理模块：深度图 + 法线图（ONNX Runtime，完全离线）。

对应 ControlNet 官方预处理器（controlnet_aux）：
  depth  —— Depth Anything V2 Small，与 DepthAnythingV2Detector 同源权重
  normal —— MoGe-2 ViT-S 法线分支。NormalBaeDetector / DSineDetector 只有
            PyTorch 权重、至今没有可直接用的 ONNX 导出，故以 MoGe-2 作为
            等价替代（输出的切空间法线与 BAE 语义一致）。

与内置 MiDaS 引擎的分工：
  core/builtin_depth.py  负责「下载 / 校验 / 会话缓存 / 通用 ONNX 骨架」
  本模块                 只负责各模型家族的**预处理与后处理差异**
两者共用同一份模型目录（~/.horizon_light_studio/models）与会话缓存。

以下常量均为**实测确认**，勿凭直觉改动：
  - Depth Anything V2 的归一化与 MiDaS 完全相同（ImageNet mean/std），
    差别只在输入尺寸（短边 518、且必须被 14 整除 —— ViT patch 大小）。
  - DAV2 输出为视差（disparity），**大 = 近**，与 render.py 的「白=近」
    约定一致，无需取反（实测：主体 4.59 > 头部 3.06 > 背景 2.55）。
  - MoGe-2 输出在 OpenCV 相机系（+z 指向场景内部，实测 z 均值 -0.9476），
    必须取反 z 才满足 render.py 的「+z 朝向观察者」约定。
  - MoGe-2 只接受正方形输入：非方图必须**黑边补方**再裁回内容区，
    直接拉伸会连带旋转法线方向（形状失真会污染几何）。
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image

from .builtin_depth import (
    _IMAGENET_MEAN,
    _IMAGENET_STD,
    MODELS_DIR_DEFAULT,
    _get_session,
    _midas_depth,
    default_model_path,
    download_file,
    is_model_ready,
)

DEPTH_MODEL_FILENAME = "depth-anything-v2-small.onnx"
NORMAL_MODEL_FILENAME = "moge2-vits-normal.onnx"

# 默认走 hf-mirror 镜像：huggingface.co / github.com 在目标网络常常不可达，
# 而镜像可达且内容一致。可用 HLS_DEPTH_MODEL_URL / HLS_NORMAL_MODEL_URL 覆盖。
DEPTH_MODEL_URL_DEFAULT = (
    "https://hf-mirror.com/onnx-community/depth-anything-v2-small/"
    "resolve/main/onnx/model.onnx")
NORMAL_MODEL_URL_DEFAULT = (
    "https://hf-mirror.com/Ruicheng/moge-2-vits-normal-onnx/"
    "resolve/main/model.onnx")

# DAV2 输入：短边 518，长边上限 1036，且两侧都必须是 14 的整数倍
_DAV2_SHORT_SIDE = 518
_DAV2_MAX_SIDE = 1036
_PATCH = 14

# MoGe-2 输入：正方形边长（其 num_tokens 必须等于 (边长/14)^2）
_MOGE_SIDE = 518

_MIN_MODEL_BYTES = 1_000_000


def depth_model_url() -> str:
    return os.environ.get("HLS_DEPTH_MODEL_URL", "").strip() or DEPTH_MODEL_URL_DEFAULT


def normal_model_url() -> str:
    return os.environ.get("HLS_NORMAL_MODEL_URL", "").strip() or NORMAL_MODEL_URL_DEFAULT


def depth_model_path() -> str:
    return os.path.join(MODELS_DIR_DEFAULT, DEPTH_MODEL_FILENAME)


def normal_model_path() -> str:
    return os.path.join(MODELS_DIR_DEFAULT, NORMAL_MODEL_FILENAME)


def preferred_depth_model(model_path: str = "") -> str:
    """实际要用的深度模型路径：显式配置 > 已部署的 DAV2 > 默认 MiDaS 路径。

    两种模型都合法（DAV2 质量更好，就是 ControlNet 的深度预处理器；MiDaS
    体积更小），写死任一个都会让另一种部署「装好了却不被使用」。
    注意这里只做**路径选择**，不判断文件是否可用 —— 调用方仍需 is_model_ready。
    """
    if model_path:
        return model_path
    dav2 = depth_model_path()
    return dav2 if is_model_ready(dav2) else default_model_path()


def download_depth_model(path: str = "", progress=None,
                         timeout: float = 30.0) -> str:
    """下载 Depth Anything V2 Small（约 94MB）；已就绪则不联网直接返回。"""
    dst = path or depth_model_path()
    if is_model_ready(dst):
        return dst
    return download_file(depth_model_url(), dst, progress, timeout,
                         _MIN_MODEL_BYTES)


def download_normal_model(path: str = "", progress=None,
                          timeout: float = 30.0) -> str:
    """下载 MoGe-2 法线模型（约 134MB）；已就绪则不联网直接返回。"""
    dst = path or normal_model_path()
    if is_model_ready(dst):
        return dst
    return download_file(normal_model_url(), dst, progress, timeout,
                         _MIN_MODEL_BYTES)


def model_family(sess) -> str:
    """按 ONNX 会话声明识别模型家族，让用户换文件即可换引擎。

    判据来自各自导出图的签名，不看文件名（用户重命名文件很常见）：
      - 输入含 "pixel_values" 或空间维动态 → dav2（HuggingFace 导出风格）
      - 有多个输入（含 0 维标量 num_tokens）→ moge_normal
      - 其余（单输入、固定或动态尺寸）→ midas
    """
    inputs = list(sess.get_inputs())
    names = {i.name for i in inputs}
    if "pixel_values" in names:
        return "dav2"
    if len(inputs) > 1 or any(len(i.shape) == 0 for i in inputs):
        return "moge_normal"
    shape = list(inputs[0].shape)
    if len(shape) > 2 and not isinstance(shape[2], int):
        return "dav2"
    return "midas"


def _normalize(x: np.ndarray) -> np.ndarray:
    return (x - _IMAGENET_MEAN) / _IMAGENET_STD


def _resize_field(a: np.ndarray, hw) -> np.ndarray:
    """单通道场（0~1）缩放：走 uint8/BICUBIC，与 depth 后处理保持一致。"""
    if a.shape[0] == hw[0] and a.shape[1] == hw[1]:
        return a
    return np.asarray(Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
                      .resize((hw[1], hw[0]), Image.BICUBIC),
                      np.float32) / 255.0


def _resize_normal(n: np.ndarray, hw) -> np.ndarray:
    """法线缩放：编码为 0~1 再插值，最后按行重新单位化。

    直接对 -1~1 分量插值会在零交叉处失真；编码后插值等价于对方向做
    加权平均，是 render.py / ai_backend.py 一贯的做法。
    """
    if n.shape[0] != hw[0] or n.shape[1] != hw[1]:
        enc = ((np.clip(n, -1, 1) * 0.5 + 0.5) * 255).astype(np.uint8)
        n = np.asarray(Image.fromarray(enc).resize(
            (hw[1], hw[0]), Image.BILINEAR), np.float32) / 255.0 * 2.0 - 1.0
    norm = np.linalg.norm(n, axis=-1, keepdims=True)
    return (n / np.maximum(norm, 1e-6)).astype(np.float32)


def normalize_depth(disp: np.ndarray, hw) -> np.ndarray:
    """视差/深度 → 0~1（大=近），按 1/99 分位裁剪后缩放到目标尺寸。"""
    d = disp.reshape(disp.shape[-2], disp.shape[-1])
    lo, hi = np.percentile(d, 1), np.percentile(d, 99)
    d = np.clip((d - lo) / max(float(hi - lo), 1e-6), 0, 1)
    return _resize_field(d, hw)


def _dav2_input_size(h: int, w: int) -> tuple:
    """DAV2 的目标输入尺寸：短边 518、长边 ≤1036，且两侧均为 14 的倍数。

    必须保持长宽比（keep_aspect_ratio）—— DAV2 的位置编码按 patch 网格
    组织，强行拉成正方形会让深度图出现整体倾斜。
    """
    scale = _DAV2_SHORT_SIDE / max(min(h, w), 1)
    if max(h, w) * scale > _DAV2_MAX_SIDE:
        scale = _DAV2_MAX_SIDE / max(max(h, w), 1)
    nh = max(_PATCH, int(round(h * scale)) // _PATCH * _PATCH)
    nw = max(_PATCH, int(round(w * scale)) // _PATCH * _PATCH)
    return nh, nw


def dav2_depth(rgb: np.ndarray, model_path: str = "", log=None,
               sess=None) -> np.ndarray:
    """Depth Anything V2 → 0~1 深度图（大=近）。

    预处理与 controlnet_aux 的 DepthAnythingV2Detector 一致：
    长宽比等比缩放到 14 的倍数 → /255 → ImageNet 归一化 → NCHW。
    """
    if sess is None:
        sess = _get_session(model_path or depth_model_path())
    h, w = rgb.shape[:2]
    nh, nw = _dav2_input_size(h, w)
    img = Image.fromarray(rgb).resize((nw, nh), Image.BICUBIC)
    x = _normalize(np.asarray(img, np.float32) / 255.0)
    x = x.transpose(2, 0, 1)[None]                      # 1x3xHxW
    in_name = sess.get_inputs()[0].name
    out = np.asarray(sess.run(None, {in_name: x})[0], np.float32)
    return normalize_depth(out[0], (h, w))


def moge_normal(rgb: np.ndarray, model_path: str = "", log=None,
                sess=None) -> np.ndarray:
    """MoGe-2 法线分支 → HxWx3 单位法线（-1~1，+z 朝向观察者）。

    两处不可省略的处理（均已实测验证）：
      1. 黑边补成正方形再裁回内容区。模型只吃方图，拉伸会让法线整体偏转。
      2. z 分量取反。MoGe 输出在 OpenCV 相机系（+z 指向场景内部），
         与 render.py 的约定相反；x/y 与图像轴一致，无需处理。
    """
    if sess is None:
        sess = _get_session(model_path or normal_model_path())
    h, w = rgb.shape[:2]
    side = _moge_side(sess)

    s = max(h, w)
    canvas = np.zeros((s, s, 3), np.uint8)
    y0, x0 = (s - h) // 2, (s - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = rgb
    img = Image.fromarray(canvas).resize((side, side), Image.BICUBIC)
    x = _normalize(np.asarray(img, np.float32) / 255.0)
    x = x.transpose(2, 0, 1)[None]

    inputs = list(sess.get_inputs())
    feed = {inputs[0].name: x}
    for inp in inputs[1:]:
        if "token" in inp.name.lower():
            feed[inp.name] = np.array(
                (side // _PATCH) * (side // _PATCH), np.int64)
    outs = sess.run(None, feed)
    names = [o.name for o in sess.get_outputs()]
    if "normal" not in names:
        raise RuntimeError("该模型没有 normal 输出，不是 MoGe 法线模型")
    n = np.asarray(outs[names.index("normal")], np.float32)
    n = n.reshape(-1, *n.shape[-3:])[0]
    if n.shape[-1] != 3:
        if n.shape[0] != 3:
            raise RuntimeError(f"法线输出形状异常：{n.shape}")
        n = n.transpose(1, 2, 0)
    n = n * np.array([1.0, 1.0, -1.0], np.float32)     # OpenCV 系 → +z 朝观察者
    n = _resize_normal(n, (s, s))[y0:y0 + h, x0:x0 + w]
    return _resize_normal(n, (h, w))


def _moge_side(sess) -> int:
    shape = list(sess.get_inputs()[0].shape)
    if len(shape) > 3 and isinstance(shape[2], int) and shape[2] > 0:
        return int(shape[2])
    return _MOGE_SIDE


def depth_map(rgb: np.ndarray, model_path: str = "", log=None,
              sess=None) -> np.ndarray:
    """按模型家族选择深度推理实现，返回 0~1 深度图（大=近）。

    软件内置链与用户自建服务都走这里，保证两边同源同参。
    """
    path = preferred_depth_model(model_path)
    if not is_model_ready(path):
        raise RuntimeError("深度模型未就绪：文件不存在或小于 1MB")
    if sess is None:
        sess = _get_session(path)
    family = model_family(sess)
    if family == "dav2":
        return dav2_depth(rgb, path, log, sess)
    if family == "moge_normal":
        raise RuntimeError("所选文件是法线模型，不是深度模型；请改选深度模型")
    return _midas_depth(sess, rgb)


def analyze(rgb: np.ndarray, depth_model: str = "", normal_model: str = "",
            timeout: float = 3.0, log=None) -> dict:
    """一次产出深度 + 法线，供内置后端链与自建服务共用。

    返回 {'depth': HxW 0~1（大=近）, 'normal': HxWx3 -1~1 或 None}。

    深度模型缺失/失败时抛异常（调用方负责降级）；法线模型缺失或推理失败时
    只记日志并返回 normal=None —— 上层会用 depth_to_height/height_to_normal
    本地派生，属于既有的合法降级路径，不该让整条后端链失败。
    """
    log = log or (lambda msg: None)
    depth = depth_map(rgb, depth_model, log)
    path = normal_model or normal_model_path()
    if not is_model_ready(path):
        return {"depth": depth, "normal": None}
    try:
        return {"depth": depth, "normal": moge_normal(rgb, path, log)}
    except Exception as e:
        log(f"法线模型推理失败，改用深度几何派生：{e}")
        return {"depth": depth, "normal": None}
