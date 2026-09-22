# -*- coding: utf-8 -*-
"""智能拾光：从一张参考照片反推三点布光（主光 / 辅光 / 轮廓光 / 环境光）。

做法是纯图像分析（复用 core.reference 的亮度梯度思路）：
- 主光方向：亮度梯度幅值加权平均方向（指向画面亮侧）；
- 主光入射角：亮暗半区相对亮度差，越平越接近正面光；
- 辅光：主光反向一侧、更接近机位，强度由阴影下限亮度决定；
- 轮廓光：某条边缘带明显亮于紧邻的内侧带时才成立；
- 环境光：整体亮度下限与全局色温。

**诚实性声明**：本引擎无法从一张普通图片推导绝对光度学量。
照度（lx）、显色指数（CRI）、色偏差（Δuv）、闪烁频率、通道功率等
实验室测量值一律不输出；所有输出都是相对估计值，并附带 confidence
（估计可信度，0~1）与 basis（中文推断依据），notes 里给出局限性说明。

自检：``python -m core.harvest``（本模块在包内，须以模块方式运行）。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .presets import get_preset
from .reference import analyze_reference
from .types import Light, RenderParams

# 光源角色（中文）
ROLES = ("主光", "辅光", "轮廓光", "环境光")

# 与引擎一致的值域
_INTENSITY_MIN, _INTENSITY_MAX = 0.05, 4.0
_KELVIN_MIN, _KELVIN_MAX = 1500.0, 12000.0
_RADIUS_MIN, _RADIUS_MAX = 0.05, 1.5

# 统计阶段的最大像素数：超出则先做块均值降采样（内存与耗时上限）
_STAT_MAX_PX = 4_000_000

# 无法推导的物理量声明（notes 中必须出现）
DISCLAIMER = (
    "本引擎只做图像启发式推断：绝对光度值（照度 lx、CRI 显色指数、Δuv 色偏差）"
    "以及闪烁频率、通道亮度功率等实验室量无法由单张图片推导，因此本结果一律不予报告；"
    "下列 intensity / kelvin / radius 均为相对估计值，confidence 只表示推断强度，"
    "不代表仪器测量精度。"
)

_ROLE_NAME = {
    "主光": "主光（智能拾光）",
    "辅光": "辅光（智能拾光）",
    "轮廓光": "轮廓光（智能拾光）",
    "环境光": "环境光（整体填充）",
}


@dataclass
class HarvestedLight:
    """一个拾取到的光源及其推断依据。"""

    role: str
    light: Light
    confidence: float = 0.0            # 0~1，估计可信度
    basis: str = ""                    # 中文推断依据

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "light": self.light.to_dict(),
            "confidence": round(float(self.confidence), 3),
            "basis": self.basis,
        }


@dataclass
class HarvestResult:
    """智能拾光的完整结果。"""

    scene_type: str = ""                       # 中文场景判断
    ambient_kelvin: float = 0.0
    lights: List[HarvestedLight] = field(default_factory=list)
    key_to_fill_ratio: float = 0.0             # 主光/辅光强度比（由图像统计直接算得）
    contrast: float = 0.0                      # 亮度动态范围 p95/p5（由图像统计直接算得）
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scene_type": self.scene_type,
            "ambient_kelvin": round(float(self.ambient_kelvin), 1),
            "lights": [h.to_dict() for h in self.lights],
            "key_to_fill_ratio": round(float(self.key_to_fill_ratio), 3),
            "contrast": round(float(self.contrast), 3),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- 工具


def _clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _to_img(rgb: np.ndarray) -> np.ndarray:
    """uint8 / 灰度 / 0~1 浮点 → 新的 float32 HxWx3（0~1）。"""
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = np.stack([a, a, a], axis=-1)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError("rgb 必须是 HxWx3 或 HxW 图像数组")
    a = a[..., :3]
    f = a.astype(np.float32)
    if a.dtype == np.uint8:
        f /= 255.0
    elif f.size and float(f.max()) > 1.5:
        f /= 255.0
    np.clip(f, 0.0, 1.0, out=f)
    return f


def _smooth3(a: np.ndarray) -> np.ndarray:
    """3×3 均值平滑，与 core.reference._box3 的做法一致（此处为局部副本）。"""
    p = np.pad(a, 1, mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
            p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
            p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def _kelvin_from_mean(mean_rgb: np.ndarray) -> float:
    """由区域平均 R/B 比值估计色温。

    与 core.reference._estimate_kelvin 同一映射（ratio 0.7→约 9000K 偏蓝、
    1.0→6500K、1.5→约 3200K 偏暖），但可作用于任意区域（阴影/轮廓带）。
    """
    r = float(mean_rgb[0]) + 1e-4
    b = float(mean_rgb[2]) + 1e-4
    ratio = _clip(r / b, 0.5, 2.0)
    return _clip(6500.0 / ratio ** 1.6, _KELVIN_MIN, _KELVIN_MAX)


def _region_kelvin(img: np.ndarray, mask: np.ndarray, fallback: float) -> float:
    """区域平均色 → 色温；区域太小则回落全局值。"""
    if int(mask.sum()) < 16:
        return float(fallback)
    return _kelvin_from_mean(img[mask].mean(axis=0))


def _make_light(role: str, intensity: float, kelvin: float, radius: float,
                dx: float = 0.0, dy: float = -1.0, dz: float = 0.7,
                kind: str = "directional") -> Light:
    return Light(
        name=_ROLE_NAME[role],
        kind=kind,
        dx=_clip(dx, -1.0, 1.0),
        dy=_clip(dy, -1.0, 1.0),
        dz=_clip(dz, 0.05, 1.0),
        intensity=_clip(intensity, _INTENSITY_MIN, _INTENSITY_MAX),
        kelvin=_clip(kelvin, _KELVIN_MIN, _KELVIN_MAX),
        radius=_clip(radius, _RADIUS_MIN, _RADIUS_MAX),
        visible=True,
    )


# ---------------------------------------------------------------- 场景判断


def _scene_type(p50: float, kelvin: float, kdx: float, kdy: float,
                directionality: float, has_rim: bool) -> str:
    """由亮度/色温/方向/轮廓光给出中文场景判断（判断，不是测量）。"""
    if p50 < 0.28:
        tone = "暗调"
    elif p50 > 0.62:
        tone = "高调"
    else:
        tone = "中间调"
    if kelvin < 4200.0:
        temp = "暖色温"
    elif kelvin > 7000.0:
        temp = "冷色温"
    else:
        temp = "中性色温"
    if directionality < 0.25:
        direction = "正面光"
    elif abs(kdx) >= abs(kdy):
        direction = "左侧光" if kdx < 0 else "右侧光"
    else:
        direction = "顶部光" if kdy < 0 else "底部光"
    scene = f"{tone} · {temp} · {direction}"
    if has_rim:
        scene += " · 带轮廓光"
    return scene


# ---------------------------------------------------------------- 轮廓光检测


def _detect_rim(lum: np.ndarray, p50: float, p60: float) -> Optional[dict]:
    """检测"某条边缘明显亮于紧邻内侧"的轮廓光。

    返回 {'side','gain','outer','inner'} 或 None。
    """
    h, w = lum.shape
    short = min(h, w)
    band = max(2, min(int(round(0.10 * short)), short // 3))
    if band < 2:
        return None
    outer = {
        "left": lum[:, :band], "right": lum[:, -band:],
        "top": lum[:band, :], "bottom": lum[-band:, :],
    }
    inner = {
        "left": lum[:, band:2 * band], "right": lum[:, -2 * band:-band],
        "top": lum[band:2 * band, :], "bottom": lum[-2 * band:-band, :],
    }
    best = None
    for side, o_slice in outer.items():
        o = float(o_slice.mean())
        i = float(inner[side].mean())
        if o < p60:                       # 边缘本身不够亮
            continue
        if i > p50 * 1.15:                # 内侧也亮 → 是受光面，不是轮廓光
            continue
        gain = (o - i) / max(i, 1e-3)
        if gain < 0.25:
            continue
        if best is None or gain > best["gain"]:
            best = {"side": side, "gain": gain, "outer": o, "inner": i}
    return best


_RIM_DIR = {"left": (-1.0, 0.0), "right": (1.0, 0.0),
            "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
_RIM_SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}


# ---------------------------------------------------------------- 回落结果


def _preset_rig(reason: str, kelvin: float, contrast: float) -> HarvestResult:
    """无法推断时回落到标准棚拍的布光，并如实说明。"""
    preset = get_preset("标准棚拍")
    lights: List[HarvestedLight] = []
    for i, role in enumerate(("主光", "辅光")):
        if i >= len(preset.lights):
            break
        light = copy.deepcopy(preset.lights[i])
        light.kelvin = _clip(float(kelvin), _KELVIN_MIN, _KELVIN_MAX)
        lights.append(HarvestedLight(role=role, light=light, confidence=0.05, basis=reason))
    amb_k = _clip(float(kelvin), _KELVIN_MIN, _KELVIN_MAX)
    lights.append(HarvestedLight(
        role="环境光",
        light=_make_light("环境光", float(preset.ambient_intensity), amb_k, 1.5,
                          dx=0.0, dy=0.0, dz=0.95, kind="point"),
        confidence=0.05, basis=reason))
    ratio = 0.0
    if len(lights) >= 2:
        ratio = float(lights[0].light.intensity) / max(float(lights[1].light.intensity), 1e-6)
    return HarvestResult(
        scene_type="无法判断（已回落到标准棚拍布光）",
        ambient_kelvin=amb_k,
        lights=lights,
        key_to_fill_ratio=ratio,
        contrast=float(contrast),
        notes=[DISCLAIMER,
               reason,
               "以上布光取自内置预设，未对参考图做任何推断；请在界面上手动调整。"],
    )


# ---------------------------------------------------------------- 主流程


def harvest(rgb: np.ndarray) -> HarvestResult:
    """从参考照片反推布光，返回 HarvestResult（纯估计，不含绝对光度量）。"""
    img = _to_img(rgb)
    h, w = img.shape[:2]
    if h < 8 or w < 8:
        return _preset_rig("参考图小于 8×8 像素，无法做梯度分析。", 6500.0, 1.0)

    # 超大图先做块均值降采样（低通不产生假梯度），把统计内存压到 4MP 以内
    if h * w > _STAT_MAX_PX:
        step = int(math.ceil(math.sqrt(h * w / _STAT_MAX_PX)))
        hh, ww = (h // step) * step, (w // step) * step
        img = np.ascontiguousarray(
            img[:hh, :ww].reshape(hh // step, step, ww // step, step, 3).mean(axis=(1, 3)))
        h, w = img.shape[:2]

    lum = img @ np.array([0.299, 0.587, 0.114], np.float32)
    p5, p10, p50, p95 = (float(x) for x in np.percentile(lum, (5.0, 10.0, 50.0, 95.0)))
    contrast = _clip(p95 / max(p5, 1e-4), 1.0, 256.0)
    global_k = _kelvin_from_mean(img.reshape(-1, 3).mean(axis=0))

    if float(lum.max() - lum.min()) < 1e-4:
        return _preset_rig("参考图亮度几乎均匀，光照方向无法推断。", global_k, contrast)

    # 亮暗半区相对差 → 侧向程度（0=接近均匀/正面光，1=强侧光）
    hi_mask = lum >= float(np.percentile(lum, 75.0))
    lo_mask = lum <= float(np.percentile(lum, 25.0))
    hi_mean = float(lum[hi_mask].mean())
    lo_mean = float(lum[lo_mask].mean())
    spread = _clip((hi_mean - lo_mean) / max(hi_mean + lo_mean, 1e-4), 0.0, 1.0)
    directionality = _clip(spread / 0.6, 0.0, 1.0)

    # 结构丰富度（相对梯度能量）→ 置信度参考
    gy, gx = np.gradient(_smooth3(lum))
    grad_rel = float(np.mean(np.hypot(gx, gy))) / max(float(np.mean(lum)), 1e-3)
    detail = _clip(grad_rel / 0.05, 0.0, 1.0)

    # 主光方向：复用 core.reference 的梯度主方向（指向亮侧 = 指向光源）
    feats = analyze_reference((img * 255.0 + 0.5).astype(np.uint8))
    kdx, kdy = float(feats["light_dx"]), float(feats["light_dy"])
    if not (math.isfinite(kdx) and math.isfinite(kdy)) or abs(kdx) + abs(kdy) < 1e-6:
        kdx, kdy = 0.0, -1.0              # 无有效梯度：依经验取顶光
        directionality = 0.0
    else:
        norm = math.hypot(kdx, kdy)
        kdx, kdy = kdx / norm, kdy / norm

    dz_key = _clip(0.90 - 0.62 * directionality, 0.20, 0.95)
    key_kelvin = _clip(float(feats.get("kelvin", global_k)), _KELVIN_MIN, _KELVIN_MAX)
    key_i = _clip(0.30 + 1.20 * p95 + 0.30 * spread, _INTENSITY_MIN, _INTENSITY_MAX)
    key_radius = _clip(0.45 - 0.28 * directionality, _RADIUS_MIN, _RADIUS_MAX)
    key_conf = _clip(0.30 + 0.45 * directionality + 0.20 * detail, 0.10, 0.92)
    key = HarvestedLight(
        role="主光",
        light=_make_light("主光", key_i, key_kelvin, key_radius, kdx, kdy, dz_key),
        confidence=key_conf,
        basis=(f"亮度梯度主方向（水平 {kdx:+.2f} / 垂直 {kdy:+.2f}）指向画面亮侧，"
               f"据此推断主光来向；亮暗半区相对差 {spread:.2f} 决定入射角"
               f"（越平越接近正面光，当前 dz={dz_key:.2f}）；高光分位 p95={p95:.3f} 定强度。"),
    )

    # 辅光：主光反向一侧、更接近机位；强度由阴影下限亮度决定
    fill_i = _clip(0.08 + 0.95 * p5, _INTENSITY_MIN, _INTENSITY_MAX)
    fill_kelvin = _region_kelvin(img, lo_mask, global_k)
    fill = HarvestedLight(
        role="辅光",
        light=_make_light("辅光", fill_i, fill_kelvin,
                          _clip(key_radius + 0.15, _RADIUS_MIN, _RADIUS_MAX),
                          -kdx, -kdy, _clip(dz_key + 0.30, 0.25, 0.95)),
        confidence=_clip(0.30 + 0.35 * directionality, 0.10, 0.85),
        basis=(f"取主光反向一侧、更接近机位的角度作为辅光；阴影下限亮度 p5={p5:.3f} "
               f"与阴影区色温 {fill_kelvin:.0f}K 定其强度与颜色（强度为相对估计）。"),
    )

    lights: List[HarvestedLight] = [key, fill]

    # 轮廓光：仅在边缘带明显亮于紧邻内侧时成立
    rim = _detect_rim(lum, p50, float(np.percentile(lum, 60.0)))
    if rim is not None:
        side = rim["side"]
        rdx, rdy = _RIM_DIR[side]
        b = max(2, min(int(round(0.10 * min(h, w))), min(h, w) // 3))
        band_mask = np.zeros_like(lum, dtype=bool)
        if side == "left":
            band_mask[:, :b] = True
        elif side == "right":
            band_mask[:, -b:] = True
        elif side == "top":
            band_mask[:b, :] = True
        else:
            band_mask[-b:, :] = True
        rim_kelvin = _region_kelvin(img, band_mask, global_k)
        lights.append(HarvestedLight(
            role="轮廓光",
            light=_make_light("轮廓光", 0.20 + 1.10 * min(rim["gain"], 2.0),
                              rim_kelvin, 0.25, rdx, rdy, 0.30),
            confidence=_clip(0.30 + 1.20 * min(rim["gain"], 1.5), 0.10, 0.90),
            basis=(f"检测到{_RIM_SIDE_CN[side]}侧边缘带均值 {rim['outer']:.3f} 高于紧邻内侧 "
                   f"{rim['inner']:.3f}（+{rim['gain'] * 100:.0f}%），推断存在轮廓光；"
                   f"边缘带色温 {rim_kelvin:.0f}K。"),
        ))

    # 环境光：整体亮度下限 + 全局色温
    amb_i = _clip(0.15 + 1.10 * p10, 0.05, 2.0)
    amb = HarvestedLight(
        role="环境光",
        light=_make_light("环境光", amb_i, global_k, 1.5,
                          dx=0.0, dy=0.0, dz=0.95, kind="point"),
        confidence=0.30,
        basis=(f"整体亮度下限 p10={p10:.3f} 与全局色温 {global_k:.0f}K 推断为环境光"
               f"（覆盖阴影的最小填充，强度为相对估计）。"),
    )
    lights.append(amb)

    notes = [
        DISCLAIMER,
        f"scene_type 是基于亮度分布（中位 {p50:.3f}）、色温与边缘特征的启发式判断，"
        f"不是场景或人物识别结果。",
        f"key_to_fill_ratio={key_i / max(fill_i, 1e-6):.2f} 与 contrast={contrast:.2f} "
        f"（p95/p5）由图像统计直接算得，可复核；其余强度/色温/半径为相对估计。",
    ]
    if key_conf < 0.35:
        notes.append("参考图方向性弱或细节不足，主光方向的可信度较低，建议人工微调。")

    return HarvestResult(
        scene_type=_scene_type(p50, global_k, kdx, kdy, directionality, rim is not None),
        ambient_kelvin=global_k,
        lights=lights,
        key_to_fill_ratio=key_i / max(fill_i, 1e-6),
        contrast=contrast,
        notes=notes,
    )


def harvest_to_params(res: HarvestResult, base: Optional[RenderParams] = None) -> RenderParams:
    """把拾光结果写入渲染参数（深拷贝 base，绝不修改入参）。"""
    src = base if base is not None else get_preset("标准棚拍")
    p = copy.deepcopy(src)
    p.lights = [copy.deepcopy(h.light) for h in res.lights]
    k = float(res.ambient_kelvin)
    p.ambient_kelvin = _clip(k if k > 0.0 else 6500.0, _KELVIN_MIN, _KELVIN_MAX)
    return p


# ---------------------------------------------------------------- 自检


def _selftest() -> int:
    import json

    failed = 0
    total = 0

    def check(cond: bool, label: str, extra: str = "") -> None:
        nonlocal failed, total
        total += 1
        if not cond:
            failed += 1
        print(f"{'PASS' if cond else 'FAIL'}  {label}{(' | ' + extra) if extra else ''}")

    rng = np.random.default_rng(7)

    def left_lit(h: int = 240, w: int = 320) -> np.ndarray:
        x = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
        base = 0.86 - 0.70 * x
        img = np.stack([base * 1.07, base * 0.98, base * 0.86], -1)
        img = img + rng.normal(0.0, 0.006, img.shape).astype(np.float32)
        return np.clip(img, 0, 1).repeat(h, axis=0)

    # ---- 左亮合成图
    lit = left_lit()
    res = harvest((lit * 255.0 + 0.5).astype(np.uint8))
    roles = [h.role for h in res.lights]
    key = next(h for h in res.lights if h.role == "主光")
    fill = next(h for h in res.lights if h.role == "辅光")
    check(key.light.dx < 0.0, "左亮图：主光 dx < 0（光来自左侧）",
          f"dx={key.light.dx:+.3f} dy={key.light.dy:+.3f} dz={key.light.dz:.3f}")
    check(0.0 < res.key_to_fill_ratio, "key_to_fill_ratio > 0（主光/辅光强度比）",
          f"ratio={res.key_to_fill_ratio:.2f} key_i={key.light.intensity:.3f} "
          f"fill_i={fill.light.intensity:.3f}")
    check(res.contrast > 0.0, "contrast = p95/p5 > 0", f"contrast={res.contrast:.2f}")
    check(res.key_to_fill_ratio > 1.0, "左亮图主光强于辅光（ratio > 1）")
    check(roles[:2] == ["主光", "辅光"] and "环境光" in roles,
          "光源角色：主光 / 辅光 / 环境光（+可选轮廓光）", f"roles={roles}")
    check(all(r in ROLES for r in roles), "所有角色均为文档化中文角色名")

    # ---- 轮廓光：暗底 + 左侧亮边
    rim_img = np.full((240, 320, 3), 0.13, np.float32)
    rim_img[30:210, :20] = np.array([0.94, 0.90, 0.82], np.float32)
    rim_res = harvest((rim_img * 255.0 + 0.5).astype(np.uint8))
    rim_roles = [h.role for h in rim_res.lights]
    check("轮廓光" in rim_roles, "亮边暗底图：检测到轮廓光", f"roles={rim_roles}")
    if "轮廓光" in rim_roles:
        rim_light = next(h for h in rim_res.lights if h.role == "轮廓光")
        check(rim_light.light.dx < 0.0 and abs(rim_light.light.dy) < 1e-6,
              "轮廓光来自亮边一侧（左侧 → dx<0）",
              f"dx={rim_light.light.dx:+.2f} dy={rim_light.light.dy:+.2f} "
              f"conf={rim_light.confidence:.2f}")
        check("轮廓光" in rim_res.scene_type, "场景判断里标注了轮廓光",
              f"scene={rim_res.scene_type}")
    flat_body = harvest((np.full((120, 160, 3), 60, np.uint8)))
    check("轮廓光" not in [h.role for h in flat_body.lights],
          "均匀图不产生轮廓光（无假阳性）")

    # ---- 诚实性：notes 必须含无法推导的量的声明；任何字段都不得伪装成光度测量
    joined = " ".join(res.notes)
    check(("照度" in joined) and ("CRI" in joined) and ("Δuv" in joined),
          "notes 含 照度/CRI/Δuv 无法推导的中文声明")
    check(all(n.strip() for n in res.notes) and len(res.notes) >= 2,
          "notes 为多条非空中文说明", f"n={len(res.notes)}")
    blob = json.dumps(res.to_dict(), ensure_ascii=False)

    def _all_keys(node) -> set:
        if isinstance(node, dict):
            out = set()
            for k, v in node.items():
                out.add(str(k).lower())
                out |= _all_keys(v)
            return out
        if isinstance(node, list):
            out = set()
            for v in node:
                out |= _all_keys(v)
            return out
        return set()

    field_keys = _all_keys({k: v for k, v in res.to_dict().items() if k != "notes"})
    forbidden = ("lx", "lux", "cri", "duv", "uv", "flicker", "power",
                 "lumen", "流明", "瓦", "照度")
    hit = sorted({t for t in forbidden for k in field_keys if t in k})
    check(not hit, "结果字段中无 lx/CRI/Δuv/flicker/power/照度 等无法计算的量",
          f"字段={sorted(field_keys)}" if not hit else f"命中={hit}")
    top_keys = set(res.to_dict().keys())
    check(top_keys == {"scene_type", "ambient_kelvin", "lights",
                       "key_to_fill_ratio", "contrast", "notes"},
          "to_dict 字段与契约一致", f"keys={sorted(top_keys)}")
    light_keys = set(res.lights[0].to_dict().keys())
    check(light_keys == {"role", "light", "confidence", "basis"},
          "HarvestedLight.to_dict 字段一致", f"keys={sorted(light_keys)}")
    check(all(0.0 <= h.confidence <= 1.0 and h.basis.strip() for h in res.lights),
          "每个光源都有 0~1 的 confidence 与中文 basis")
    check(DISCLAIMER in res.notes and "启发式判断" in blob,
          "结果与 notes 均明确标注为启发式估计")
    check(all("估计" in h.basis or "推断" in h.basis for h in res.lights),
          "每个光源的 basis 说明都是推断/估计口径")

    # ---- 值域夹取
    bad_ok = True
    for h in res.lights:
        li = h.light
        bad_ok = bad_ok and (_INTENSITY_MIN - 1e-9 <= li.intensity <= _INTENSITY_MAX + 1e-9) \
            and (_KELVIN_MIN - 1e-9 <= li.kelvin <= _KELVIN_MAX + 1e-9) \
            and (_RADIUS_MIN - 1e-9 <= li.radius <= _RADIUS_MAX + 1e-9) \
            and all(math.isfinite(v) for v in (li.dx, li.dy, li.dz, li.intensity,
                                               li.kelvin, li.radius))
    check(bad_ok, "所有光源参数在引擎值域内且为有限值 (i 0.05~4 / K 1500~12000 / r 0.05~1.5)")

    # 极亮/极暗/强色偏图也要落在值域内
    for name, arr in (("极亮", np.full((64, 64, 3), 255, np.uint8)),
                      ("极暗", np.full((64, 64, 3), 0, np.uint8)),
                      ("纯红", np.tile(np.array([255, 0, 0], np.uint8), (64, 64, 1)))):
        r2 = harvest(arr)
        ok = all(_KELVIN_MIN - 1e-9 <= h.light.kelvin <= _KELVIN_MAX + 1e-9
                 and _INTENSITY_MIN - 1e-9 <= h.light.intensity <= _INTENSITY_MAX + 1e-9
                 for h in r2.lights) and r2.lights and math.isfinite(r2.contrast)
        check(ok, f"{name}图：不崩且值域合法",
              f"scene={r2.scene_type} contrast={r2.contrast:.2f}")

    # ---- 退化输入
    tiny = harvest(np.zeros((4, 4, 3), np.uint8))
    check(len(tiny.lights) >= 2 and "无法" in tiny.scene_type,
          "小于 8px 的图：回落到预设布光并说明原因", f"scene={tiny.scene_type}")
    check("照度" in " ".join(tiny.notes), "回落结果同样带诚实性声明")
    gray = harvest(np.full((40, 40), 120, np.uint8))
    check(len(gray.lights) >= 2, "灰度 HxW 输入可用", f"roles={[h.role for h in gray.lights]}")
    f01 = harvest(np.full((40, 40, 3), 0.5, np.float32))
    check(len(f01.lights) >= 2, "0~1 浮点输入可用")
    try:
        harvest(np.zeros((4, 4), np.uint8))
        check(True, "极小灰度图不崩")
    except Exception as e:                              # pragma: no cover
        check(False, "极小灰度图不崩", repr(e))

    # ---- harvest_to_params
    base = get_preset("标准棚拍")
    before = base.to_json()
    before_first = base.lights[0]
    out = harvest_to_params(res, base)
    check(base.to_json() == before, "harvest_to_params 不修改 base")
    check(base.lights[0] is before_first, "base 的光源对象未被替换")
    check(out is not base and isinstance(out, RenderParams), "返回新的 RenderParams")
    check([l.name for l in out.lights] == [h.light.name for h in res.lights]
          and len(out.lights) == len(res.lights),
          "返回参数使用拾取到的光源", f"n={len(out.lights)}")
    check(out.ambient_kelvin == res.ambient_kelvin, "ambient_kelvin 已同步",
          f"{out.ambient_kelvin:.1f}")
    check(out.lights[0] is not res.lights[0].light,
          "返回的光源是深拷贝（改动不会回写结果）")
    out.lights[0].intensity = 3.99
    out.ambient_kelvin = 3000.0
    check(res.lights[0].light.intensity != 3.99 and res.ambient_kelvin != 3000.0,
          "改动返回参数不影响 HarvestResult")
    default_out = harvest_to_params(res)
    check(isinstance(default_out, RenderParams) and len(default_out.lights) == len(res.lights),
          "base=None 时使用标准棚拍默认参数")
    empty = HarvestResult()
    check(isinstance(harvest_to_params(empty, base), RenderParams)
          and base.to_json() == before,
          "空结果/空光源：可用且不改 base")
    check(harvest_to_params(HarvestResult(ambient_kelvin=99999.0), base).ambient_kelvin <= 12000.0,
          "异常的 ambient_kelvin 被夹取")

    # ---- 确定性 + 可序列化
    r_a = harvest((lit * 255.0 + 0.5).astype(np.uint8)).to_dict()
    r_b = harvest((lit * 255.0 + 0.5).astype(np.uint8)).to_dict()
    check(r_a == r_b, "同一输入两次拾光结果一致（确定性）")
    check(json.loads(json.dumps(r_a, ensure_ascii=False)) == r_a,
          "结果可 JSON 序列化且无损往返")

    print(f"\n{'PASS' if failed == 0 else 'FAIL'}  core.harvest 自检："
          f"{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
