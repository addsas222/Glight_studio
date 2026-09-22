# -*- coding: utf-8 -*-
"""用户数据目录与旧版本迁移。

数据目录：`~/.horizon_light_studio`
旧目录（产品更名前）：`~/.gacha_light_studio`

首次访问数据目录时自动把旧目录迁移过来，避免用户已下载的深度模型
（约 64MB）与 AI 缓存（可能数百 MB）因更名而失效。
迁移失败不会阻断启动，旧目录保持原样。
"""
from __future__ import annotations

import os
import shutil

DATA_DIR_NAME = ".horizon_light_studio"
LEGACY_DIR_NAME = ".gacha_light_studio"

_migrated = False


def home() -> str:
    return os.path.expanduser("~")


def data_dir() -> str:
    """数据目录（存在则保证已完成迁移）。"""
    _ensure_migrated()
    return os.path.join(home(), DATA_DIR_NAME)


def legacy_data_dir() -> str:
    return os.path.join(home(), LEGACY_DIR_NAME)


def subdir(*parts: str) -> str:
    """数据目录下的子路径，例如 subdir("cache")。"""
    return os.path.join(data_dir(), *parts)


def _ensure_migrated() -> None:
    global _migrated
    if _migrated:
        return
    _migrated = True
    new = os.path.join(home(), DATA_DIR_NAME)
    old = os.path.join(home(), LEGACY_DIR_NAME)
    if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(new):
        return
    try:
        if not os.path.exists(new):
            os.makedirs(os.path.dirname(new) or ".", exist_ok=True)
            shutil.move(old, new)
            return
        # 两边都存在：只搬新目录里还没有的子项，不覆盖用户的新数据
        for name in os.listdir(old):
            src = os.path.join(old, name)
            dst = os.path.join(new, name)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    except Exception:
        pass
