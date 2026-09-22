# -*- coding: utf-8 -*-
"""逐帧光绘：把发光笔触（点光源 / 柔光晕 / 光束 / 星芒）叠加到单帧图像上。

用于视频逐帧的"光绘"创作：用户在某一帧上画下若干笔触，程序按笔触的
颜色（色温）、强度、柔边与叠加方式把光"画"进画面。

设计要点：
- 全程 numpy 向量化，且每个笔触只在其包围盒 ROI 内计算与合成，
  因此 4K 帧叠加 20 个笔触也远快于一帧视频的时间预算；
- 颜色复用 ``core.render.kelvin_to_rgb``，与渲染引擎的色温定义保持一致；
- 坐标一律为归一化图像坐标（x 向右、y 向下为正），
  ``radius`` 相对长边归一化，故不同分辨率下笔触视觉大小一致；
- 返回值始终是新的 uint8 数组，绝不修改入参；
- 参数越界一律夹取到文档化范围，未知类型/叠加方式回落到默认值，绝不抛错。

自检：``python -m core.strokes``（本模块在包内，须以模块方式运行）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Sequence

import numpy as np

from .render import kelvin_to_rgb

# 笔触类型：点光源 / 柔光晕 / 光束 / 星芒
STROKE_TYPES = ("point", "glow", "beam", "starburst")
# 叠加方式：叠加 / 滤色 / 柔光
BLEND_MODES = ("add", "screen", "soft")

_DEFAULT_TYPE = "glow"
_DEFAULT_BLEND = "screen"
_DEFAULT_RADIUS = 0.15
_DEFAULT_KELVIN = 5600.0
_DEFAULT_SPREAD = 30.0

_KELVIN_MIN, _KELVIN_MAX = 1500.0, 12000.0
_INTENSITY_MAX = 4.0
_RADIUS_MAX = 4.0
_SPREAD_MIN, _SPREAD_MAX = 1.0, 180.0
_SPIKES_MIN, _SPIKES_MAX = 2, 24
# 柔边过渡带最小宽度：防止 softness=0 时除以 0（此时为硬边）
_FALLOFF_MIN = 0.01


@dataclass
class Stroke:
    """一个发光笔触。"""

    type: str = "glow"          # point|glow|beam|starburst
    x: float = 0.5              # 归一化图像坐标（0~1，右为正）
    y: float = 0.5              # 归一化图像坐标（0~1，下为正）
    radius: float = 0.15        # 归一化半径（相对长边）
    intensity: float = 1.0      # 0~4
    kelvin: float = 5600.0      # 1500~12000
    softness: float = 0.6       # 0~1 柔边
    blend: str = "screen"       # add|screen|soft
    angle: float = 0.0          # 光束方向，度（x 正方向为 0°，顺时针为正）
    spread: float = 30.0        # 光束张角，度
    spikes: int = 6             # 星芒条数

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Stroke":
        if not isinstance(d, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def stroke_track_from_dict(d: dict) -> Dict[int, List[Stroke]]:
    """把 ``{"87": [{...}], "88": [...]}`` 反序列化为 ``{87: [Stroke, ...]}``。

    非法的帧号键或非列表值会被跳过，不会抛错。
    """
    track: Dict[int, List[Stroke]] = {}
    if not isinstance(d, dict):
        return track
    for key, value in d.items():
        try:
            frame = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, (list, tuple)):
            continue
        items: List[Stroke] = []
        for item in value:
            if isinstance(item, Stroke):
                items.append(item)
            elif isinstance(item, dict):
                items.append(Stroke.from_dict(item))
        track[frame] = items
    return track


# ---------------------------------------------------------------- 参数夹取


def _num(v, default: float) -> float:
    """尽力转 float；非法或非有限值回落到默认值。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(f):
        return float(default)
    return f


def _clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _norm(st: Stroke) -> Stroke:
    """夹取到文档化范围：未知类型/叠加方式回落默认，非法半径回落默认。"""
    radius = _num(st.radius, _DEFAULT_RADIUS)
    if radius <= 0.0:
        radius = _DEFAULT_RADIUS
    spread = _num(st.spread, _DEFAULT_SPREAD)
    if spread <= 0.0:
        spread = _DEFAULT_SPREAD
    return Stroke(
        type=st.type if st.type in STROKE_TYPES else _DEFAULT_TYPE,
        x=_clip(_num(st.x, 0.5), 0.0, 1.0),
        y=_clip(_num(st.y, 0.5), 0.0, 1.0),
        radius=min(radius, _RADIUS_MAX),
        intensity=_clip(_num(st.intensity, 1.0), 0.0, _INTENSITY_MAX),
        kelvin=_clip(_num(st.kelvin, _DEFAULT_KELVIN), _KELVIN_MIN, _KELVIN_MAX),
        softness=_clip(_num(st.softness, 0.6), 0.0, 1.0),
        blend=st.blend if st.blend in BLEND_MODES else _DEFAULT_BLEND,
        angle=_num(st.angle, 0.0),
        spread=_clip(spread, _SPREAD_MIN, _SPREAD_MAX),
        spikes=int(_clip(_num(st.spikes, 6.0), _SPIKES_MIN, _SPIKES_MAX)),
    )


# ---------------------------------------------------------------- 掩膜


def _roi(st: Stroke, h: int, w: int, long_edge: float) -> tuple:
    """笔触的像素包围盒（含 1px 余量）。"""
    r_px = st.radius * long_edge
    cx, cy = st.x * w, st.y * h
    x0 = max(int(math.floor(cx - r_px)), 0)
    x1 = min(int(math.ceil(cx + r_px)) + 1, w)
    y0 = max(int(math.floor(cy - r_px)), 0)
    y1 = min(int(math.ceil(cy + r_px)) + 1, h)
    return x0, x1, y0, y1


def _smoothstep(a: np.ndarray) -> None:
    """就地 smoothstep：a ← a²(3-2a)。"""
    t = a * a
    a *= -2.0
    a += 3.0
    t *= a
    a[:] = t


def _cos_kphi(u: np.ndarray, k: int) -> np.ndarray:
    """逐像素 cos(k·φ)，其中 u = cos(φ)。

    用切比雪夫递推 T₀=1、T₁=u、Tₙ=2u·Tₙ₋₁-Tₙ₋₂（cos(kφ) 即 Tₖ(u)），
    数学上与直接调用 cos(kφ) 等价，但省掉逐像素反三角/三角函数。
    """
    if k < 2:
        return np.ones_like(u) if k <= 0 else u
    prev = np.ones_like(u)
    cur = u.copy()
    for _ in range(2, k + 1):
        nxt = u * cur
        nxt *= 2.0
        nxt -= prev
        prev, cur = cur, nxt
    return cur


def _mask(st: Stroke, box: tuple, h: int, w: int, long_edge: float) -> np.ndarray:
    """在 ROI 内生成 0~1 的笔触亮度掩膜（径向衰减 + 类型整形）。"""
    x0, x1, y0, y1 = box
    r_px = st.radius * long_edge
    dy = (np.arange(y0, y1, dtype=np.float32) + 0.5) - st.y * h
    dx = (np.arange(x0, x1, dtype=np.float32) + 0.5) - st.x * w
    dyy = dy[:, None]
    dxx = dx[None, :]
    dist = np.sqrt(dxx * dxx + dyy * dyy)          # 像素距离
    # 光束/星芒需要方向分量：u = cos(θ - angle)，必须在除以半径之前算
    unit = None
    if st.type in ("beam", "starburst"):
        ang = math.radians(st.angle)
        unit = (dxx * math.cos(ang) + dyy * math.sin(ang)) / np.maximum(dist, 1e-6)
        np.copyto(unit, 1.0, where=dist <= 1e-6)    # 正中心：方向未定义，取最大亮

    dist /= r_px                                    # 归一化距离，1.0 = 笔触边界
    # 径向衰减：softness 只改变过渡带宽度，不改变外径
    dist -= 1.0
    dist *= -1.0 / max(0.9 * st.softness, _FALLOFF_MIN)
    np.clip(dist, 0.0, 1.0, out=dist)
    smooth = dist
    _smoothstep(smooth)                             # dist 现在就是 smoothstep 后的径向剖面

    if st.type == "point":
        root = np.sqrt(smooth)
        smooth *= root                              # 收紧：smooth^1.5
        return smooth
    if st.type == "glow":
        root = np.sqrt(smooth)
        smooth *= root                              # smooth^1.5
        np.sqrt(smooth, out=smooth)                 # smooth^0.75，柔和大光晕
        return smooth
    if st.type == "beam":
        half = math.radians(st.spread * 0.5)
        c_zero = math.cos(half)                     # 完全熄灭的张角
        c_full = math.cos(half * 0.65)              # 满亮的张角
        unit -= c_zero
        unit *= 1.0 / max(c_full - c_zero, 1e-6)
        np.clip(unit, 0.0, 1.0, out=unit)
        _smoothstep(unit)
        smooth *= unit
        return smooth
    # starburst：cos(k·φ) 的增亮瓣
    lobe = _cos_kphi(unit, st.spikes)
    np.clip(lobe, 0.0, 1.0, out=lobe)
    _smoothstep(lobe)
    lobe *= 0.65
    lobe += 0.35                                    # 中心保底亮度
    np.sqrt(smooth, out=smooth)
    smooth *= lobe
    return smooth


# ---------------------------------------------------------------- 合成


def _blend(region: np.ndarray, contrib: np.ndarray, mode: str) -> None:
    """把 contrib 按 mode 就地合成进 region（float32，0~1+）。"""
    if mode == "add":                              # 叠加：直接相加（可越过 1，末尾统一裁剪）
        np.add(region, contrib, out=region)
        return
    # 滤色 / 柔光只对 0~1 的源层有定义
    np.clip(contrib, 0.0, 1.0, out=contrib)
    if mode == "screen":                           # 滤色：1-(1-a)(1-b)
        np.subtract(1.0, contrib, out=contrib)
        contrib *= (1.0 - region)
        np.subtract(1.0, contrib, out=contrib)
        region[:] = contrib
        return
    # 柔光：只保留软光的"增亮"分支（b<=0.5 不动，避免笔触边缘压暗画面）
    d = np.sqrt(region)
    shadow_branch = ((16.0 * region - 12.0) * region + 4.0) * region
    np.copyto(d, shadow_branch, where=region <= 0.25)
    lift = region + (2.0 * contrib - 1.0) * (d - region)
    np.copyto(region, lift, where=contrib > 0.5)


def _as_float_rgb(rgb: np.ndarray) -> np.ndarray:
    """uint8/灰度/0~1 浮点 → 新的 float32 HxWx3（0~1）。"""
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = np.stack([a, a, a], axis=-1)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError("rgb 必须是 HxWx3 或 HxW 图像数组")
    a = a[..., :3]
    f = a.astype(np.float32)
    if f.size and a.dtype != np.uint8 and float(f.max()) > 1.5:
        f /= 255.0
    elif a.dtype == np.uint8:
        f /= 255.0
    np.clip(f, 0.0, 1.0, out=f)
    return f


def apply_strokes(rgb: np.ndarray, strokes: Sequence[Stroke]) -> np.ndarray:
    """把若干笔触发光叠加到一帧图像上，返回新的 uint8 HxWx3 数组。

    入参 ``rgb`` 不会被修改；未知类型/叠加方式回落默认值，越界参数夹取。
    """
    out = _as_float_rgb(rgb)
    h, w = out.shape[:2]
    long_edge = float(max(h, w))
    if h < 1 or w < 1:
        return out.astype(np.uint8)
    for raw in list(strokes or ()):
        st = _norm(raw if isinstance(raw, Stroke) else Stroke.from_dict(raw))
        if st.intensity <= 0.0:
            continue
        box = _roi(st, h, w, long_edge)
        if box[0] >= box[1] or box[2] >= box[3]:
            continue
        x0, x1, y0, y1 = box
        color = kelvin_to_rgb(st.kelvin).astype(np.float32)
        contrib = _mask(st, box, h, w, long_edge)[..., None] * color
        if st.intensity != 1.0:
            contrib *= st.intensity
        _blend(out[y0:y1, x0:x1], contrib, st.blend)
    np.clip(out, 0.0, 1.0, out=out)
    out *= 255.0
    np.rint(out, out=out)
    return out.astype(np.uint8)


# ---------------------------------------------------------------- 自检


def _selftest() -> int:
    import hashlib

    failed = 0
    total = 0

    def check(cond: bool, label: str, extra: str = "") -> None:
        nonlocal failed, total
        total += 1
        if not cond:
            failed += 1
        print(f"{'PASS' if cond else 'FAIL'}  {label}{(' | ' + extra) if extra else ''}")

    def digest(a: np.ndarray) -> str:
        return hashlib.sha1(a.tobytes()).hexdigest()

    h, w = 192, 256
    yy, xx = np.mgrid[0:h, 0:w]
    frame = (0.16 + 0.42 * (xx / (w - 1)) + 0.16 * (yy / (h - 1))).astype(np.float32)
    frame = (frame * 255.0).astype(np.uint8)
    frame = np.stack([frame] * 3, axis=-1)
    frame[:12, :12] = 40                      # 一点结构，避免整帧均匀
    base_digest = digest(frame)
    base_copy = frame.copy()

    types = list(STROKE_TYPES)
    blends = list(BLEND_MODES)
    all_ok = True
    for t in types:
        for b in blends:
            st = Stroke(type=t, x=0.5, y=0.5, radius=0.18, intensity=1.0,
                        kelvin=5600.0, softness=0.5, blend=b)
            out = apply_strokes(frame, [st])
            ok = (out.dtype == np.uint8 and out.shape == frame.shape
                  and digest(out) != base_digest)
            if b == "add":
                ok = ok and bool(np.all(out.astype(np.int16) >= frame.astype(np.int16)))
            bright = int(out.sum()) - int(frame.sum())
            check(ok, f"笔触 type={t} blend={b}：uint8/同尺寸/有变化"
                      + ("/逐像素不暗于原图" if b == "add" else ""),
                  f"delta_sum={bright}")
            all_ok = all_ok and ok
    check(all_ok, "4 种类型 × 3 种叠加方式 全部通过")

    # 四种类型混合 + 多种叠加
    mixed = [Stroke(type=t, x=0.15 + 0.22 * i, y=0.4, radius=0.2, blend=blends[i % 3])
             for i, t in enumerate(types)]
    out_mixed = apply_strokes(frame, mixed)
    check(out_mixed.dtype == np.uint8 and out_mixed.shape == frame.shape
          and digest(out_mixed) != base_digest, "四种笔触混合叠加：uint8/同尺寸/有变化")
    check(int(out_mixed.sum()) > int(frame.sum()), "混合叠加后总亮度上升")
    # 类型语义：光束只在张角内、星芒亮瓣数 = spikes、柔光晕尾部比点光宽
    def _ring_mask(st: Stroke, samples: int = 360, frac: float = 0.5) -> np.ndarray:
        le = float(max(h, w))
        box = _roi(st, h, w, le)
        m = _mask(st, box, h, w, le)
        r = frac * st.radius * le
        vals = []
        for a in np.linspace(0.0, 2.0 * math.pi, samples, endpoint=False):
            py = st.y * h + r * math.sin(a) - box[2]
            px = st.x * w + r * math.cos(a) - box[0]
            iy, ix = int(py), int(px)
            fy, fx = py - iy, px - ix
            iy2 = min(iy + 1, m.shape[0] - 1)
            ix2 = min(ix + 1, m.shape[1] - 1)
            vals.append(m[iy, ix] * (1 - fy) * (1 - fx) + m[iy, ix2] * (1 - fy) * fx
                        + m[iy2, ix] * fy * (1 - fx) + m[iy2, ix2] * fy * fx)
        return np.asarray(vals, np.float32)

    for k in (2, 6, 12):
        prof = _ring_mask(Stroke(type="starburst", x=0.5, y=0.5, radius=0.4,
                                 softness=0.3, spikes=k, angle=0.0))
        bright = prof > 0.75
        arcs = int((bright & ~np.roll(bright, 1)).sum())
        check(arcs == k, f"星芒 spikes={k} → 亮瓣数 {arcs}",
              f"min={prof.min():.2f} max={prof.max():.2f}")

    le = float(max(h, w))
    beam = Stroke(type="beam", x=0.5, y=0.5, radius=0.35, softness=0.4,
                  angle=0.0, spread=40.0)
    box = _roi(beam, h, w, le)
    bm = _mask(beam, box, h, w, le)
    cy, cx = h // 2 - box[2], w // 2 - box[0]
    step = int(round(0.5 * beam.radius * le))
    fwd = float(bm[cy, min(cx + step, bm.shape[1] - 1)])
    back = float(bm[cy, max(cx - step, 0)])
    check(fwd > 0.5 and back < 1e-6, "光束：张角内亮、反向完全熄灭",
          f"forward={fwd:.3f} back={back:.3f}")

    common = dict(x=0.5, y=0.5, radius=0.3, softness=0.5)
    box = _roi(Stroke(type="point", **common), h, w, le)
    pm = _mask(Stroke(type="point", **common), box, h, w, le)
    gm = _mask(Stroke(type="glow", **common), box, h, w, le)
    cy, cx = h // 2 - box[2], w // 2 - box[0]
    off = int(round(0.78 * common["radius"] * le))
    check(gm[cy, cx + off] > pm[cy, cx + off] and gm[cy, cx] >= pm[cy, cx] - 1e-6,
          "柔光晕尾部比点光宽（且中心不弱）",
          f"glow={float(gm[cy, cx + off]):.3f} point={float(pm[cy, cx + off]):.3f}")

    # 参数夹取
    bad = _norm(Stroke(type="point", kelvin=99999, radius=-1.0, intensity=99,
                       softness=5.0, spread=-10, spikes=0, x=9, y=-3))
    check(bad.kelvin <= 12000.0 and bad.kelvin == 12000.0, "kelvin 99999 → 夹取到 12000",
          f"kelvin={bad.kelvin}")
    check(bad.radius == 0.15, "负半径 → 回落默认 0.15", f"radius={bad.radius}")
    check(bad.intensity == 4.0, "intensity 99 → 4.0", f"intensity={bad.intensity}")
    check(bad.softness == 1.0, "softness 5 → 1.0", f"softness={bad.softness}")
    check(bad.spread == 30.0, "spread -10 → 回落默认 30", f"spread={bad.spread}")
    check(bad.spikes == 2, "spikes 0 → 2", f"spikes={bad.spikes}")
    check(bad.x == 1.0 and bad.y == 0.0, "x/y 夹取到 0~1", f"x={bad.x} y={bad.y}")
    zero_r = _norm(Stroke(radius=0.0, kelvin=100.0))
    check(zero_r.radius == 0.15 and zero_r.kelvin == 1500.0,
          "零半径回落默认 / kelvin 100 → 1500",
          f"radius={zero_r.radius} kelvin={zero_r.kelvin}")

    # 负半径笔触仍然可见（证明回落真的生效，而不是被丢弃）
    out_neg = apply_strokes(frame, [Stroke(type="point", x=0.5, y=0.5, radius=-0.4)])
    check(digest(out_neg) != base_digest, "负半径笔触回落默认后仍生效")

    # 未知类型 / 未知叠加方式：不抛错，且等价于默认值
    unknown = apply_strokes(frame, [Stroke(type="warp", blend="multiply")])
    default = apply_strokes(frame, [Stroke(type="glow", blend="screen")])
    check(unknown.dtype == np.uint8 and digest(unknown) != base_digest,
          "未知 type/blend 不抛错且产生输出")
    check(digest(unknown) == digest(default), "未知 type/blend 等价于默认 glow/screen")

    # 空笔触：新数组、内容不变
    out_empty = apply_strokes(frame, [])
    check(digest(out_empty) == base_digest and out_empty is not frame,
          "空笔触：内容不变且返回新数组")
    out_none = apply_strokes(frame, None)
    check(digest(out_none) == base_digest, "strokes=None：安全返回")

    # 入参不被修改
    check(digest(frame) == base_digest and bool(np.array_equal(frame, base_copy)),
          "入参 rgb 未被修改")

    # 4 种类型都真的改变了中心像素之外的外圈（避免只有中心一个点变化）
    for t in types:
        o = apply_strokes(frame, [Stroke(type=t, x=0.5, y=0.5, radius=0.45,
                                         angle=0.0, spread=40.0, softness=0.4)])
        ring = int(o[h // 2, w // 2 + int(0.3 * w)] .sum()) > int(
            frame[h // 2, w // 2 + int(0.3 * w)].sum())
        check(ring, f"{t} 在半径内非中心处同样增亮")

    # 极小尺寸不崩
    tiny = apply_strokes(np.zeros((8, 8, 3), np.uint8),
                         [Stroke(type="starburst", radius=0.4)])
    check(tiny.dtype == np.uint8 and tiny.shape == (8, 8, 3) and int(tiny.sum()) > 0,
          "8×8 帧可用（最小尺寸）")
    gray = apply_strokes(np.full((32, 32), 90, np.uint8), [Stroke()])
    check(gray.dtype == np.uint8 and gray.shape == (32, 32, 3) and int(gray.sum()) > 90 * 32 * 32,
          "灰度 HxW 输入可用并返回 HxWx3")

    # 零强度笔触不改变画面；贴边（被 ROI 裁剪）的笔触仍生效
    zero_i = apply_strokes(frame, [Stroke(x=0.5, y=0.5, radius=0.3, intensity=0.0)])
    check(digest(zero_i) == base_digest, "intensity=0 的笔触不改变画面")
    corner = apply_strokes(frame, [Stroke(x=1.0, y=1.0, radius=0.25)])
    check(digest(corner) != base_digest, "贴右下角的笔触（ROI 被裁剪）仍生效")
    corner_px = corner[h - 1, w - 1].astype(np.int16) - frame[h - 1, w - 1].astype(np.int16)
    check(bool(np.all(corner_px >= 0)) and int(corner_px.sum()) > 0,
          "贴角笔触只增亮、不压暗", f"corner_delta={corner_px.tolist()}")

    # 序列化往返
    st = Stroke(type="beam", x=0.3, y=0.7, radius=0.22, intensity=2.3, kelvin=3200.0,
                softness=0.35, blend="add", angle=45.0, spread=50.0, spikes=8)
    check(Stroke.from_dict(st.to_dict()) == st, "Stroke to_dict/from_dict 往返一致")
    check(Stroke.from_dict({"nope": 1}).type == "glow"
          and Stroke.from_dict("bad").type == "glow", "from_dict 容错未知/畸形输入")
    track = stroke_track_from_dict({"87": [st.to_dict(), st.to_dict()], "88": [],
                                    "bad": [st.to_dict()], "89": "nope"})
    check(sorted(track.keys()) == [87, 88] and len(track[87]) == 2
          and isinstance(track[87][0], Stroke) and track[88] == [],
          "stroke_track_from_dict：帧号转 int、跳过非法项",
          f"keys={sorted(track.keys())}")
    check(stroke_track_from_dict(None) == {}, "stroke_track_from_dict(None) → 空字典")

    # 性能：4K/2K 帧多笔触（逐帧视频预算内）
    import time
    big = np.full((2048, 2048, 3), 96, np.uint8)
    many = [Stroke(type=types[i % 4], x=(i * 0.11) % 1.0, y=(i * 0.17) % 1.0,
                   radius=0.15, blend=blends[i % 3]) for i in range(20)]
    t0 = time.perf_counter()
    out_big = apply_strokes(big, many)
    dt = (time.perf_counter() - t0) * 1000.0
    check(out_big.dtype == np.uint8 and out_big.shape == big.shape and dt < 2000.0,
          "2048×2048 + 20 笔触性能", f"{dt:.1f} ms")

    print(f"\n{'PASS' if failed == 0 else 'FAIL'}  core.strokes 自检："
          f"{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
