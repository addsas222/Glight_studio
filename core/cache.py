# -*- coding: utf-8 -*-
"""AI 分析结果持久化缓存：JSON 元数据 + PNG 深度/法线，按图片内容哈希键控。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from typing import Optional

import numpy as np
from PIL import Image

from .paths import subdir

CACHE_DIR_DEFAULT = subdir("cache")

# 缓存容量上限。视频逐帧分析会把**每一帧**的深度/法线都写进来（1080p 一帧
# 约 4MB），不设上限时处理几段长片就能吃掉几十 GB 系统盘。默认 2GB，可用
# HLS_CACHE_MAX_MB 覆盖（设为 0 或负数 = 不限）。
CACHE_MAX_BYTES_DEFAULT = 2 * 1024 ** 3

# 两次容量检查之间的最小间隔（秒）。逐帧处理时每帧都会 put，每次都全量扫目录
# 代价太高，故做节流。节流状态必须是**模块级**的：AICache 是每次处理都新建的
# （见 core/video.py、webui/api.py），实例级计数会永远从 0 开始，等于没节流。
_EVICT_INTERVAL = 30.0
_last_evict_check = 0.0


def cache_max_bytes() -> int:
    """缓存容量上限（字节）；0 表示不限。"""
    raw = os.environ.get("HLS_CACHE_MAX_MB", "").strip()
    if not raw:
        return CACHE_MAX_BYTES_DEFAULT
    try:
        mb = float(raw)
    except ValueError:
        return CACHE_MAX_BYTES_DEFAULT
    return 0 if mb <= 0 else int(mb * 1024 * 1024)


class AICache:
    def __init__(self, cache_dir: Optional[str] = None):
        # 目录在实例化时解析，便于测试注入临时目录
        self.cache_dir = cache_dir or CACHE_DIR_DEFAULT
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def key_for(image_bytes: bytes, backend_id: str, model_hint: str = "") -> str:
        h = hashlib.sha256(image_bytes).hexdigest()[:24]
        return f"{h}_{hashlib.sha1((backend_id + model_hint).encode()).hexdigest()[:8]}"

    def _dir(self, key: str) -> str:
        return os.path.join(self.cache_dir, key)

    def get(self, key: str) -> Optional[dict]:
        """返回 {'depth': np.ndarray, 'normal': np.ndarray|None, 'meta': dict} 或 None。"""
        d = self._dir(key)
        meta_path = os.path.join(d, "meta.json")
        depth_path = os.path.join(d, "depth.png")
        if not (os.path.isfile(meta_path) and os.path.isfile(depth_path)):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            depth = np.asarray(Image.open(depth_path), np.float32) / 255.0
            normal = None
            normal_path = os.path.join(d, "normal.png")
            if os.path.isfile(normal_path):
                n = np.asarray(Image.open(normal_path), np.float32)[..., :3]
                normal = np.clip(n / 255.0 * 2.0 - 1.0, -1, 1)
            meta["depth"] = depth
            meta["normal"] = normal
            meta["from_cache"] = True
            try:
                os.utime(d)      # 触碰条目目录的时间戳 = 标记「最近使用」
            except OSError:
                pass
            return meta
        except Exception:
            return None

    def put(self, key: str, depth: np.ndarray, meta: dict,
            normal: Optional[np.ndarray] = None) -> None:
        d = self._dir(key)
        os.makedirs(d, exist_ok=True)
        Image.fromarray((np.clip(depth, 0, 1) * 255).astype(np.uint8)).save(
            os.path.join(d, "depth.png"))
        if normal is not None:
            Image.fromarray(((np.clip(normal[..., :3], -1, 1) * 0.5 + 0.5) *
                             255).astype(np.uint8)).save(
                os.path.join(d, "normal.png"))
        meta = dict(meta)
        meta.pop("depth", None)
        meta.pop("normal", None)
        meta["cached_at"] = time.time()
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        self._maybe_prune()

    @staticmethod
    def _entry_size(path: str) -> int:
        total = 0
        try:
            with os.scandir(path) as it:
                for f in it:
                    try:
                        total += f.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def size_bytes(self) -> int:
        """缓存目录当前占用（只统计条目目录内的文件）。"""
        total = 0
        try:
            with os.scandir(self.cache_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        total += self._entry_size(entry.path)
        except OSError:
            pass
        return total

    def prune(self, max_bytes: Optional[int] = None) -> tuple:
        """按「最久未使用」淘汰条目，直到占用回落到上限以内。

        条目的最近使用时间取**目录** mtime：get() 命中时会 os.utime 触碰它，
        put() 写入文件时也会刷新它，因此它天然等于「最后一次读或写」的时刻。

        返回 (被删除的条目数, 释放的字节数)；未超限或上限 <= 0 时不做任何事。
        """
        cap = cache_max_bytes() if max_bytes is None else int(max_bytes)
        if cap <= 0:
            return (0, 0)
        entries = []
        total = 0
        try:
            with os.scandir(self.cache_dir) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        mtime = 0.0
                    size = self._entry_size(entry.path)
                    entries.append((mtime, entry.path, size))
                    total += size
        except OSError:
            return (0, 0)
        if total <= cap:
            return (0, 0)
        # 淘汰到 90%：留点余量，免得刚淘汰完又被下一帧写超、反复扫目录
        target = int(cap * 0.9)
        entries.sort(key=lambda e: e[0])          # 最久未用的排最前
        removed = freed = 0
        for _mtime, path, size in entries:
            if total - freed <= target:
                break
            shutil.rmtree(path, ignore_errors=True)
            # ignore_errors 不报错也不代表删干净了（文件可能正被别的线程读取），
            # 所以按**实际**剩下的占用记账；删不动就跳过，继续试下一条
            left = self._entry_size(path)
            if left < size:
                removed += 1
                freed += size - left
        return (removed, freed)

    def _maybe_prune(self) -> None:
        """写入后按需做容量守护（节流见 _EVICT_INTERVAL）。"""
        global _last_evict_check
        now = time.time()
        if now - _last_evict_check < _EVICT_INTERVAL:
            return
        _last_evict_check = now
        try:
            self.prune()
        except Exception:
            # 淘汰只是顺手清理，任何意外都不该让刚写好的缓存条目变成一次失败
            pass

    def clear(self) -> int:
        n = 0
        for name in os.listdir(self.cache_dir):
            p = os.path.join(self.cache_dir, name)
            if os.path.isdir(p):
                for fn in os.listdir(p):
                    os.remove(os.path.join(p, fn))
                os.rmdir(p)
                n += 1
        return n
