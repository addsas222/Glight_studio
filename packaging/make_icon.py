# -*- coding: utf-8 -*-
"""生成应用图标（纯 PIL，无外部素材依赖）。

产出：
  packaging/icons/appicon.png   512x512，供 Linux AppImage / macOS icns 使用
  packaging/icons/appicon.ico   多尺寸，供 Windows 打包使用

运行：python packaging/make_icon.py
"""
from __future__ import annotations

import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
SIZE = 512


def _render() -> Image.Image:
    """深色圆角底 + 一枚受光球体（呼应“智能光照”主题）。"""
    s = SIZE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方形背景（深蓝灰渐变）
    pad, radius = int(s * 0.06), int(s * 0.22)
    grad = Image.new("RGB", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        k = y / (s - 1)
        gd.line([(0, y), (s, y)],
                fill=(int(22 + 14 * k), int(28 + 18 * k), int(38 + 26 * k)))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, s - pad - 1, s - pad - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # 球体：从左上受光的漫反射渐变
    cx, cy = s * 0.5, s * 0.54
    r = s * 0.30
    lx, ly, lz = -0.55, -0.62, 0.56          # 指向光源
    nm = math.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / nm, ly / nm, lz / nm

    yy, xx = np.mgrid[0:s, 0:s].astype(np.float32)
    nx = (xx - cx) / r
    ny = (yy - cy) / r
    d2 = nx * nx + ny * ny
    inside = d2 <= 1.0
    nz = np.sqrt(np.clip(1.0 - d2, 0, 1))
    ndl = np.clip(nx * lx + ny * ly + nz * lz, 0, 1)

    # 主光（暖白） + 环境光（冷蓝）
    base = np.array([214, 226, 245], np.float32)
    amb = np.array([46, 60, 86], np.float32)
    lit = amb[None, None, :] + ndl[..., None] * (base - amb)[None, None, :]
    # 高光
    spec = np.clip(ndl, 0, 1) ** 46
    lit = np.clip(lit + spec[..., None] * 190, 0, 255)
    # 边缘暗化（菲涅尔式收边，避免死板白饼）
    edge = np.clip(nz, 0, 1) ** 0.6
    lit = lit * (0.62 + 0.38 * edge[..., None])

    rgba = np.zeros((s, s, 4), np.float32)
    rgba[..., :3] = lit
    rgba[..., 3] = np.where(inside, 255.0, 0.0)
    # 反锯齿：球体边缘 1.5px 过渡
    alpha = np.clip((1.0 - np.sqrt(d2)) * r / 1.5, 0, 1)
    rgba[..., 3] = np.where(d2 <= 1.6, alpha * 255.0, 0.0)
    rgba[..., :3] = np.where(inside[..., None], rgba[..., :3], 0)

    sphere = Image.fromarray(rgba.astype(np.uint8), "RGBA")
    img.alpha_composite(sphere)

    # 底部投影，让球体“落”在面板上
    sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse(
        [cx - r * 1.02, cy + r * 0.92, cx + r * 1.02, cy + r * 1.16],
        fill=(0, 0, 0, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(10))
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.alpha_composite(sh)
    out.alpha_composite(img)
    return out


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    icon = _render()
    png = os.path.join(OUT_DIR, "appicon.png")
    icon.save(png)
    ico = os.path.join(OUT_DIR, "appicon.ico")
    icon.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print("已生成：", png)
    print("已生成：", ico)


if __name__ == "__main__":
    main()
