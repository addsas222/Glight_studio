# -*- coding: utf-8 -*-
"""渲染管线编排：AI 深度获取 → 物理渲染 → 输出。

为 GUI（QThread）与 GIMP 插件提供统一的高层接口与进度回调。
所有 AI 调用超时自动降级，永不阻塞抛出。
"""
from __future__ import annotations

import io
import time
from typing import Callable, Optional

import numpy as np
from PIL import Image

from .ai_backend import AIBackendConfig, AIPreprocessor, encode_png
from .cache import AICache
from .render import render
from .types import RenderParams

MIN_SIZE, MAX_SIZE = 64, 4096


def load_image(path: str) -> np.ndarray:
    img = Image.open(path)
    img = img.convert("RGBA")
    a = np.asarray(img)
    # 白底合成透明区（动漫立绘常见）
    if a.shape[2] == 4:
        alpha = a[..., 3:4].astype(np.float32) / 255.0
        rgb = (a[..., :3].astype(np.float32) * alpha +
               255.0 * (1 - alpha)).astype(np.uint8)
    else:
        rgb = a[..., :3]
    h, w = rgb.shape[:2]
    if not (MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE <= h <= MAX_SIZE):
        raise ValueError(f"图片尺寸 {w}x{h} 超出支持范围（{MIN_SIZE}~{MAX_SIZE}px）")
    return rgb


class Pipeline:
    def __init__(self, config: Optional[AIBackendConfig] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config = config or AIBackendConfig()
        self.log = log or (lambda m: None)
        self.pre = AIPreprocessor(self.config, AICache(), self.log)

    def run(self, rgb: np.ndarray, params: RenderParams,
            progress: Optional[Callable[[float, str], None]] = None,
            force_backend: Optional[str] = None,
            ) -> dict:
        """返回 {'image': np.ndarray uint8, 'depth': ..., 'backend': str,
                 'elapsed': float}"""
        t0 = time.time()
        progress = progress or (lambda p, m: None)

        progress(0.02, "获取深度数据")
        res = self.pre.get_depth(rgb, encode_png(rgb), force_backend)
        depth = res["depth"]
        normal = res.get("normal")
        progress(0.12, f"深度就绪（{res['backend']}）")

        out = render(rgb, depth, params, progress, normal)
        elapsed = time.time() - t0
        self.log(f"渲染完成：{elapsed:.1f}s，后端 {res['backend']}")
        return {"image": out, "depth": depth, "normal": normal,
                "backend": res["backend"],
                "from_cache": res["from_cache"], "elapsed": elapsed}

    def run_file(self, in_path: str, out_path: str, params: RenderParams,
                 progress=None, force_backend=None) -> dict:
        rgb = load_image(in_path)
        res = self.run(rgb, params, progress, force_backend)
        Image.fromarray(res["image"]).save(out_path)
        res["input"] = rgb
        return res
