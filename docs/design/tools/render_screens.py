# -*- coding: utf-8 -*-
"""按 docs/design/screens.json 批量渲染设计稿 PNG（.pen → HTML → 无头 Edge 截图）。

为什么需要它：屏幕节点 id 是 gen_screens.py 每次随机生成的，渲染命令必须从
screens.json 取 id，否则只能靠人肉从 stdout 抄（抄错就是 KeyError: '<id>'）。

前置：无头 Edge 已带调试端口启动（见 HANDOFF §7 #7）：
    msedge.exe --headless=new --disable-gpu --hide-scrollbars \
      --remote-debugging-port=9222 --user-data-dir=/tmp/shoot-profile \
      --no-first-run --no-default-browser-check about:blank

用法：.venv/Scripts/python.exe docs/design/tools/render_screens.py            # 全部 11 张
      .venv/Scripts/python.exe docs/design/tools/render_screens.py --only B16 # 只渲染一张
"""
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = r"C:/Users/54301/Downloads/Glight_studio"
PEN = ROOT + "/docs/design/凌日光影棚-设计稿.pen"
INDEX = ROOT + "/docs/design/screens.json"
OUT_DIR = ROOT + "/docs/design/b"
TMP = os.path.join(tempfile.gettempdir(), "hls_shots")


def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper()

    screens = json.load(io.open(INDEX, encoding="utf-8"))["screens"]
    os.makedirs(TMP, exist_ok=True)

    pairs = []
    for s in screens:
        tag = os.path.basename(s["png"]).split("-")[0]
        if only and tag != only:
            continue
        html = os.path.join(TMP, tag + ".html")
        subprocess.check_call([sys.executable, ROOT + "/docs/design/tools/pen2html.py",
                               PEN, s["id"], html])
        pairs += ["file:///" + html.replace("\\", "/"),
                  os.path.join(OUT_DIR, s["png"])]

    if not pairs:
        print("没有匹配的屏幕")
        return

    subprocess.check_call(["node", ROOT + "/docs/design/tools/shot_url.mjs",
                           "1440", "900"] + pairs)


if __name__ == "__main__":
    main()
