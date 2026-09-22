# -*- coding: utf-8 -*-
"""核心数据类型：光源、渲染参数、预设。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Light:
    """单个光源。

    方向约定（方向光）：direction = 指向光源的单位向量，
    (dx, dy) 为图像平面内分量（dx 向右为正，dy 向下为正），
    dz 为垂直于图像平面向外（朝向观察者）为正，取值 0.05~1。
    （点光源）position：图像归一化坐标 (x, y) 与高度 z（0~3，1 约等于图宽）。
    """

    name: str = "主光源"
    kind: str = "directional"          # directional | point
    # 方向光参数
    dx: float = -0.5
    dy: float = -0.6
    dz: float = 0.7
    # 点光源参数（归一化图像坐标）
    px: float = 0.5
    py: float = 0.35
    pz: float = 1.2
    intensity: float = 1.0             # 0~4
    kelvin: float = 5500.0             # 色温 1500~12000K
    radius: float = 0.35               # 点光衰减半径 / 半影尺度 0.05~1.5
    visible: bool = True
    group: str = ""                    # 光源分组名（界面分组/批量操作；渲染不使用）

    def direction_vector(self) -> tuple:
        """返回指向光源的单位方向向量。"""
        import math
        n = math.sqrt(self.dx * self.dx + self.dy * self.dy + self.dz * self.dz) or 1.0
        return (self.dx / n, self.dy / n, max(self.dz / n, 0.02))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Light":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RenderParams:
    """一次渲染的全部参数。"""

    lights: List[Light] = field(default_factory=lambda: [Light()])
    ambient_intensity: float = 0.45     # 0~2，环境光独立调节
    ambient_kelvin: float = 6500.0
    shadow_mode: str = "soft"           # hard=硬朗(日系赛璐珞) soft=柔化半影(摄影棚)
    lighting_mode: str = "linear"       # linear=快速预览(线性叠加) hq=高质量物理阴影
    preview_size: int = 512             # 快速预览的光照计算分辨率上限
    lighting_size: int = 1024           # 高质量模式的光照计算分辨率上限
    shadow_strength: float = 0.85       # 0~1
    specular_strength: float = 0.35     # 高光 0~1
    tone_preserve: float = 0.6          # 0~1 保留原图固有明暗结构的比例
    exposure: float = 1.0               # 0.2~2.5 整体曝光
    reference_offset: Optional[dict] = None  # 参考图迁移偏移（由 reference.py 生成）

    def to_json(self) -> str:
        d = asdict(self)
        d["lights"] = [l.to_dict() for l in self.lights]
        return json.dumps(d, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "RenderParams":
        d = json.loads(s)
        lights = [Light.from_dict(l) for l in d.pop("lights", [])]
        p = cls(**d)
        p.lights = lights
        return p

    @classmethod
    def from_dict(cls, d: dict) -> "RenderParams":
        """由字典构造（忽略多余/历史字段，缺失字段取默认值）。"""
        d = dict(d or {})
        raw_lights = d.pop("lights", None)
        known = {f for f in cls.__dataclass_fields__}
        p = cls(**{k: v for k, v in d.items() if k in known})
        p.lights = [Light.from_dict(l) for l in (raw_lights or [])] or [Light()]
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lights"] = [l.to_dict() for l in self.lights]
        return d
