# -*- coding: utf-8 -*-
"""把其余 11 个界面追加进 凌日光影棚-设计稿.pen。

克隆「暗房终端」(Skfts) 的外壳（顶栏 sx6D3 / 导航栏 QeFKw / 状态栏 a86zuX），
换上全新 id、激活对应的导航按钮，再按真实前端文案（webui/frontend/src/）挂载
每个界面的主体内容。诚实性脚注逐字保留：任何界面都不显示
照度 lx / CRI / Δuv / 频闪 / DMX / 灯具温度这类引擎算不出的量。

用法：.venv/Scripts/python.exe docs/design/tools/gen_screens.py
渲染：python docs/design/tools/pen2html.py <pen> <screenId> <html>
      node docs/design/tools/shot_url.mjs 1440 900 file:///<html> <png>
      （screenId 从 docs/design/screens.json 取：名称 → id / PNG 文件名）
"""
import io
import json
import random
import string

PEN = r"C:/Users/54301/Downloads/Glight_studio/docs/design/凌日光影棚-设计稿.pen"
SCREEN_INDEX = r"C:/Users/54301/Downloads/Glight_studio/docs/design/screens.json"

HONEST_BRIDGE = "强度 0–4 为引擎相对标度；照度 lx / CRI / Δuv / 频闪 / DMX / 灯具温度本引擎无法计算。"
HONEST_DESK = "强度/色温为引擎相对标度（0–4 · K）；照度 lx、CRI、Δuv、DMX、灯具温度本引擎无法计算，故不显示。"

# ---------------------------------------------------------------- id 生成

_ALPHABET = string.ascii_letters + string.digits
_USED = set()


def nid():
    while True:
        s = "".join(random.choice(_ALPHABET) for _ in range(5))
        if s not in _USED:
            _USED.add(s)
            return s


def seed_ids(doc):
    def walk(n):
        if isinstance(n, dict):
            if n.get("id"):
                _USED.add(n["id"])
            for c in n.get("children") or []:
                walk(c)

    for top in doc.get("children", []):
        walk(top)


# ---------------------------------------------------------------- 节点构造

def frame(name, kids, **kw):
    n = {"type": "frame", "id": nid(), "name": name, "children": list(kids)}
    for k, v in kw.items():
        if v is not None:
            n[k] = v
    return n


def text(content, size=11, weight="normal", fill="$text-secondary", ls=None, mono=False):
    n = {"type": "text", "id": nid(), "name": str(content)[:10],
         "fill": fill, "content": content,
         "fontFamily": "$font-mono" if mono else "Inter",
         "fontSize": size, "fontWeight": weight, "children": []}
    if ls is not None:
        n["letterSpacing"] = ls
    return n


def icon(icon_name, size=14, fill="$text-secondary"):
    return {"type": "icon", "id": nid(), "name": "图标 " + icon_name,
            "width": size, "height": size, "icon": icon_name,
            "library": "lucide", "fill": fill, "children": []}


def rect(w, h, fill="$border", radius=None, stroke=None, sw=None):
    n = {"type": "rectangle", "id": nid(), "name": "矩形",
         "width": w, "height": h, "fill": fill, "children": []}
    if radius is not None:
        n["cornerRadius"] = radius
    if stroke is not None:
        n["stroke"] = stroke
        n["strokeWidth"] = sw if sw is not None else 1
    return n


def dot(fill, size=7, stroke=None):
    n = {"type": "ellipse", "id": nid(), "name": "圆点",
         "width": size, "height": size, "fill": fill, "children": []}
    if stroke is not None:
        n["stroke"] = stroke
        n["strokeWidth"] = 1
    return n


def ellipse_grad(w, h, c0, c1, rotation=225, stroke=None):
    n = {"type": "ellipse", "id": nid(), "name": "渐变圆",
         "width": w, "height": h,
         "fill": {"type": "gradient", "gradientType": "linear", "enabled": True,
                  "rotation": rotation, "size": {"height": 1},
                  "colors": [{"color": c0, "position": 0},
                             {"color": c1, "position": 1}]},
         "children": []}
    if stroke is not None:
        n["stroke"] = stroke
        n["strokeWidth"] = 1
    return n


# ---------------------------------------------------------------- 通用组件

def spacer(w="fill_container", h=1):
    return frame("弹性", [], width=w, height=h)


CHIP_STYLE = {
    "default": ("$bg-elevated", "$border", "$text-secondary"),
    "accent": ("$accent-tint", "$accent-line", "$accent-bright"),
    "success": ("#34d3991f", "#34d39955", "$success"),
    "warm": ("#ffa94d1f", "#ffa94d55", "$warm"),
    "violet": ("#a78bfa1f", "#a78bfa55", "$violet"),
    "danger": ("#f871711a", "#f8717155", "#f87171"),
}


def chip(content, kind="default", icon_name=None, with_dot=False):
    bg, line, fg = CHIP_STYLE[kind]
    kids = []
    if with_dot:
        kids.append(dot(fg))
    if icon_name:
        kids.append(icon(icon_name, 12, fg))
    kids.append(text(content, 10.5, "normal", fg))
    # width=fit_content：chip 是内联元素，必须按内容取宽。
    # 不给的话，直接放进竖排卡片里会被拉伸成整行色条（pen2html 已支持该取值）。
    return frame("Chip " + str(content)[:6], kids, width="fit_content",
                 fill=bg, cornerRadius=999,
                 stroke=line, strokeWidth=1, gap=6, padding=[3, 9], alignItems="center")


def btn(content, kind="ghost", icon_name=None, w=None):
    if kind == "primary":
        bg, fg, line = "$accent", "#ffffff", "$accent-line"
    elif kind == "danger":
        bg, fg, line = "#f871711a", "#f87171", "#f8717155"
    else:
        bg, fg, line = "$bg-elevated", "$text-secondary", "$border"
    kids = []
    if icon_name:
        kids.append(icon(icon_name, 13, fg))
    kids.append(text(content, 11, "normal", fg))
    return frame("按钮 " + str(content)[:6], kids, height=26, width=w, fill=bg,
                 cornerRadius=6, stroke=line, strokeWidth=1, gap=6, padding=[0, 10],
                 alignItems="center")


def card(title, kids, head_chip=None, head_icon=None, body_pad=(10, 14),
         body_gap=8, height=None, fill_h=False):
    head_kids = []
    if head_icon:
        head_kids.append(icon(head_icon, 13, "$text-secondary"))
    head_kids.append(text(title, 11.5, "600", "$text-primary"))
    head_kids.append(spacer())
    if head_chip is not None:
        head_kids.append(head_chip)
    head = frame("卡头 " + title[:6], head_kids, width="fill_container", height=32,
                 fill="$bg-elevated", gap=8, padding=[0, 14], alignItems="center")
    body_kw = {"width": "fill_container", "layout": "vertical", "gap": body_gap,
               "padding": list(body_pad)}
    card_kw = {"width": "fill_container", "layout": "vertical", "clip": True,
               "fill": "$bg-panel", "cornerRadius": 8, "stroke": "$border",
               "strokeWidth": 1}
    if height is not None:
        card_kw["height"] = height
        body_kw["height"] = "fill_container"
    elif fill_h:
        card_kw["height"] = "fill_container"
        body_kw["height"] = "fill_container"
    body = frame("卡体 " + title[:6], list(kids), **body_kw)
    divider = rect("fill_container", 1, "$border")
    return frame("卡片 " + title[:6], [head, divider, body], **card_kw)


def kv(label, value, mono=True):
    return frame("kv " + str(label)[:6],
                 [text(label, 10.5, "normal", "$text-muted"), spacer(),
                  text(value, 10.5, "normal", "$text-secondary", mono=mono)],
                 width="fill_container", height=16, gap=8, alignItems="center")


def track(pct, total=250, h=4):
    w_filled = max(2, int(round(total * pct / 100.0)))
    return frame("进度条",
                 [rect(w_filled, h, "$accent", radius=2),
                  rect(max(0, total - w_filled), h, "$bg-canvas", radius=2)],
                 width=total, height=h, gap=0)


def slider(label, value, pct, total=250):
    return frame("滑杆 " + str(label)[:6],
                 [frame("滑杆标签",
                        [text(label, 10.5, "normal", "$text-secondary"), spacer(),
                         text(value, 10.5, "normal", "$text-primary", mono=True)],
                        width="fill_container", gap=8, alignItems="center"),
                  frame("滑杆轨道", [track(pct, total)], width="fill_container")],
                 width="fill_container", layout="vertical", gap=5)


def seg(options, active_idx, total=None, fs=10, pad=(0, 8)):
    kids = []
    for i, o in enumerate(options):
        active = (i == active_idx)
        kids.append(frame("分段 " + str(o)[:4],
                          [text(o, fs, "normal",
                                "$accent-bright" if active else "$text-muted")],
                          height=20, fill="$accent-tint" if active else "$bg-elevated",
                          cornerRadius=5, stroke="$accent-line" if active else "$border",
                          strokeWidth=1, padding=list(pad),
                          alignItems="center", justifyContent="center"))
    kw = {"gap": 4, "alignItems": "center", "justifyContent": "center"}
    if total is not None:
        kw["width"] = total
    else:
        kw["width"] = "fill_container"
    return frame("分段组", kids, **kw)


def field(placeholder, h=28, icon_name=None, w="fill_container"):
    kids = []
    if icon_name:
        kids.append(icon(icon_name, 13, "$text-muted"))
    kids.append(text(placeholder, 10.5, "normal", "$text-muted"))
    return frame("输入框", kids, width=w, height=h, fill="$bg-elevated",
                 cornerRadius=6, stroke="$border", strokeWidth=1, gap=6,
                 padding=[0, 10], alignItems="center")


def textarea(placeholder, h=80):
    return frame("多行输入", [text(placeholder, 10.5, "normal", "$text-muted")],
                 width="fill_container", height=h, fill="$bg-elevated",
                 cornerRadius=6, stroke="$border", strokeWidth=1,
                 layout="vertical", padding=[8, 10])


def item(kids, active=False, h=None):
    kw = {"width": "fill_container", "cornerRadius": 6, "gap": 8,
          "padding": [8, 10], "alignItems": "center"}
    if active:
        kw["fill"] = "$accent-tint"
        kw["stroke"] = "$accent-line"
        kw["strokeWidth"] = 1
    if h is not None:
        kw["height"] = h
    return frame("列表项", list(kids), **kw)


def note(s):
    return text(s, 10, "normal", "$text-muted")


def row(kids, **kw):
    return frame("行", list(kids), **kw)


def col(w, kids, fill_h=True, **kw):
    kw2 = {"width": w, "layout": "vertical", "gap": 10}
    if fill_h:
        kw2["height"] = "fill_container"
    kw2.update(kw)
    return frame("栏", list(kids), **kw2)


def header(title, subtitle, right_chip=None):
    kids = [frame("标题组",
                  [text(title, 19, "600", "$text-primary"),
                   text(subtitle, 10.5, "normal", "$text-muted", ls=0.8)],
                  layout="vertical", gap=2),
            spacer()]
    if right_chip is not None:
        kids.append(right_chip)
    return frame("页头", kids, width="fill_container", gap=10, alignItems="center")


def stage(kids, fill_h=True):
    return frame("舞台", list(kids), width="fill_container",
                 height="fill_container" if fill_h else None,
                 fill="$bg-canvas", cornerRadius=8, stroke="$border",
                 strokeWidth=1, layout="vertical", gap=10, padding=12,
                 clip=True, alignItems="center", justifyContent="center")


def preview_box(w, h, caption=None, sub=None, icon_name="image"):
    kids = [icon(icon_name, 26, "#2b3648")]
    if caption:
        kids.append(text(caption, 10.5, "normal", "$text-secondary"))
    if sub:
        kids.append(text(sub, 9.5, "normal", "$text-muted"))
    return frame("预览框", kids, width=w, height=h, fill="$bg-canvas",
                 cornerRadius=8, stroke="$border", strokeWidth=1,
                 layout="vertical", gap=6, alignItems="center",
                 justifyContent="center")


def mini_light(label, color, sub):
    return frame("灯位 " + label[:6],
                 [dot(color, 8),
                  frame("灯位文字",
                        [text(label, 10.5, "600", "$text-primary"),
                         text(sub, 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)],
                 gap=8, alignItems="center")


def light_label(name, color, off=False):
    return frame("星点 " + name[:6],
                 [dot(color, 9, stroke="$border" if off else None),
                  text(name, 9, "normal", "$text-muted")],
                 layout="vertical", gap=3, alignItems="center")


def body_split(*cols):
    return frame("主体分栏", [c for c in cols if c is not None],
                 width="fill_container", height="fill_container", gap=12)


# ---------------------------------------------------------------- 外壳克隆

RAIL_SLOTS = [
    ("光影工作台", "hDDLa", "layers"),
    ("智能打光", "GHlvm", "sparkles"),
    ("专业模式", "uvk3t", "workflow"),
    ("视频调光", "AHM3J", "film"),
    ("逐帧光绘", "xtA0D", "wand"),
    ("光源星图", "HF5mK", "radar"),
    ("智能拾光", "Vuf8E", "sparkles"),
    ("三维布光预演", "Z5ZlAg", "crosshair"),
    ("暗房终端", "M4cPoG", "terminal"),
    ("调光台", "Jidko", "sliders-horizontal"),
    ("光影指挥屏", "ksMVw", "gauge"),
    ("插件中心", "OHEJu", "puzzle"),
    ("主题引擎", "lfvTM", "palette"),
]


def clone(node, overrides=None):
    """深拷贝并换全新 id；overrides 按旧 id 在任意深度覆写属性。"""
    overrides = overrides or {}

    def rec(n):
        out = {}
        for k, v in n.items():
            if k == "children":
                out[k] = [rec(c) for c in (v or [])]
            else:
                out[k] = v
        old_id = n.get("id")
        if old_id and old_id in overrides:
            out.update(overrides[old_id])
        if old_id is not None:
            out["id"] = nid()
        return out

    return rec(node)


def make_shell(by_id, screen_name, rail_label, body_kids, media_text=None,
               size_text=None, status_text="就绪",
               deep_text="深度来源：内置本地引擎", x=0, y=0):
    top_over = {}
    if media_text is not None:
        top_over["myiuC"] = {"content": media_text}
    topbar = clone(by_id["sx6D3"], top_over)

    rail_over = {}
    for label, old_id, ic in RAIL_SLOTS:
        if label == rail_label:
            rail_over[old_id] = {"fill": "$accent-tint",
                                 "descendants": {"kd7ZN": {"icon": ic, "fill": "$accent-bright"},
                                                 "nSrQM": {"content": label, "fill": "$text-primary"}}}
        else:
            rail_over[old_id] = {"fill": "$bg-panel",
                                 "descendants": {"kd7ZN": {"icon": ic, "fill": "$text-muted"},
                                                 "nSrQM": {"content": label, "fill": "$text-muted"}}}
    rail = clone(by_id["QeFKw"], rail_over)

    stat_over = {"i8D4dc": {"content": status_text}}
    if size_text is not None:
        stat_over["j3RK0"] = {"content": size_text}
    if deep_text is not None:
        stat_over["iH491"] = {"content": deep_text}
    statusbar = clone(by_id["a86zuX"], stat_over)

    content = frame("内容", list(body_kids), width="fill_container",
                    height="fill_container", fill="$bg-root",
                    layout="vertical", gap=12, padding=16)
    main = frame("主体", [rail, content], width="fill_container",
                 height="fill_container")
    return frame(screen_name, [topbar, main, statusbar], width=1440, height=900,
                 fill="$bg-root", layout="vertical", clip=True, x=x, y=y)


# ---------------------------------------------------------------- 各屏主体

def light_item(name, color, sub, state, active=False):
    return item([dot(color, 8),
                 frame("灯文字",
                       [text(name, 10.5, "600" if active else "normal",
                             "$text-primary" if active else "$text-secondary"),
                        text(sub, 9.5, "normal", "$text-muted")],
                       layout="vertical", gap=1),
                 spacer(),
                 chip(state, "success" if state == "启用" else "default")],
                active=active)


def build_workbench():
    left = col(264, [
        card("素材库", [
            item([icon("image", 14, "$accent-bright"),
                  frame("素材文字",
                        [text("角色立绘.png", 10.5, "600", "$text-primary"),
                         text("512 × 512 · PNG", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)], active=True),
            item([icon("film", 14, "$text-muted"),
                  frame("素材文字",
                        [text("未载入", 10.5, "normal", "$text-secondary"),
                         text("尚未导入素材", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)]),
            row([btn("导入图片 / 视频", "primary", "upload"),
                 btn("导出", "ghost", "download")], gap=8),
        ], head_icon="folder", head_chip=chip("1 张", "default")),
        card("参考图迁移", [
            preview_box("fill_container", 96, "参考图", "导入参考图提取光照"),
            btn("导入参考图提取光照", "ghost", "upload", w="fill_container"),
            note("仅迁移主光方向与色温，保留原图明暗结构。"),
            row([btn("一键套用", "primary"), btn("撤销迁移", "ghost")], gap=8),
        ], head_icon="image", fill_h=True),
    ])
    center = frame("画布区", [
        stage([preview_box(380, 380, "角色立绘.png",
                           "对比原图：关闭（显示当前渲染）")]),
        row([chip("对比原图", "default", "image"),
             chip("显示深度图", "default", "layers"),
             chip("拾取主光", "accent", "crosshair"),
             spacer(),
             btn("快速预览", "primary", "play"),
             btn("全部重置", "ghost", "rotate")],
            width="fill_container", alignItems="center"),
        note("在画布上点击，按该点的法线方向设置主光"),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)
    right = col(320, [
        card("光照控制", [
            light_item("主光 · 点光", "#ffd54a", "强度 1.40 · 5200 K", "启用", active=True),
            light_item("补光 · 平行光", "#cfe4ff", "强度 0.60 · 6500 K", "启用"),
            light_item("轮廓光 · 点光", "#a78bfa", "强度 0.80 · 8400 K", "停用"),
            row([btn("新增一个光源节点", "ghost", "plus"),
                 btn("删除光源", "ghost", "trash")], gap=8),
        ], head_icon="lightbulb", head_chip=chip("3 光源", "default"), fill_h=True),
        card("光源", [
            seg(["点光", "平行光"], 0, total=272),
            slider("强度", "1.40", 35, total=272),
            slider("半径", "0.60", 40, total=272),
            slider("柔化半影", "0.40", 40, total=272),
            slider("整体曝光", "1.00", 40, total=272),
        ], head_icon="sliders"),
        note(HONEST_BRIDGE),
    ])
    return [header("光影工作台", "素材 · 光源 · 实时预览",
                   chip("本会话素材", "default", "folder")),
            body_split(left, center, right)]


def build_autolight():
    left = col(300, [
        card("AI 预处理后端", [
            row([chip("内置本地引擎", "success", with_dot=True),
                 chip("云端 API", "default"),
                 chip("本地自建服务", "default")], gap=6),
            note("已就绪（未返回法线，由深度近似）"),
        ], head_icon="sparkles"),
        card("光照描述（自然语言）", [
            textarea("例如：黄昏时分的舞台逆光，暖橘色主光从左后方打来，"
                     "冷蓝补光勾勒人物轮廓，空气中有薄雾感。", 118),
            btn("描述 → 光照节点", "primary", "wand", w="fill_container"),
            note("AI 根据文本描述生成 · 参数可无限叠加"),
        ], head_icon="terminal", fill_h=True),
    ])
    center = frame("画布区", [
        stage([preview_box(380, 380, "未载入媒体", "生成的光照节点会叠加到预览")]),
        row([chip("仅预览（灰度模拟）", "warm"),
             chip("图片模式", "default", "image"),
             spacer(),
             btn("应用此光照参数", "primary", "check")],
            width="fill_container", alignItems="center"),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)
    right = col(340, [
        card("光照节点", [
            note("尚未生成：以下为当前参数里的光源，可先用左侧描述生成新的光照节点。"),
            light_item("主光 · 平行光", "#ffd54a", "强度 1.20 · 4800 K", "已启用", active=True),
            light_item("补光 · 点光", "#cfe4ff", "强度 0.50 · 6800 K", "未启用"),
            note("强度条为 0–4 相对标度"),
        ], head_icon="lightbulb", head_chip=chip("未生成", "default"), fill_h=True),
        card("参考图光照迁移", [
            note("仅做方向与色温迁移，保留原图明暗结构。"),
            btn("导入参考图提取主光方向", "ghost", "upload", w="fill_container"),
            chip("已应用参考图光照", "success"),
        ], head_icon="upload"),
    ])
    return [header("智能打光", "描述 → 光照节点",
                   chip("未载入媒体", "default", "film")),
            body_split(left, center, right)]


def build_promode():
    def node_box(title, sub, color, sel=False):
        return frame("节点 " + title[:6],
                     [dot(color, 8),
                      frame("节点文字",
                            [text(title, 10.5, "600", "$text-primary"),
                             text(sub, 9, "normal", "$text-muted")],
                            layout="vertical", gap=1)],
                     width=178, height=60, fill="$bg-elevated", cornerRadius=8,
                     stroke="$accent-line" if sel else "$border",
                     strokeWidth=2 if sel else 1, gap=10, padding=[0, 12],
                     alignItems="center")

    def h_arrow():
        return rect(26, 2, "$border-strong", radius=1)

    def v_arrow():
        return rect(2, 22, "$border-strong", radius=1)

    toolbar = frame("工作流工具条", [
        btn("导入 JSON", "ghost", "upload"),
        btn("导出 JSON", "ghost", "download"),
        rect(1, 20, "$border"),
        btn("定位视图", "ghost", "crosshair"),
        btn("删除", "ghost", "trash"),
        spacer(),
        chip("专业模式工作流", "default", "workflow"),
        btn("求值", "primary", "play"),
    ], width="fill_container", height=44, fill="$bg-panel", cornerRadius=8,
        stroke="$border", strokeWidth=1, gap=8, padding=[0, 12], alignItems="center")

    left = col(248, [
        card("节点库", [
            field("搜索节点…", icon_name="search"),
            item([icon("image", 13, "$accent-bright"),
                  text("图像/视频输入", 10.5, "normal", "$text-primary")], active=True),
            note("光照模式"),
            item([icon("layers", 13, "$text-muted"), text("本地深度 / 法线估计", 10.5)]),
            note("光源类型"),
            item([icon("sun", 13, "$text-muted"), text("新光源", 10.5)]),
            item([icon("lightbulb", 13, "$text-muted"), text("柔光", 10.5)]),
            note("参考"),
            item([icon("image", 13, "$text-muted"), text("参考图保守迁移", 10.5)]),
            item([icon("download", 13, "$text-muted"), text("合成输出", 10.5)]),
        ], head_icon="search", head_chip=chip("分类", "default"), fill_h=True),
    ])

    canvas = frame("节点画布", [
        chip("求值成功，渲染参数已应用", "success", "check"),
        row([node_box("图像/视频输入", "图片", "#6fa8ff"),
             h_arrow(),
             node_box("本地深度 / 法线估计", "光照模式", "#34d399")],
            gap=0, alignItems="center"),
        v_arrow(),
        row([node_box("新光源", "光源类型", "#ffd54a", sel=True),
             h_arrow(),
             node_box("柔光", "光源类型", "#a78bfa")], gap=0, alignItems="center"),
        v_arrow(),
        row([node_box("合成输出 · 交由导出", "导出", "#6fa8ff")], gap=0, alignItems="center"),
        note("在画布上点击一个节点查看并编辑它的参数；点击连线可选中连线。"),
        note("双击画布空白处可快速添加光源节点"),
        note("求值时只使用确定性的离线解析，不会发起网络请求。"),
    ], layout="vertical", gap=8, alignItems="center")

    center = frame("画布区", [stage([canvas])],
                   width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(320, [
        card("属性", [
            kv("名称", "新光源", mono=False),
            text("方向 · 3D 轨迹球", 10.5, "600", "$text-secondary"),
            row([ellipse_grad(64, 64, "#2a4c82", "#0e121a", rotation=225,
                              stroke="$accent-line"),
                 frame("方向读数", [kv("方向 dx", "+0.32"),
                                    kv("方向 dy", "-0.10"),
                                    kv("方向 dz", "+0.94")],
                       layout="vertical", gap=2, width="fill_container")],
                width="fill_container", gap=12, alignItems="center"),
            slider("强度", "1.40", 35, total=276),
            slider("半径", "0.60", 40, total=276),
            slider("曝光", "1.00", 40, total=276),
        ], head_icon="sliders", head_chip=chip("选中：新光源", "accent"), fill_h=True),
    ])

    ai_bar = frame("AI 光照条", [
        chip("AI 光照", "violet", "sparkles"),
        frame("氛围描述框",
              [text("氛围描述（求值时由本地解析器生成光源）", 10.5, "normal", "$text-muted")],
              width="fill_container", height=40, fill="$bg-elevated", cornerRadius=6,
              stroke="$border", strokeWidth=1, layout="vertical", padding=[6, 10],
              justifyContent="center"),
        btn("AI 打光", "primary", "sparkles"),
    ], width="fill_container", height=64, fill="$bg-panel", cornerRadius=8,
        stroke="$border", strokeWidth=1, gap=10, padding=[0, 12], alignItems="center")

    return [toolbar, body_split(left, center, right), ai_bar]


def build_video():
    def thumb(sel=False, kf=False):
        st = "$accent" if sel else ("#ffa94d" if kf else "$border")
        return rect(52, 30, "#121826", radius=3, stroke=st, sw=2 if sel else 1)

    left = col(236, [
        card("关键帧传播", [
            note("抽取关键帧做 AI 分析，深度沿时间轴传播，光照全片统一，速度快。"),
            slider("关键帧数量", "8", 40, total=204),
            btn("检测关键帧", "primary", "radar", w="fill_container"),
            chip("已标记关键帧", "success", "star"),
        ], head_icon="film", head_chip=chip("选中", "accent")),
        card("逐帧分析", [
            note("每一帧独立分析，光照随画面动态变化，耗时与帧数成正比。"),
        ], head_icon="layers"),
        note("光源 · 与图片模式共用参数"),
    ])

    center = frame("画布区", [
        stage([preview_box(420, 300, "舞台切片_01.mp4",
                          "450 帧 · 1920 × 1080 · 30 fps", icon_name="film")]),
        row([btn("打开视频", "ghost", "upload"),
             chip("播放", "default", "play"),
             chip("上一帧", "default"),
             chip("下一帧", "default"),
             chip("回到首帧", "default"),
             chip("含音轨", "default"),
             spacer(),
             chip("拾取主光", "accent", "crosshair")],
            width="fill_container", gap=6, alignItems="center"),
        card("时间轴", [
            row([rect(2, 34, "$accent"),
                 thumb(), thumb(kf=True), thumb(), thumb(), thumb(sel=True), thumb(),
                 thumb(kf=True), thumb(), thumb(), thumb(), thumb(kf=True)],
                gap=4, alignItems="center"),
            row([chip("已标记关键帧", "warm", "star"),
                 kv("帧号", "0128 / 0450")], width="fill_container", alignItems="center"),
        ], head_icon="film", height=150),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(300, [
        card("光源 · 与图片模式共用参数", [
            light_item("主光 · 平行光", "#ffd54a", "强度 1.20 · 4800 K", "光源可见", active=True),
            light_item("补光 · 点光", "#cfe4ff", "强度 0.50 · 6800 K", "光源隐藏"),
            row([btn("添加", "ghost", "plus"), btn("删除", "ghost", "trash")], gap=6),
            slider("环境光强度", "0.35", 18, total=260),
            slider("环境光色温 (K)", "6500", 47, total=260),
        ], head_icon="lightbulb"),
        card("处理与导出", [
            kv("实际帧速", "412 ms / 帧"),
            kv("剩余时间", "39 s（按本次实测外推）"),
            btn("开始处理", "primary", "play", w="fill_container"),
            btn("下载成品", "ghost", "download", w="fill_container"),
            chip("处理完成，可以下载成品", "success", "check"),
            note("处理时的剩余时间只按本次任务实测到的帧速外推。"),
        ], head_icon="download", fill_h=True),
        card("耗时提示", [
            note("估算：耗时随帧数线性增加；测到实际帧速后显示实测 ETA"),
            note("帧预览由本机 ffmpeg 逐帧解码，播放速度受解码速度限制"),
            note("处理在本机后台线程执行；处理过程中可以继续调整预览，"
                 "但本次任务使用的是点击「开始处理」时的参数。"),
        ], head_icon="info"),
    ])

    return [header("视频调光", "关键帧传播 · 逐帧分析 · 处理与导出",
                   chip("已导入视频", "success", "film")),
            body_split(left, center, right)]


def build_framepaint():
    def swatch(label, color):
        return frame("色板 " + label,
                     [dot(color, 14, stroke="$border"),
                      text(label, 9, "normal", "$text-muted")],
                     layout="vertical", gap=4, alignItems="center")

    def fcell(sel=False):
        return rect(46, 26, "#121826", radius=3,
                    stroke="$accent" if sel else "$border", sw=2 if sel else 1)

    def tcell(on=False):
        return rect(46, 22, "$accent-tint" if on else "#0d1119", radius=3,
                    stroke="$accent-line" if on else "$border")

    left = col(238, [
        card("光笔类型", [
            item([icon("lightbulb", 13, "$accent-bright"),
                  text("柔光晕", 10.5, "normal", "$text-primary")], active=True),
            item([icon("sun", 13, "$text-muted"), text("定向投射·锥形", 10.5)]),
            item([icon("star", 13, "$text-muted"), text("星芒", 10.5)]),
            item([icon("sparkles", 13, "$text-muted"), text("十字衍射·闪耀", 10.5)]),
            item([icon("wand", 13, "$text-muted"), text("光束", 10.5)]),
        ], head_icon="wand", head_chip=chip("5 种", "default")),
        card("光绘笔刷", [
            slider("强度", "0.85", 21, total=210),
            slider("半径", "0.22", 22, total=210),
            slider("不透明度", "0.70", 70, total=210),
            slider("光束张角", "35°", 39, total=210),
            slider("星芒条数", "6", 30, total=210),
            note("不透明度 / 辉光扩散会乘进笔触强度与半径（预览与导出一致）；"
                 "闪烁频率与下方两个开关是界面值：引擎逐帧独立合成，无逐帧时间轴字段。"),
            note("张角写入笔触 spread；绘制时光束方向由拖拽方向决定（angle）。"),
        ], head_icon="sliders"),
        card("光色", [
            row([swatch("日光", "#ffe9c4"), swatch("冷月", "#cfe4ff"),
                 swatch("暖白", "#fff6e6"), swatch("暖钨丝", "#ffb46b"),
                 swatch("中性", "#d8dee9")], gap=10),
            text("压感曲线", 10.5, "600", "$text-secondary"),
            seg(["S 曲线", "快衰减", "循环"], 0, total=210),
            note("压感曲线为界面预设值：当前引擎的笔触不含笔压字段，故不参与预览与导出。"),
        ], head_icon="palette"),
    ])

    paint_canvas = frame("光绘画布", [
        ellipse_grad(190, 190, "#ffd54acc", "#ffd54a00", rotation=225),
        row([chip("当前帧", "accent"), chip("本帧笔触 3", "default")], gap=6),
    ], width=420, height=290, fill="#0b0e16", cornerRadius=8, stroke="$border",
        strokeWidth=1, layout="vertical", gap=10, alignItems="center",
        justifyContent="center")

    center = frame("画布区", [
        stage([paint_canvas,
               note("在画面上按住拖动即可绘制 · 光束由拖拽方向决定投射角与长度 · "
                    "快捷键 ← → 跳帧 / N 新建空白帧 / [ ] 调半径 · "
                    "预览与导出使用同一份笔触数据")]),
        row([btn("打开视频", "ghost", "upload"),
             chip("播放", "default", "play"),
             chip("上一帧", "default"),
             chip("下一帧", "default"),
             chip("回到首帧", "default"),
             spacer(),
             btn("新建", "primary", "plus"),
             btn("删除", "ghost", "trash")],
            width="fill_container", gap=6, alignItems="center"),
        card("光绘时间轴", [
            row([fcell(True), fcell(), fcell(), fcell(True), fcell(), fcell(),
                 fcell(), fcell(True), fcell(), fcell(), fcell(), fcell()],
                gap=4, alignItems="center"),
            row([text("光绘", 9.5, "normal", "$text-muted"),
                 tcell(True), tcell(True), tcell(), tcell(True), tcell(True), tcell(),
                 tcell(), tcell(True), tcell(True), tcell(), tcell(), tcell()],
                gap=4, alignItems="center"),
            row([text("底片 · 视频源", 9.5, "normal", "$text-muted")] +
                [tcell(True) for _ in range(12)],
                gap=4, alignItems="center"),
            note("与「视频调光」同一个上传接口"),
        ], head_icon="film", height=170),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(276, [
        card("光绘图层", [
            item([icon("lightbulb", 13, "$accent-bright"),
                  frame("图层文字",
                        [text("仅已绘帧", 10.5, "600", "$text-primary"),
                         text("显示该图层", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)], active=True),
            item([icon("film", 13, "$text-muted"),
                  frame("图层文字",
                        [text("底片 · 视频源", 10.5, "normal", "$text-secondary"),
                         text("显示该图层", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)]),
            note("图层增益乘进笔触强度/半径，预览与导出共用同一变换。"),
        ], head_icon="layers", fill_h=True),
        card("本帧概况", [
            kv("本帧笔触", "3"),
            kv("当前帧", "012 / 045"),
            btn("删除当前帧的全部笔触", "danger", "trash", w="fill_container"),
            btn("导出序列", "primary", "download", w="fill_container"),
        ], head_icon="info"),
    ])

    return [header("逐帧光绘", "光绘笔刷 · 帧级标记 · 导出序列",
                   chip("底片 · 视频源", "default", "film")),
            body_split(left, center, right)]


def build_starmap():
    def ch_item(name, color, group, state, active=False):
        return item([dot(color, 8),
                     frame("通道文字",
                           [text(name, 10.5, "600" if active else "normal",
                                 "$text-primary" if active else "$text-secondary"),
                            text(group, 9.5, "normal", "$text-muted")],
                           layout="vertical", gap=1),
                     spacer(),
                     chip(state, "success" if state == "在线" else "default", with_dot=True)],
                    active=active)

    left = col(242, [
        card("光源分组", [
            field("搜索光源 / 分组…", icon_name="search"),
            ch_item("CH01 · 主光", "#ffd54a", "主灯组", "在线", active=True),
            ch_item("CH02 · 补光", "#cfe4ff", "主灯组", "在线"),
            ch_item("CH03 · 轮廓光", "#a78bfa", "主灯组", "在线"),
            ch_item("CH04 · 背景光", "#6fa8ff", "主灯组", "在线"),
            ch_item("CH05 · 眼神光", "#ffd54a", "主灯组", "在线"),
            ch_item("CH06 · 辅光", "#34d399", "未分组", "离线"),
        ], head_icon="radar", head_chip=chip("列表", "accent"), fill_h=True),
        card("拓扑", [
            btn("新建光源（归入当前分组）", "ghost", "plus", w="fill_container"),
            btn("整组上线 / 离线（写入 visible）", "ghost", "power", w="fill_container"),
            btn("同步到未分组", "ghost", "check", w="fill_container"),
            btn("复位视图（缩放 100%、居中）", "ghost", "rotate", w="fill_container"),
            row([chip("显示/隐藏网格", "default"), chip("显示/隐藏节点标签", "default")],
                gap=6),
        ], head_icon="layers"),
    ])

    center = frame("星图区", [
        stage([
            frame("分组 · 主灯组", [
                text("主灯组 · 5 在线", 11, "600", "$text-primary"),
                row([light_label("主光", "#ffd54a"), light_label("补光", "#cfe4ff"),
                     light_label("轮廓光", "#a78bfa"), light_label("背景光", "#6fa8ff"),
                     light_label("眼神光", "#ffd54a")], gap=26, alignItems="center"),
            ], width="fill_container", layout="vertical", gap=10, padding=[10, 12],
                fill="$bg-panel", cornerRadius=8, stroke="$accent-line", strokeWidth=1,
                alignItems="center"),
            frame("分组 · 未分组", [
                text("未分组 · 1 离线", 11, "600", "$text-secondary"),
                row([light_label("辅光", "#34d399", off=True)], gap=26, alignItems="center"),
            ], width="fill_container", layout="vertical", gap=10, padding=[10, 12],
                fill="$bg-panel", cornerRadius=8, stroke="$border", strokeWidth=1,
                alignItems="center"),
            frame("汇总条", [
                text("光源 6 · 在线 5 · 离线 1 · 平均色温 5400 K",
                     10.5, "normal", "$text-secondary", mono=True),
                spacer(),
                chip("写入引擎的 visible", "accent", "check"),
            ], width="fill_container", height=30, fill="$bg-elevated", cornerRadius=6,
                stroke="$border", strokeWidth=1, padding=[0, 10], alignItems="center"),
            note("平移：拖拽或滚轮缩放平面"),
            note("功率 / DMX / 温度非本引擎可测，故以可推导量替代"),
            note("以上四项只保存在本屏幕的界面状态里，引擎当前按 "
                 "intensity/kelvin/radius 与方向渲染。"),
            note("引擎 Light 无这些字段，仅作界面备注"),
            note("方向光由 dx/dy/dz 决定朝向（指向光源的单位向量），没有平面位置。"),
        ]),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(300, [
        card("光源属性", [
            kv("名称", "CH01 · 主光", mono=False),
            row([chip("启用", "success", with_dot=True), chip("在线", "accent")], gap=6),
            slider("强度", "1.40", 35, total=264),
            slider("色温", "5200 K", 37, total=264),
        ], head_icon="lightbulb", head_chip=chip("选中：CH01", "accent"), fill_h=True),
        card("光束参数", [
            text("光束角", 10.5, "600", "$text-secondary"),
            seg(["15° 窄", "35° 中", "60° 宽", "90° 泛光"], 1, total=264),
            text("平方反比", 10.5, "600", "$text-secondary"),
            seg(["恒定", "平方反比"], 0, total=264),
            text("柔光附件", 10.5, "600", "$text-secondary"),
            seg(["反光伞", "柔光罩 · 中", "束光筒", "格栅"], 0, total=264),
            kv("方向 dx", "+0.32"),
            kv("方向 dy", "-0.10"),
            kv("方向 dz", "+0.94"),
        ], head_icon="sun"),
    ])

    return [header("光源星图", "光源分组 · 拓扑 · 光束参数",
                   chip("光源 6", "default", "radar")),
            body_split(left, center, right)]


def build_harvest():
    def role_row(role, color, kelvin, intensity, conf):
        return frame("角色行 " + role,
                     [dot(color, 8), text(role, 10.5, "600", "$text-primary"), spacer(),
                      text(kelvin, 10, "normal", "$text-secondary", mono=True),
                      text(intensity, 10, "normal", "$text-secondary", mono=True),
                      text(conf, 10, "normal", "$text-muted", mono=True)],
                     width="fill_container", height=22, gap=8, alignItems="center")

    left = col(270, [
        card("场景采集 · SCENE CAPTURE", [
            row([chip("场景采样", "success", "check"), chip("光源分离", "success", "check"),
                 chip("灯组映射", "success", "check")], gap=6),
            text("场景采样 → 光源分离 → 灯组映射", 10, "normal", "$text-muted"),
            preview_box("fill_container", 110, "舞台_黄昏.jpg", "参考图"),
            kv("场景类型", "舞台 · 黄昏", mono=False),
            btn("重新分析", "ghost", "rotate", w="fill_container"),
        ], head_icon="camera", head_chip=chip("已解析", "success")),
        card("识别光源 · 方案与微调", [
            row([chip("主光 ↖ 3200 K", "warm"), chip("辅光 → 5600 K", "default")], gap=6),
            row([chip("轮廓光 ↗ 8400 K", "accent"), chip("环境光 6500 K", "default")],
                gap=6),
            note("解析后可切换各光源的方向箭头"),
            note("引擎会分离主光 / 辅光 / 轮廓光，给出真实推导的色温、相对强度与关键光比。"),
        ], head_icon="crosshair", fill_h=True),
    ])

    center = frame("舞台区", [
        stage([
            preview_box(420, 300, "舞台_黄昏.jpg", "参考图 · 场景采样完成"),
            text("AI LIGHT HARVEST · 从实拍场景提取专业灯光方案",
                 11, "600", "$text-secondary", ls=1),
            text("选择一张参考图，自动提取专业灯组", 10.5, "normal", "$text-muted"),
        ]),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(340, [
        card("光源解析", [
            frame("表头",
                  [text("角色", 9.5, "normal", "$text-muted"), spacer(),
                   text("色温", 9.5, "normal", "$text-muted"),
                   text("强度", 9.5, "normal", "$text-muted"),
                   text("置信度", 9.5, "normal", "$text-muted")],
                  width="fill_container", gap=8, alignItems="center"),
            role_row("主光", "#ffd54a", "3200 K", "1.00", "0.92"),
            role_row("补光", "#cfe4ff", "5600 K", "0.45", "0.81"),
            role_row("轮廓光", "#a78bfa", "8400 K", "0.38", "0.66"),
            role_row("环境光", "#6fa8ff", "6500 K", "0.18", "—"),
            kv("关键光比", "2.2 : 1"),
            kv("对比度", "0.37"),
            kv("色温一致性", "0.88"),
            kv("灯具数量", "4"),
            row([btn("应用建议", "primary", "check"),
                 btn("重置为引擎建议值", "ghost", "rotate")], gap=8),
            chip("已应用到调光台，其他屏幕同步生效", "success"),
            note("引擎只计算相对亮度与 Lambert 光照；色温、相对强度与光比为真实推导值。"),
            note("色温一致性 = 1 − 色温极差 ÷ 引擎量程（10500K）；光比与对比度由引擎返回。"),
        ], head_icon="sparkles", head_chip=chip("已解析", "success"), fill_h=True),
    ])

    return [header("智能拾光", "场景采集 · 识别光源 · 应用建议",
                   chip("已载入拾光建议强度", "success", "sparkles")),
            body_split(left, center, right)]


def build_previs():
    left = col(290, [
        card("场景对象 · SCENE OBJECTS", [
            row([btn("添加灯具", "primary", "plus"), btn("添加反光板", "ghost")], gap=8),
            item([icon("lightbulb", 13, "$accent-bright"),
                  frame("灯文字",
                        [text("主光 · 点光", 10.5, "600", "$text-primary"),
                         text("虚拟灯位 · 2.4 m", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)], active=True),
            item([icon("lightbulb", 13, "$text-muted"),
                  frame("灯文字",
                        [text("补光 · 点光", 10.5, "normal", "$text-secondary"),
                         text("虚拟灯位 · 1.8 m", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)]),
            item([icon("sun", 13, "$text-muted"),
                  frame("板文字",
                        [text("反光板", 10.5, "normal", "$text-secondary"),
                         text("仅界面对象 · 不参与引擎渲染", 9.5, "normal", "$text-muted")],
                        layout="vertical", gap=1)]),
            note("反光板为界面对象，不写入渲染参数、不影响引擎出图。"),
        ], head_icon="crosshair", fill_h=True),
        card("空间信息", [
            kv("受光素材", "角色立绘.png", mono=False),
            kv("主体宽 (m)", "0.5"),
            kv("主体高 (m)", "1.6"),
            kv("棚体尺寸", "3.0 × 3.0 × 2.6 m", mono=False),
            kv("表面模型", "漫反射（Lambert，引擎固定）", mono=False),
            note("网格 0.5 m"),
        ], head_icon="info"),
    ])

    rig = frame("虚拟棚体", [
        frame("灯位行", [mini_light("主光 · 2.4 m", "#ffd54a", "α 135° · β 35°")],
              width="fill_container", padding=[0, 20]),
        rect(48, 96, "#1c2434", radius=4),
        frame("灯位行", [mini_light("补光 · 1.8 m", "#cfe4ff", "α 315° · β 20°")],
              width="fill_container", justifyContent="flex-end", padding=[0, 20]),
    ], width=380, height=250, fill="#0b0f18", cornerRadius=8, stroke="$accent-line",
        strokeWidth=1, layout="vertical", gap=10, alignItems="center",
        justifyContent="center")

    center = frame("预演区", [
        stage([
            rig,
            seg(["透视", "俯视", "侧视"], 0, total=380),
            note("左键旋转 · 滚轮缩放"),
        ]),
        frame("预演状态条", [
            text("主光:补光 3.1:1 · 距主体 2.4 m · 光斑直径 0.8 m",
                 10.5, "normal", "$text-secondary", mono=True),
            spacer(),
            chip("布光已应用到调光台", "success", "check"),
            btn("应用布光", "primary", "check"),
        ], width="fill_container", height=32, fill="$bg-panel", cornerRadius=6,
            stroke="$border", strokeWidth=1, padding=[0, 10], gap=8,
            alignItems="center"),
        note("光比按强度降序（最高:次高）推导；反光板仅界面对象，不影响引擎渲染"),
    ], width="fill_container", height="fill_container", layout="vertical", gap=10)

    right = col(330, [
        card("灯具参数 · FIXTURE", [
            seg(["点光", "平行光"], 0, total=294),
            slider("强度", "1.40", 35, total=294),
            slider("色温", "5200 K", 37, total=294),
            slider("α 方位 (°)", "135", 38, total=294),
            slider("β 俯仰 (°)", "35", 19, total=294),
            kv("θ 光束角 (°)", "46（界面估算）", mono=False),
            note("θ = 2·atan(radius × 0.35)，为界面估算。"),
            note("α/β 写入引擎方向向量 dx/dy/dz。"),
            note("点光方向由灯位指向主体，故 α/β 为推导值（不可编辑）。"),
        ], head_icon="sliders", head_chip=chip("选中：主光", "accent"), fill_h=True),
        card("空间坐标（米）", [
            kv("x", "+1.70 m"),
            kv("y", "+0.80 m"),
            kv("z", "+2.40 m"),
            chip("已锁定位置", "warm"),
            note("已锁定位置：坐标输入不可编辑。"),
            note("平行光无物理灯位（引擎只用方向），此处为沿方向 2.4 m 的虚拟灯位。"),
            note("引擎坐标是归一化量，预演按 3.0 × 3.0 × 2.6 m 虚拟棚体线性映射，非实拍米数。"),
        ], head_icon="crosshair"),
    ])

    return [header("三维布光预演",
                   "3D LIGHTING PREVIS · 平面主体灯位预演（相对光照模型）",
                   chip("受光素材：角色立绘.png", "default", "image")),
            body_split(left, center, right)]


def build_desk():
    def channel_row(name, color, pct, value):
        return row([dot(color, 7),
                    text(name, 10.5, "normal", "$text-secondary"),
                    spacer(),
                    track(pct, 200),
                    text(value, 10.5, "normal", "$text-primary", mono=True)],
                   width="fill_container", height=30, gap=10, alignItems="center")

    def cue_item(name, fade, active=False, act_chip=None):
        kids = [icon("star", 12, "$accent-bright" if active else "$text-muted"),
                frame("CUE 文字",
                      [text(name, 10.5, "600" if active else "normal",
                            "$text-primary" if active else "$text-secondary"),
                       text(fade, 9.5, "normal", "$text-muted")],
                      layout="vertical", gap=1),
                spacer()]
        if act_chip is not None:
            kids.append(act_chip)
        return item(kids, active=active)

    subject = frame("被摄体", [text("被摄体", 9, "normal", "$text-muted")],
                    fill="$bg-elevated", cornerRadius=999, stroke="$border",
                    strokeWidth=1, padding=[2, 10], alignItems="center")

    plot = frame("灯位图", [
        row([light_label("CH01 主光", "#ffd54a"), light_label("CH03 轮廓光", "#a78bfa")],
            width="fill_container", justifyContent="space-between"),
        row([rect(60, 1, "$border"), subject, rect(60, 1, "$border")],
            width="fill_container", gap=8, alignItems="center",
            justifyContent="center"),
        row([light_label("CH02 补光", "#cfe4ff"), light_label("CH04 氛围光", "#34d399")],
            width="fill_container", justifyContent="space-between"),
    ], width="fill_container", height=186, fill="$bg-canvas", cornerRadius=6,
        stroke="$border", strokeWidth=1, layout="vertical", padding=[14, 22],
        justifyContent="space-between")

    left = col(300, [
        card("灯位图 LIGHT PLOT", [
            plot,
            note("归一化平面 · 每格 0.1 · 强度 0–4 相对标度"),
            row([btn("缩小", "ghost"), btn("放大", "ghost"), spacer(),
                 chip("俯视", "default")],
                width="fill_container", gap=8, alignItems="center"),
        ], head_icon="radar", head_chip=chip("4 通道", "default")),
        card("CUE 预设场景", [
            cue_item("CUE 01 · 黄昏逆光", "淡变 0s · 硬切", True,
                     chip("已应用", "success")),
            cue_item("CUE 02 · 舞台追光", "淡变 1.0s"),
            # 输入框与按钮各占一行：并排时输入框会被压窄，占位文案「CUE 名称
            # （留空为「未命名 CUE」）」会溢出到按钮底下（字段宽度不够）。
            field("CUE 名称（留空为「未命名 CUE」）", 26),
            btn("保存当前为 CUE", "primary", "star", w="fill_container"),
            slider("淡变时长", "0.0 s", 0, total=270),
            note("保存与载入都用此淡变时长；选中 CUE 会自动填入它存储的值。"),
            note("点击行选择 · GO 载入"),
        ], head_icon="star", head_chip=chip("2 预设", "default"), fill_h=True),
    ])

    center = col("fill_container", [
        stage([
            preview_box(320, 224, "未载入媒体", "支持 PNG / JPG / MP4 / WEBM"),
            row([chip("对比原图：关闭（显示当前渲染）", "default"),
                 chip("适应窗口", "default"),
                 spacer(),
                 chip("图片模式", "accent", "image")],
                width="fill_container", gap=6, alignItems="center"),
            note("画面：滚轮缩放 · 拖拽平移 · 双击适应窗口"),
        ]),
        card("主控 MASTER（比例微调）", [
            slider("主控", "1.00", 100, total=620),
            note("主控按比例缩放全部通道强度（0 = 全黑，抬回按归零前快照还原）。"),
        ], head_icon="sliders", head_chip=chip("100%", "accent")),
        card("通道 CHANNELS", [
            channel_row("CH01 主光", "#ffd54a", 70, "1.40"),
            channel_row("CH02 补光", "#cfe4ff", 30, "0.60"),
            channel_row("CH03 轮廓光", "#a78bfa", 40, "0.80"),
            channel_row("CH04 氛围光", "#34d399", 15, "0.30"),
            row([text("点光 · 5200 K · 半径 0.42", 10, "normal", "$text-muted", mono=True),
                 spacer(),
                 btn("添加通道", "ghost", "plus"),
                 btn("重置通道", "ghost", "rotate")],
                width="fill_container", gap=8, alignItems="center"),
            note("独奏：只让独奏通道参与渲染（界面本地，不写入参数）"),
            note("静音：写入引擎字段 visible=false"),
        ], head_icon="sliders", head_chip=chip("4 通道", "default"), fill_h=True),
    ])

    right = col(320, [
        card("参考图迁移", [
            btn("导入参考图提取主光方向", "ghost", "upload", w="fill_container"),
            note("仅做方向与色温迁移，保留原图明暗结构（需要先导入图片）。"),
            chip("已应用参考图光照", "success"),
        ], head_icon="upload"),
        card("描述 → 光照节点", [
            textarea("描述想要的光照氛围：黄昏侧逆光，暖色轮廓光打在角色右后方，"
                     "保留原画明暗结构。", 96),
            btn("生成光照节点", "primary", "wand", w="fill_container"),
            chip("AI 自动打光", "accent", "sparkles"),
            note("输出：4 通道 · 环境 0.35（离线解析）"),
        ], head_icon="wand", fill_h=True),
        note(HONEST_DESK),
    ])

    return [header("调光台", "CUE 预设场景 · 通道推子实时写回当前参数",
                   chip("当前 CUE：未应用", "default", "star")),
            body_split(left, center, right)]


def build_plugins():
    def plugin_item(icon_name, ident, sub, state_chip, active=False, icon_fill="$text-muted"):
        return item([icon(icon_name, 14, icon_fill),
                     frame("插件文字",
                           [text(ident, 10.5, "600" if active else "normal",
                                 "$text-primary" if active else "$text-secondary", mono=True),
                            text(sub, 9.5, "normal", "$text-muted")],
                           layout="vertical", gap=1),
                     spacer(),
                     state_chip], active=active)

    toolbar = row([btn("安装插件", "primary", "upload"),
                   btn("从仓库示例安装", "ghost", "download"),
                   spacer(),
                   field("搜索插件、作者、标识…", 28, "search", w=280)],
                  width="fill_container", gap=8, alignItems="center")

    cats = row([chip("全部", "accent"), chip("主题外观", "default"),
                chip("光照工具", "default"), chip("灯组", "default"),
                chip("后端扩展", "default")],
               width="fill_container", gap=6, alignItems="center")

    left = col("fill_container", [
        card("我的插件", [
            plugin_item("palette", "com.example.amber-theme",
                        "主题外观 · 未署名 · v1.0.0", chip("已启用", "success"),
                        active=True, icon_fill="$accent-bright"),
            plugin_item("sliders", "com.example.velvet-night",
                        "光照工具 · 未署名 · v1.0.0", chip("已停用", "default")),
            plugin_item("lightbulb", "com.example.warm-rig",
                        "灯组 · 未署名 · v1.0.0", chip("已启用", "success"),
                        icon_fill="$warm"),
            row([text("共 3 个", 10, "normal", "$text-muted"),
                 spacer(),
                 note("当前显示 3 个")],
                width="fill_container", gap=8, alignItems="center"),
            note("① 点击上方「安装插件」，选择一份 .json 清单；② 若从仓库目录运行，"
                 "可用「从仓库示例安装」载入 plugins/examples 下的示例。"),
            note("插件是可选的本地扩展，软件不捆绑任何第三方插件；"
                 "插件目录默认位于用户数据目录下的 plugins/。"),
        ], head_icon="puzzle", head_chip=chip("插件数 3", "default"), fill_h=True),
    ])

    right = col(340, [
        card("插件详情", [
            kv("标识", "com.example.amber-theme", mono=False),
            kv("类型", "theme"),
            kv("版本", "1.0.0"),
            kv("作者", "未署名", mono=False),
            kv("权限", "无 · 纯数据清单", mono=False),
            row([chip("theme", "accent"), chip("preset", "default"),
                 chip("light-rig", "default"), chip("backend", "default")],
                width="fill_container", gap=6, alignItems="center"),
            row([btn("停用插件", "danger"), btn("查看插件开发说明", "ghost", "info")],
                width="fill_container", gap=8, alignItems="center"),
            note("说明：这是纯数据插件：只解析 JSON 清单，不执行任何插件代码"),
            note("停用后该插件的贡献（预设 / 灯组 / 令牌）不再出现在界面上，清单文件保留。"),
            note("安装只是把清单写入插件目录；未知字段会被丢弃，越界数值会被收敛到"
                 "引擎取值范围，非法颜色不会被采纳。"),
            note("后端只保存一个启用标记（state.json），本项与「已启用」是同一状态的"
                 "两种表述。"),
        ], head_icon="info"),
        card("插件开发说明 · 纯数据清单", [
            note("一个插件就是一个 .json 文件。后端只解析它、收敛它，绝不执行其中的"
                 "任何内容——不需要写代码，也没有脚本入口。"),
            note("data 随 kind 变化：theme 为「令牌名 → #RRGGBB」映射；"
                 "preset 为一份 RenderParams；light-rig 为一组光源。"),
            note("仓库内的示例见 plugins/examples/：com.example.amber-theme.json（主题）、"
                 "com.example.velvet-night.json（预设）、com.example.warm-rig.json（灯组）。"),
        ], head_icon="puzzle", fill_h=True),
        chip("本地扩展 · 仅解析 JSON 清单，不执行插件代码", "default"),
    ])

    return [header("插件中心", "本地扩展 · 仅解析 JSON 清单，不执行插件代码",
                   chip("插件数 3", "default", "puzzle")),
            toolbar, cats,
            body_split(left, right)]


def build_theme():
    def contrast_row(label, ratio, level_chip):
        return row([text(label, 10.5, "normal", "$text-secondary"),
                    spacer(),
                    text(ratio, 10.5, "normal", "$text-primary", mono=True),
                    level_chip],
                   width="fill_container", height=22, gap=8, alignItems="center")

    def theme_item(name, sub, active=False, state=None):
        kids = [icon("palette", 12, "$accent-bright" if active else "$text-muted"),
                frame("主题文字",
                      [text(name, 10.5, "600" if active else "normal",
                            "$text-primary" if active else "$text-secondary"),
                       text(sub, 9.5, "normal", "$text-muted")],
                      layout="vertical", gap=1),
                spacer()]
        if state is not None:
            kids.append(state)
        return item(kids, active=active)

    def mock_row(label, pct, value):
        return row([text(label, 9.5, "normal", "$text-muted"),
                    track(pct, 110, 3),
                    text(value, 9.5, "normal", "$text-secondary", mono=True)],
                   width="fill_container", gap=6, alignItems="center")

    def mock_nav(icon_name, label, active=False):
        return frame("示意导航",
                     [icon(icon_name, 12, "$accent-bright" if active else "$text-muted"),
                      text(label, 8.5, "normal",
                           "$text-primary" if active else "$text-muted")],
                     width="fill_container", layout="vertical", gap=2,
                     padding=[5, 0], alignItems="center")

    def token_row(label, token, value):
        return row([dot(value, 9, stroke="$border"),
                    text(label, 9.5, "normal", "$text-secondary"),
                    spacer(),
                    text(token, 9.5, "normal", "$text-muted", mono=True),
                    text(value.upper(), 9.5, "normal", "$text-muted", mono=True)],
                   width="fill_container", height=16, gap=8, alignItems="center")

    mock = frame("示意窗口", [
        frame("示意标题栏",
              [dot("#f87171", 6), dot("$warm", 6), dot("$success", 6),
               text("凌日光影棚", 9.5, "normal", "$text-secondary"),
               spacer(),
               text("预览示意", 9, "normal", "$text-muted")],
              width="fill_container", height=22, fill="$bg-panel", gap=5,
              padding=[0, 10], alignItems="center"),
        row([
            frame("示意侧栏",
                  [mock_nav("folder", "素材", True), mock_nav("sliders", "光照"),
                   mock_nav("sparkles", "AI 打光"), mock_nav("layers", "图层")],
                  width=54, height="fill_container", fill="$bg-panel", gap=4,
                  padding=[8, 4], layout="vertical"),
            frame("示意主区", [
                row([chip("环境光已启用", "success", with_dot=True),
                     spacer(),
                     text("本地 AI · 已连接", 9, "normal", "$success")],
                    width="fill_container", alignItems="center"),
                text("主光 · 示意", 10, "600", "$text-primary"),
                mock_row("强度", 62, "1.40"),
                mock_row("色温", 38, "5200 K"),
                mock_row("半径", 24, "0.60"),
                spacer(),
                note("（示意，非真实测量值）"),
            ], width="fill_container", height="fill_container", layout="vertical",
                gap=7, padding=10),
        ], width="fill_container", height="fill_container"),
    ], width="fill_container", height=196, fill="$bg-canvas", cornerRadius=8,
        stroke="$border", strokeWidth=1, layout="vertical", clip=True)


    left = col(300, [
        card("当前主题", [
            kv("名称", "暗夜蓝调", mono=False),
            kv("标识", "blue"),
            kv("外观", "深色 · #3B82F6"),
            row([text("跟随系统外观", 10.5, "normal", "$text-secondary"),
                 spacer(),
                 chip("开", "success"), chip("关", "default")],
                width="fill_container", gap=6, alignItems="center"),
            note("系统为深色时自动应用默认主题；浅色系统下保持当前主题"),
            row([btn("应用主题", "primary", "check", w="fill_container"),
                 btn("导出主题 JSON", "ghost", "download", w="fill_container")],
                width="fill_container", gap=8, alignItems="center"),
            note("导出内容为当前主题的 id / name / tokens，可直接作为「主题外观」"
                 "插件的 data.tokens 使用。"),
        ], head_icon="palette", head_chip=chip("8 个内置主题", "default")),
        card("排版", [
            kv("正文字体", "Inter", mono=False),
            note("只读：字体随构建产物固定，主题只换颜色，不换字体。"),
        ], head_icon="info"),
        card("形状与密度", [
            kv("圆角", "8px"),
            kv("描边", "1px"),
            note("这是界面密度偏好，写在 --hls-radius / --hls-border-width 上"
                 "（边框颜色 --hls-border 仍由主题决定），仅影响本机界面观感。"),
        ], head_icon="sliders", fill_h=True),
    ])

    center = col("fill_container", [
        card("预览示意", [mock, note("界面预览，直接使用当前令牌着色")],
             head_icon="sun", fill_h=True),
        card("对比度检查", [
            contrast_row("主文本 / 背景", "16.7:1", chip("AAA", "success")),
            contrast_row("次要文字 / 面板", "7.5:1", chip("AAA", "success")),
            contrast_row("按钮文字 / 强调色", "5.1:1", chip("AA", "success")),
            note("全部满足 WCAG 2.1 AA（正文需 4.5:1，大字 / UI 元素需 3:1）"),
        ], head_icon="gauge",
            head_chip=chip("WCAG 2.1 相对亮度，实时计算", "default")),
    ])


    right = col(330, [
        card("已安装主题", [
            theme_item("暗夜蓝调", "blue · 深色 · 跟随系统 · 16 个令牌", True,
                       chip("已应用", "success")),
            theme_item("霓虹青紫", "cyan · 深色 · 高饱和 · 16 个令牌"),
            theme_item("暗房琥珀", "amber · 深色 · 暖调 · 16 个令牌"),
            row([btn("新建主题", "ghost", "plus"),
                 btn("跟随系统", "ghost", "settings"), spacer(),
                 note("8 个内置主题")],
                width="fill_container", gap=8, alignItems="center"),
            note("自定义主题由插件中心的「主题外观」类插件提供（安装后由其贡献令牌）；"
                 "当前版本主题引擎列出的是内置主题。"),
        ], head_icon="palette", head_chip=chip("8 个", "default")),
        card("设计令牌", [
            token_row("背景", "bg-root", "#0A0D13"),
            token_row("面板", "bg-panel", "#0E121A"),
            token_row("浮层", "bg-elevated", "#161C27"),
            token_row("画布", "bg-canvas", "#070910"),
            token_row("边框", "border", "#1D2532"),
            token_row("强边框", "border-strong", "#2B3648"),
            token_row("主文本", "text-primary", "#E9EEF8"),
            token_row("次文本", "text-secondary", "#98A5BA"),
            token_row("弱文本", "text-muted", "#5E6A7E"),
            token_row("强调色", "accent", "#3B82F6"),
            token_row("强调亮色", "accent-bright", "#6FA8FF"),
            token_row("强调底色", "accent-tint", "#13233C"),
            token_row("强调描边", "accent-line", "#2A4C82"),
            token_row("成功色", "success", "#34D399"),
            token_row("暖色", "warm", "#FFA94D"),
            token_row("紫罗兰", "violet", "#A78BFA"),
        ], head_icon="layers", head_chip=chip("16 个", "default"), fill_h=True),
    ])

    return [header("主题引擎",
                   "外观系统 · 16 个设计令牌 · 运行时写入 CSS 变量，无需重新构建",
                   chip("当前主题：暗夜蓝调", "accent", "palette")),
            body_split(left, center, right)]


# ---------------------------------------------------------------- 注册表与主流程

# (屏幕名, 激活的导轨标签, 构建函数, 顶栏/状态栏文案, 导出 PNG 文件名)
SCREENS = [
    ("光影工作台 · Light Workbench", "光影工作台", build_workbench,
     dict(media_text="角色立绘.png", size_text="1 张图片 · 512 × 512 · 缩放 100%",
          status_text="就绪"), "B8-光影工作台.png"),
    ("智能打光 · Auto Light", "智能打光", build_autolight,
     dict(media_text="未载入媒体", size_text="未载入媒体",
          status_text="正在解析光照描述…"), "B9-智能打光.png"),
    ("专业模式 · Pro Mode", "专业模式", build_promode,
     dict(media_text="未命名", size_text="节点 4 · 连线 3",
          status_text="求值成功，渲染参数已应用"), "B10-专业模式.png"),
    ("视频调光 · Video Light", "视频调光", build_video,
     dict(media_text="舞台片段_1080p.mp4", size_text="1 个视频 · 1280 × 720 · 24 fps",
          status_text="播放中"), "B11-视频调光.png"),
    ("逐帧光绘 · Frame Paint", "逐帧光绘", build_framepaint,
     dict(media_text="舞台片段_1080p.mp4", size_text="1 个视频 · 48 帧 · 当前帧 12",
          status_text="合成中…"), "B12-逐帧光绘.png"),
    ("光源星图 · Light Starmap", "光源星图", build_starmap,
     dict(media_text="角色立绘.png", size_text="光源 6 · 在线 5 · 离线 1",
          status_text="就绪"), "B13-光源星图.png"),
    ("智能拾光 · Light Harvest", "智能拾光", build_harvest,
     dict(media_text="舞台_黄昏.jpg", size_text="1 张参考图 · 1920 × 1080",
          status_text="解析中…"), "B14-智能拾光.png"),
    ("三维布光预演 · 3D Previs", "三维布光预演", build_previs,
     dict(media_text="未打开媒体", size_text="虚拟棚体 3.0 × 3.0 × 2.6 m · 网格 0.5 m",
          status_text="布光已应用到调光台"), "B15-三维布光预演.png"),
    ("调光台 · Light Desk", "调光台", build_desk,
     dict(media_text="角色立绘.png", size_text="4 通道 · 主控 100%",
          status_text="当前 CUE：未应用"), "B16-调光台.png"),
    ("插件中心 · Plugins", "插件中心", build_plugins,
     dict(media_text="未打开媒体", size_text="插件数 3 · 已启用 2",
          status_text="就绪"), "B17-插件中心.png"),
    ("主题引擎 · Theme Engine", "主题引擎", build_theme,
     dict(media_text="未打开媒体", size_text="主题数 8 · 令牌数 16",
          status_text="已应用"), "B18-主题引擎.png"),
]

# 画布坐标：Skfts 在 (940, 0)、ASOlL 无坐标，新画板从 x=2440 起按 3×4 网格铺开，互不重叠。
POS = [(2440, 0), (3940, 0), (5440, 0),
       (2440, 940), (3940, 940), (5440, 940),
       (2440, 1880), (3940, 1880), (5440, 1880),
       (2440, 2820), (3940, 2820)]


def main():
    doc = json.load(io.open(PEN, encoding="utf-8"))
    before = len(doc["children"])

    # 1) 先把已有 id 全部登记，保证新 id 不和设计稿里任何既有节点撞车
    seed_ids(doc)

    # 2) 外壳模板（顶栏 sx6D3 / 导轨 QeFKw / 状态栏 a86zuX）必须在追加之前取好
    by_id = {}

    def walk(n):
        if n.get("id"):
            by_id[n["id"]] = n
        for c in n.get("children") or []:
            walk(c)

    for top in doc["children"]:
        walk(top)

    missing = [k for k in ("sx6D3", "QeFKw", "a86zuX") if k not in by_id]
    if missing:
        raise SystemExit("外壳模板缺失，拒绝写入：%s" % ", ".join(missing))

    # 3) 幂等：重跑时先摘掉同名屏幕，避免越跑越多
    names = {s[0] for s in SCREENS}
    doc["children"] = [c for c in doc["children"] if c.get("name") not in names]

    added = []
    for (name, rail, builder, texts, png), (x, y) in zip(SCREENS, POS):
        root = make_shell(by_id, name, rail, builder(), x=x, y=y, **texts)
        doc["children"].append(root)
        added.append((name, root["id"], count_nodes(root), png))

    with io.open(PEN, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)

    # 屏幕 id 是每次随机生成的，把 名称 → id / PNG 落到 screens.json，
    # 渲染 PNG 的脚本（pen2html + shot_url）才能确定性地取到屏幕节点。
    index = {
        "pen": PEN,
        "screens": [
            {"name": n, "id": i, "nodes": c, "png": p, "x": x, "y": y}
            for (n, i, c, p), (x, y) in zip(added, POS)
        ],
    }
    with io.open(SCREEN_INDEX, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    print("顶层节点 %d → %d（%s）" % (before, len(doc["children"]), PEN))
    print("屏幕索引写入 %s" % SCREEN_INDEX)
    for name, sid, n, png in added:
        print("  + %-30s %-6s %4d 节点  → b/%s" % (name, sid, n, png))
    print("全部 #CHUNK 标记已消耗完毕")


def count_nodes(node):
    n = 1
    for c in node.get("children") or []:
        n += count_nodes(c)
    return n


if __name__ == "__main__":
    main()
