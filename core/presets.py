# -*- coding: utf-8 -*-
"""5 种预设场景：标准棚拍、逆光黄昏、冷色月夜、舞台聚光、霓虹氛围。"""
from __future__ import annotations

from .types import Light, RenderParams


def _p(lights, amb_i, amb_k, shadow="soft", **kw) -> RenderParams:
    return RenderParams(lights=lights, ambient_intensity=amb_i,
                        ambient_kelvin=amb_k, shadow_mode=shadow, **kw)


PRESETS = {
    "标准棚拍": lambda: _p(
        [Light(name="主光（柔光箱）", dx=-0.4208, dy=-0.6079, dz=0.6733,
               intensity=0.7, kelvin=5500, radius=0.5),
         Light(name="轮廓补光", dx=0.8179, dy=-0.2337, dz=0.5258,
               intensity=0.3, kelvin=6800, radius=0.6)],
        amb_i=0.55, amb_k=6500),

    "逆光黄昏": lambda: _p(
        [Light(name="夕阳逆光", kind="point", px=0.82, py=0.12, pz=0.35,
               intensity=1.5, kelvin=2900, radius=0.8),
         Light(name="天光补光", dx=-0.3328, dy=0.4438, dz=0.8321,
               intensity=0.3, kelvin=7500, radius=0.7)],
        amb_i=0.42, amb_k=4200, shadow="soft", specular_strength=0.45),

    "冷色月夜": lambda: _p(
        [Light(name="月光", dx=0.3522, dy=-0.7547, dz=0.5535,
               intensity=0.95, kelvin=9800, radius=0.4),
         Light(name="地面反光", dx=-0.2233, dy=0.8930, dz=0.3907,
               intensity=0.25, kelvin=8200, radius=0.8)],
        amb_i=0.28, amb_k=9500, shadow="hard", specular_strength=0.4,
        exposure=0.95),

    "舞台聚光": lambda: _p(
        [Light(name="顶部聚光", kind="point", px=0.5, py=-0.05, pz=1.4,
               intensity=1.4, kelvin=5200, radius=0.35),
         Light(name="左侧染色灯", kind="point", px=0.05, py=0.5, pz=0.9,
               intensity=0.7, kelvin=3200, radius=0.5),
         Light(name="右侧染色灯", kind="point", px=0.95, py=0.5, pz=0.9,
               intensity=0.7, kelvin=12000, radius=0.5)],
        amb_i=0.18, amb_k=6500, shadow="hard", specular_strength=0.55),

    "霓虹氛围": lambda: _p(
        [Light(name="霓虹粉（左）", kind="point", px=0.08, py=0.35, pz=0.6,
               intensity=1.1, kelvin=2100, radius=0.7),
         Light(name="霓虹青（右）", kind="point", px=0.92, py=0.45, pz=0.6,
               intensity=1.1, kelvin=15000, radius=0.7),
         Light(name="顶部白光", dx=0.0000, dy=-0.8742, dz=0.4856,
               intensity=0.4, kelvin=6500, radius=0.5)],
        amb_i=0.25, amb_k=8000, shadow="soft", specular_strength=0.6),
}

PRESET_NAMES = list(PRESETS.keys())


def get_preset(name: str) -> RenderParams:
    return PRESETS[name]()
