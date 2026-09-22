# -*- coding: utf-8 -*-
"""GIMP 插件调用的命令行渲染入口。

用法：
  python hls_cli.py [--preset 名称] [--shadow soft|hard] [--lighting linear|hq]
                    [--ambient 0.45] [--exposure 1.0]
                    [--preview-only] [--backend ...] 输入图 输出图

也被 PyInstaller 打包为 hls_cli.exe 供 GIMP 插件调用。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.ai_backend import AIBackendConfig
from core.pipeline import Pipeline
from core.presets import get_preset


def main():
    ap = argparse.ArgumentParser(description="凌日光影棚 CLI")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--preset", default="标准棚拍")
    ap.add_argument("--shadow", default="soft", choices=["soft", "hard"])
    ap.add_argument("--lighting", default="linear",
                    choices=["linear", "hq"],
                    help="linear=快速预览（线性叠加） hq=高质量（物理阴影）")
    ap.add_argument("--ambient", type=float, default=None)
    ap.add_argument("--exposure", type=float, default=None)
    ap.add_argument("--preview-only", action="store_true",
                    help="仅预览模式：跳过全部 AI 后端，完全离线")
    ap.add_argument("--backend", default=None,
                    help="强制指定 AI 后端：builtin/local/cloud/simulate")
    args = ap.parse_args()

    from core.presets import PRESETS
    params = get_preset(args.preset) if args.preset in PRESETS \
        else get_preset("标准棚拍")
    params.shadow_mode = args.shadow
    params.lighting_mode = args.lighting
    if args.ambient is not None:
        params.ambient_intensity = args.ambient
    if args.exposure is not None:
        params.exposure = args.exposure

    # CLI 模式下默认禁用云端（避免在 GIMP 内因联网卡顿）
    cfg = AIBackendConfig.load()
    cfg.cloud_enabled = False
    if args.preview_only:
        cfg.preview_only = True
    pl = Pipeline(cfg, log=lambda m: print("[hls]", m, file=sys.stderr))
    res = pl.run_file(args.input, args.output, params,
                      force_backend=args.backend)
    print("OK backend=%s elapsed=%.1fs" % (res["backend"], res["elapsed"]))


if __name__ == "__main__":
    main()
