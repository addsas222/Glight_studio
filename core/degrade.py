# -*- coding: utf-8 -*-
"""失败降级：无 AI 后端时用图像灰度梯度模拟粗糙深度图（完全离线）。"""
from __future__ import annotations

import numpy as np
from PIL import Image


def simulate_depth_from_luminance(rgb: np.ndarray) -> np.ndarray:
    """由亮度+局部对比度模拟一个粗糙深度图（0~1，越大越近）。

    策略（动漫立绘友好）：
    - 亮度高（受光、皮肤、高光区域）视为更靠近观察者；
    - 边缘处（灰度梯度大）做轻度平滑避免锯齿深度噪声。
    仅用于“仅预览模式”，质量有限属预期行为。
    """
    img = rgb.astype(np.float32)
    if img.ndim == 3:
        lum = img @ np.array([0.299, 0.587, 0.114], np.float32)
    else:
        lum = img.astype(np.float32)

    # 归一化亮度
    lo, hi = np.percentile(lum, 2), np.percentile(lum, 98)
    lum_n = np.clip((lum - lo) / max(hi - lo, 1e-3), 0, 1)

    # 轻微平滑（分离盒式模糊 5x5）
    depth = _box_blur(lum_n, 5)
    # 再做一次较大尺度平滑使过渡柔和
    depth = 0.65 * depth + 0.35 * _box_blur(depth, 17)
    return np.clip(depth, 0, 1).astype(np.float32)


def _box_blur(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return a
    r = k // 2
    p = np.pad(a, r, mode="edge")
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = a.shape
    s = c[k:k + h, k:k + w] - c[0:h, k:k + w] - c[k:k + h, 0:w] + c[0:h, 0:w]
    return (s / (k * k)).astype(np.float32)
