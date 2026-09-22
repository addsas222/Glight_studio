# -*- coding: utf-8 -*-
"""保守参考图迁移：从参考图提取主光方向与全局色温，作为限幅偏移量
叠加到当前光源参数上。保留原图固有明暗结构，仅做柔和微调，禁止暴力重绘。"""
from __future__ import annotations

from typing import Tuple

import numpy as np

from .types import Light, RenderParams

# 安全限幅
MAX_ANGLE_OFFSET_DEG = 15.0    # 方向偏移上限
MAX_KELVIN_OFFSET = 500.0      # 色温偏移上限
MAX_INTENSITY_OFFSET = 0.25    # 强度偏移上限


def analyze_reference(ref_rgb: np.ndarray) -> dict:
    """提取参考图光照特征。

    返回 {'light_dx','light_dy','kelvin','brightness'}：
    - 主光方向：亮度梯度场按幅度加权平均，指向光源（亮侧）；
    - 全局色温：由 R/B 比值估计开尔文。
    """
    img = ref_rgb.astype(np.float32) / 255.0
    if img.ndim == 2:
        img = np.stack([img] * 3, -1)
    lum = img @ np.array([0.299, 0.587, 0.114], np.float32)

    # Sobel 梯度
    gy, gx = np.gradient(_box3(lum))
    mag = np.sqrt(gx * gx + gy * gy)
    thr = np.percentile(mag, 75)
    m = mag > thr
    if m.sum() < 16:
        m = mag > 0
    # 光从亮侧来：梯度指向变亮方向（gx,gy）即指向光源
    dx = float(np.mean(gx[m]))
    dy = float(np.mean(gy[m]))
    n = np.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n

    kelvin = _estimate_kelvin(img)
    return {"light_dx": dx, "light_dy": dy, "kelvin": kelvin,
            "brightness": float(np.mean(lum))}


def _box3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
            p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
            p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def _estimate_kelvin(img: np.ndarray) -> float:
    r = float(np.mean(img[..., 0])) + 1e-4
    b = float(np.mean(img[..., 2])) + 1e-4
    ratio = r / b
    # 粗略映射：ratio≈0.7 → 9000K（偏蓝），≈1.0 → 6500K，≈1.5 → 3200K（偏暖）
    ratio = min(max(ratio, 0.5), 2.0)
    kelvin = 6500.0 / ratio ** 1.6
    return float(min(max(kelvin, 1800.0), 12000.0))


def apply_reference_offset(params: RenderParams, features: dict) -> RenderParams:
    """将参考图特征作为偏移量叠加到主光源与环境光上（限幅、保守）。

    仅调整主光源（第一个光源）的方向小角度、全局色温与环境光强度；
    其余光源不动，保留用户已有设置与原图明暗结构。
    """
    import copy
    p = copy.deepcopy(params)
    if not p.lights:
        return p

    main = p.lights[0]
    # 方向按**三维单位向量**处理：只对 xy 分量做二维旋转而固定 dz，
    # 会让真实的三维偏转角超过限幅值——例如水平分量被拉长、z 不变时，
    # 实际偏转可达 26°，而界面回报的却是 15°（既超限又报错数）。
    # 这里改为绕「当前方向 × 目标方向」轴做 Rodrigues 旋转，并把旋转角
    # 严格夹到 MAX_ANGLE_OFFSET_DEG，回报的也是真正施加的三维夹角。
    cur3 = np.array([main.dx, main.dy, main.dz], np.float64)
    nc = np.linalg.norm(cur3)
    cur3 = cur3 / nc if nc > 1e-6 else np.array([-0.5, -0.5, 0.7])
    if cur3[2] < 0:                     # 光源必须在朝向观察者的半球
        cur3 = np.array([cur3[0], cur3[1], abs(cur3[2])])
        cur3 = cur3 / np.linalg.norm(cur3)

    # 目标方向：参考图给出的是图像平面内的方向，补一个合理的 z 分量后归一化
    tgt2 = np.array([float(features["light_dx"]), float(features["light_dy"])], np.float64)
    nt = np.linalg.norm(tgt2)
    if nt > 1e-4:
        tgt3 = np.array([tgt2[0] / nt, tgt2[1] / nt, cur3[2]])
    else:
        tgt3 = cur3.copy()
    tgt3 = tgt3 / (np.linalg.norm(tgt3) or 1.0)

    # 三维夹角与旋转轴
    dot3 = float(np.clip(float(np.dot(cur3, tgt3)), -1.0, 1.0))
    angle3 = float(np.degrees(np.arccos(dot3)))
    axis = np.cross(cur3, tgt3)
    na = float(np.linalg.norm(axis))
    applied = min(angle3, MAX_ANGLE_OFFSET_DEG)
    if na > 1e-8 and applied > 1e-9:
        k = axis / na
        th = np.radians(applied)
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * (K @ K)
        new3 = R @ cur3
    else:
        new3 = cur3.copy()
    new3 = new3 / (np.linalg.norm(new3) or 1.0)
    main.dx, main.dy = float(new3[0]), float(new3[1])
    main.dz = float(max(new3[2], 0.02))

    # 色温偏移（限幅）
    k_off = min(max(features["kelvin"] - main.kelvin,
                    -MAX_KELVIN_OFFSET), MAX_KELVIN_OFFSET)
    main.kelvin = float(main.kelvin + k_off)

    # 亮度差 → 主光强度微调（限幅）
    b_off = min(max((features["brightness"] - 0.5) * 0.5,
                    -MAX_INTENSITY_OFFSET), MAX_INTENSITY_OFFSET)
    main.intensity = float(min(max(main.intensity + b_off, 0.2), 4.0))

    # 环境光色温同步微调
    p.ambient_kelvin = float(min(max(
        p.ambient_kelvin + k_off * 0.5, 2000), 12000))

    p.reference_offset = {"d_angle_deg": float(applied),
                          "d_kelvin": float(k_off),
                          "d_intensity": float(b_off)}
    return p
