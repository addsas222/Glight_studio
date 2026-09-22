# -*- coding: utf-8 -*-
"""物理渲染引擎（本地离线）。

- 深度图 → 2.5D 高度场 → 法线重建
- 多光源 Lambert 漫反射 + Phong 高光，色温（开尔文→RGB）
- 2.5D 高度场逐光源阴影投射（沿光线步进做深度缓冲比较，光源间独立互斥遮挡）
- 阴影锐度：hard=硬朗（日系赛璐珞，锐利二值边） / soft=柔化半影（摄影棚，PCF 式柔化）
- 环境光独立强度/色温通道
- 快速预览（低分辨率光照图上采样叠加）与高质量（全分辨率）双模式
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

import numpy as np
from PIL import Image

from .types import Light, RenderParams

# ---------------------------------------------------------------- 色温


def kelvin_to_rgb(k: float) -> np.ndarray:
    """开尔文色温 → 线性近似 RGB（0~1）。Tanner Helland 近似式。"""
    k = min(max(k, 1000.0), 40000.0) / 100.0
    if k <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(k) - 161.1195681661
    else:
        r = 329.698727446 * ((k - 60) ** -0.1332047592)
        g = 288.1221695283 * ((k - 60) ** -0.0755148492)
    if k >= 66:
        b = 255.0
    elif k <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(k - 10) - 305.0447927307
    rgb = np.array([r, g, b], np.float32)
    return np.clip(rgb, 0, 255) / 255.0

# ---------------------------------------------------------------- 深度/法线


def depth_to_height(depth: np.ndarray, height_scale: float = 0.22) -> np.ndarray:
    """深度（0~1，大=近）→ 高度场。动漫立绘默认适中的凸起幅度。"""
    return depth.astype(np.float32) * height_scale


def height_to_normal(height: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """由高度场重建法线图（HxWx3，指向 +z 朝观察者）。

    量纲说明（关键）：高度场以**归一化长度单位**计（长边 = 1，见
    depth_to_height 的 height_scale），而 np.gradient 默认以**像素**为步长，
    两者相差 L = max(h,w) 倍。因此必须显式传入 spacing=1/L，否则梯度会被
    低估约 L 倍，法线几乎恒为 (0,0,1)——光照退化成整体色偏、高光变成
    均匀光泽、点击拾取得到的方向在任何像素都一样。

    strength 为感知放大系数（1.0 = 物理上正确的坡度）。
    """
    h, w = height.shape[:2]
    L = float(max(h, w))                 # 1 个归一化单位 = L 个像素
    gy, gx = np.gradient(height, 1.0 / L)
    nx = -gx * strength
    ny = -gy * strength
    nz = np.ones_like(height)
    n = np.sqrt(nx * nx + ny * ny + nz * nz)
    return np.stack([nx / n, ny / n, nz / n], axis=-1).astype(np.float32)

# ---------------------------------------------------------------- 基础工具


def _box_blur_sep(a: np.ndarray, r: int) -> np.ndarray:
    """分离盒式模糊（半径 r），float32。"""
    if r <= 0:
        return a
    k = 2 * r + 1
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    c = np.cumsum(p, 1)
    a = (c[:, k - 1:k - 1 + a.shape[1]] -
         np.pad(c, ((0, 0), (1, 0)))[:, :a.shape[1]]) / k
    p = np.pad(a, ((r, r), (0, 0)), mode="edge")
    c = np.cumsum(p, 0)
    a = (c[k - 1:k - 1 + a.shape[0], :] -
         np.pad(c, ((1, 0), (0, 0)))[:a.shape[0], :]) / k
    return a.astype(np.float32)


def _resize_f(a: np.ndarray, hw: Tuple[int, int]) -> np.ndarray:
    if a.shape[0] == hw[0] and a.shape[1] == hw[1]:
        return a.astype(np.float32)
    return np.asarray(Image.fromarray(a).resize((hw[1], hw[0]), Image.BILINEAR),
                      np.float32)


def _resize_normal_field(n: np.ndarray, hw: Tuple[int, int]) -> np.ndarray:
    """缩放法线场并重新归一化（编码到 0~1 再插值，避免负值截断）。"""
    n = np.asarray(n, np.float32)[..., :3]
    if n.shape[0] == hw[0] and n.shape[1] == hw[1]:
        return n
    enc = ((np.clip(n, -1, 1) * 0.5 + 0.5) * 255).astype(np.uint8)
    dec = np.asarray(Image.fromarray(enc).resize(
        (hw[1], hw[0]), Image.BILINEAR), np.float32) / 255.0 * 2.0 - 1.0
    norm = np.linalg.norm(dec, axis=-1, keepdims=True)
    return (dec / np.maximum(norm, 1e-6)).astype(np.float32)

# ---------------------------------------------------------------- 单光源光照


def _light_dirs(l: Light, hw: Tuple[int, int], yy, xx):
    """返回逐像素指向光源的单位方向 L（HxWx3）及该光源为点光时的位置。"""
    h, w = hw
    if l.kind == "point":
        lx = l.px * w
        ly = l.py * h
        lz = max(l.pz, 0.05) * w
        vx = lx - xx
        vy = ly - yy
        vz = lz - 0.0
        dist = np.sqrt(vx * vx + vy * vy + vz * vz) + 1e-6
        L = np.stack([vx / dist, vy / dist, vz / dist], -1)
        return L, dist
    dx, dy, dz = l.direction_vector()
    L = np.empty((h, w, 3), np.float32)
    L[..., 0] = dx
    L[..., 1] = dy
    L[..., 2] = dz
    return L, None


def _shadow_mask(l: Light, height: np.ndarray, hw: Tuple[int, int],
                 L: np.ndarray, steps: int = 22) -> np.ndarray:
    """2.5D 高度场阴影投射：沿指向光源方向步进采样高度场，
    若采样点高度高于光线高度则被遮挡。返回 HxW float 0(遮挡)~1(受光)。

    量纲说明（关键）：高度场的水平范围是归一化坐标 [0,1]（以图像长边为 1），
    高度值同样以该单位计（height = depth * height_scale）。因此水平步长与
    光线爬升都必须用**归一化**单位：水平 1/steps，爬升 slope*(1/steps)。
    若误用像素步长（max(h,w)/steps），第一段光线就会抬升到高度场之上，
    遮挡永远不成立。
    """
    h, w = hw
    H0 = height
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    if l.kind == "point":
        lx, ly, lz = l.px * w, l.py * h, max(l.pz, 0.05) * w
        # 每像素方向不同：逐帧 gather 采样
        vx = lx - xx
        vy = ly - yy
        vz = lz
        dist = np.sqrt(vx * vx + vy * vy + vz * vz) + 1e-6
        ux, uy, uz = vx / dist, vy / dist, vz / dist
    else:
        dx, dy, dz = l.direction_vector()
        ux = np.full((h, w), dx, np.float32)
        uy = np.full((h, w), dy, np.float32)
        uz = np.full((h, w), dz, np.float32)

    # 光线爬升速率：dz 相对于水平位移（两者同量纲，均为归一化单位）
    horiz = np.maximum(np.sqrt(ux * ux + uy * uy), 1e-4)
    slope = uz / horiz
    # 采样步长（像素）与其归一化长度（= 水平步长 / 图像长边）
    step_px = max(h, w) / steps
    step_norm = 1.0 / steps
    occluded = np.zeros((h, w), bool)

    for i in range(1, steps + 1):
        sx = xx + ux / horiz * step_px * i
        sy = yy + uy / horiz * step_px * i
        ray_h = H0 + slope * step_norm * i
        # 采样（越界视为无遮挡）
        sx0 = np.clip(sx, 0, w - 1).astype(np.int32)
        sy0 = np.clip(sy, 0, h - 1).astype(np.int32)
        inb = (sx >= 0) & (sx <= w - 1) & (sy >= 0) & (sy <= h - 1)
        hs = H0[sy0, sx0]
        occluded |= inb & (hs > ray_h + 0.002)

    shadow = (~occluded).astype(np.float32)
    return shadow

# ---------------------------------------------------------------- 主渲染


def render(rgb: np.ndarray, depth: np.ndarray, params: RenderParams,
           progress: Optional[Callable[[float, str], None]] = None,
           normal: Optional[np.ndarray] = None
           ) -> np.ndarray:
    """核心渲染入口。rgb/depth 同尺寸（HxWx3 uint8 / HxW float 0~1）。

    normal：可选的 AI 法线图（HxWx3，单位向量，-1~1）。为 None 时由深度场
    几何派生。

    lighting_mode 决定光照计算方式：
      'hq'     —— 逐光源物理阴影投射（遮挡互斥），按 lighting_size 限制
                  光照计算分辨率后上采样；
      'linear' —— 快速预览，线性叠加，不做阴影投射。
    """
    h, w = rgb.shape[:2]

    # 光照计算代理分辨率（仅限制开销，输出始终为原尺寸）
    proxy_max = (params.lighting_size if params.lighting_mode == "hq"
                 else params.preview_size)
    scale = min(1.0, max(proxy_max, 64) / max(h, w))
    ph, pw = max(int(h * scale), 8), max(int(w * scale), 8)
    if scale < 1.0:
        img_p = np.asarray(Image.fromarray(rgb).resize((pw, ph), Image.BILINEAR))
        dep_p = _resize_f(depth, (ph, pw))
        n_p = None if normal is None else _resize_normal_field(normal, (ph, pw))
    else:
        img_p, dep_p = rgb, depth
        n_p = normal

    light_map, spec_map = _compute_light_maps(img_p, dep_p, params,
                                              progress, n_p)
    if progress:
        progress(0.8, "合成输出")
    # 光照图上采样回原尺寸
    lh, lw = light_map.shape[:2]
    if (lh, lw) != (h, w):
        light_map = np.asarray(Image.fromarray(
            np.clip(light_map * 255, 0, 255).astype(np.uint8)
        ).resize((w, h), Image.BILINEAR), np.float32) / 255.0
        spec_map = np.asarray(Image.fromarray(
            np.clip(spec_map * 255, 0, 255).astype(np.uint8)
        ).resize((w, h), Image.BILINEAR), np.float32) / 255.0

    base = rgb.astype(np.float32) / 255.0
    out = _composite(base, light_map, spec_map, params)
    if progress:
        progress(1.0, "完成")
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def _composite(base: np.ndarray, light_map: np.ndarray,
               spec_map: np.ndarray, params: RenderParams) -> np.ndarray:
    """合成：新光照（环境光+光源）与原照明（=1，即原图明暗结构）按
    tone_preserve 插值，叠加高光；多光源过曝经软肩部滚降到纯白。

    tone_preserve=1 → 完全保留原图明暗；=0 → 完全采用新光照。
    """
    amb_rgb = kelvin_to_rgb(params.ambient_kelvin)
    amb = amb_rgb[None, None, :] * params.ambient_intensity
    L_new = amb + light_map
    tp = min(max(params.tone_preserve, 0.0), 1.0)
    L = L_new * (1.0 - tp) + tp
    out = base * L + spec_map * params.specular_strength
    out = out * params.exposure
    # 软肩部：0.85 以上平滑滚降至 1.0
    knee = 0.85
    over = np.maximum(out - knee, 0.0) / (1.0 - knee)
    out = np.where(out > knee, knee + (1.0 - knee) * np.tanh(over), out)
    return np.clip(out, 0.0, 1.0)


def _compute_light_maps(img: np.ndarray, depth: np.ndarray,
                        params: RenderParams, progress=None,
                        normal: Optional[np.ndarray] = None
                        ) -> Tuple[np.ndarray, np.ndarray]:
    """计算逐像素照明（RGB 0~）与高光（0~）。

    normal 为 None 时由深度场几何派生。
    """
    h, w = img.shape[:2]
    height = depth_to_height(depth)
    if normal is None:
        normal = height_to_normal(height)
    else:
        normal = np.asarray(normal, np.float32)[..., :3]
        if normal.shape[:2] != (h, w):
            normal = _resize_normal_field(normal, (h, w))
    physical = params.lighting_mode == "hq"

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    light_acc = np.zeros((h, w, 3), np.float32)
    spec_acc = np.zeros((h, w, 3), np.float32)
    view = np.zeros((h, w, 3), np.float32)
    view[..., 2] = 1.0

    lights = [l for l in params.lights if l.visible]
    n = len(lights)
    for i, l in enumerate(lights):
        if progress:
            progress(0.15 + 0.55 * i / max(n, 1), f"光源 {i+1}/{n}：{l.name}")
        L, dist = _light_dirs(l, (h, w), yy, xx)
        ndl = np.clip((normal * L).sum(-1), 0, 1)

        # 点光距离衰减（半径控制）
        if dist is not None:
            r = max(l.radius, 0.02) * w
            atten = 1.0 / (1.0 + (dist / r) ** 2)
        else:
            atten = 1.0

        # 物理阴影投射仅在高质量光照模式下执行（快速预览为线性叠加）
        if physical:
            steps = 20 if max(h, w) <= 512 else 28
            vis = _shadow_mask(l, height, (h, w), L, steps)
            if params.shadow_mode == "soft":
                # 柔化半影：随半径模糊 + 光线斜率加权
                pr = max(int(2 + l.radius * 8), 2)
                vis = _box_blur_sep(vis, pr)
                vis = np.clip(vis, 0, 1)
            else:
                # 硬朗赛璐珞：锐利阈值
                vis = (vis > 0.5).astype(np.float32)
            # 阴影强度：0=无阴影，1=全黑（默认 0.85 → 阴影区保留 15% 环境贡献）
            ss = min(max(params.shadow_strength, 0.0), 1.0)
            vis = 1.0 - ss * (1.0 - vis)
        else:
            vis = 1.0

        lc = kelvin_to_rgb(l.kelvin)
        contrib = (ndl * atten * vis)[..., None] * lc[None, None, :] * l.intensity
        light_acc += contrib

        # Phong 高光（半程向量）
        if params.specular_strength > 0.001:
            if dist is not None:
                Vv = view
                Hh = L + Vv
                Hn = Hh / (np.sqrt((Hh * Hh).sum(-1))[..., None] + 1e-6)
            else:
                Hh = L + view
                Hn = Hh / (np.sqrt((Hh * Hh).sum(-1))[..., None] + 1e-6)
            ndh = np.clip((normal * Hn).sum(-1), 0, 1)
            # 高光锐度固定，不受「阴影锐度」影响：该开关只应改变阴影边缘，
            # 否则用户切换阴影风格时会连带改变材质光泽（行为不一致）。
            s = ndh ** 18.0 * atten * vis
            spec_acc += (s * l.intensity)[..., None] * lc[None, None, :]

    return light_acc, np.clip(spec_acc, 0, 1)
