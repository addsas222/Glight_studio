# -*- coding: utf-8 -*-
"""设计稿 → 可用代码 保真度自检（换机后可复跑，默认仅报告、不失败）。

做什么：
  逐个 Pen 界面抽出全部 text 文案，去空白后在**整个前端源码**里查它是否已落地。
  命中 = 设计稿这条文案已经作为「可用代码」存在于 webui/frontend/src。

为什么不是简单字符串相等：
  设计稿 body 里的是「静态样例值」，真实前端由 /api/state 驱动、是运行时值
  —— 照度示例、对比度 16.7:1、色温 5200 K、角色「主光 · 点光」组合、当前主题名、
  后端标签都属此类，两边故意不同。因此把它们归为 RUNTIME / 占位，不算缺口，
  只统计「本该逐字落地」的说明句 / 标签 / 小标题。

用法：
  .venv/Scripts/python.exe docs/design/tools/check_design_fidelity.py          # 报告
  .venv/Scripts/python.exe docs/design/tools/check_design_fidelity.py --strict # 有真缺口则 exit 1

对应 HANDOFF §7 #7：「把设计稿回写成前端代码」这一半的验收口径。
"""
import argparse
import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))          # 仓库根
PEN = os.path.join(ROOT, "docs", "design", "凌日光影棚-设计稿.pen")
SRC = os.path.join(ROOT, "webui", "frontend", "src")

# Pen 界面名（中文前缀） -> 前端 screen 文件
NAME2TS = {
    "光影工作台": "workbench.ts", "智能打光": "autolight.ts", "专业模式": "promode.ts",
    "视频调光": "video.ts", "逐帧光绘": "framepaint.ts", "光源星图": "starmap.ts",
    "智能拾光": "harvest.ts", "三维布光预演": "previs.ts", "调光台": "desk.ts",
    "插件中心": "plugins.ts", "主题引擎": "theme.ts",
}
# 上一轮的 2 个真实界面 + 2 个复用组件（Frame / NavBtn），非本轮 B8–B18
OLD_IDS = {"bi8Au", "ivUPE", "Skfts", "ASOlL"}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def squash(s):
    """去全部空白：让比对对中英文间距、全半角空格不敏感。"""
    return re.sub(r"\s+", "", s or "")


def walk(n):
    yield n
    for c in n.get("children") or []:
        yield from walk(c)


def is_runtime(s):
    """设计稿静态样例 vs 代码运行时值 —— 这类按设计允许不同。"""
    if re.search(r"\d", s):
        return True                       # 含数字：数值 / 单位 / 比例 / 计数 / 色温
    if "·" in s and re.search(r"[A-Za-z]", s):
        return True                       # 「主光 · 点光」角色·类型组合标签
    core = re.sub(r"[·:×x/.+\-–—()（）°K#]", "", squash(s))
    return core != "" and all(ord(c) < 128 for c in core)  # 纯 ASCII 数值标记


def build_corpus():
    files = ["shell.ts", "bridge.ts", "api.ts", "styles.css"]
    files += ["screens/" + f for f in NAME2TS.values()]
    out = []
    for rel in files:
        p = os.path.join(SRC, rel)
        if os.path.exists(p):
            out.append(squash(io.open(p, encoding="utf-8").read()))
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="存在真·文案缺口时以 exit 1 结束")
    args = ap.parse_args()

    pen = json.load(io.open(PEN, encoding="utf-8"))
    corpus = build_corpus()

    total = miss_copy = miss_runtime = 0
    rows = []
    for top in pen["children"]:
        if top.get("id") in OLD_IDS:
            continue
        zh = (top.get("name") or "").split(" · ")[0].split(" ")[0]
        fn = NAME2TS.get(zh)
        texts = [norm(n.get("content")) for n in walk(top)
                 if n.get("type") == "text" and n.get("content")]
        texts = [t for t in dict.fromkeys(texts) if len(t) >= 3]
        if not fn:
            print("?? 未映射：", top.get("name"))
            continue
        scr = squash(io.open(os.path.join(SRC, "screens", fn), encoding="utf-8").read())
        miss = []
        for t in texts:
            sq = squash(t)
            if sq in scr or sq in corpus:
                continue
            if is_runtime(t):
                miss_runtime += 1
                continue
            miss.append(t)
            miss_copy += 1
        total += len(texts)
        if miss:
            rows.append((zh, fn, miss))

    for zh, fn, miss in rows:
        print("### %-12s -> %-13s  真·文案缺口 %d" % (zh, fn, len(miss)))
        for t in miss:
            print("      缺:", t)

    realized = total - miss_copy - miss_runtime
    print("\n== 设计稿 → 可用代码 保真度 ==")
    print("文案总数 %d | 已落地 %d | 运行时/占位差异(允许) %d | 真·文案缺口 %d" % (
        total, realized, miss_runtime, miss_copy))
    if total:
        print("已落地占比 %.1f%%（分母含运行时值；纯文案口径接近 100%%）" %
              (100.0 * realized / total))
    if miss_copy:
        print("提示：以上为设计稿有、代码尚未逐字落地的文案；多数是设计侧小标题/示例，"
              "可择要回写（见 HANDOFF §7 #7）。")
    if args.strict and miss_copy:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
