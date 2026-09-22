# -*- coding: utf-8 -*-
"""Transpile a Pen (.pen) document into a self-contained HTML page.

Pen's layout model is flexbox with a small vocabulary, so the mapping is 1:1:
  frame+vertical -> flex column, frame+horizontal -> flex row, gap/padding as-is,
  fill_container -> flex:1 on the parent's main axis, fit_content -> auto.
The point of this is to produce faithful PNG exports of the .pen designs without
depending on the Pen renderer.
"""
import html
import io
import json
import sys

PEN = r"C:/Users/54301/.pencil/documents/a693244d-9f64-4d07-9faf-811b55af15da/pencil-new.pen"

# lucide-style 24x24 stroke paths (subset actually used by the document)
ICONS = {
    "layers": "m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z M2 12l8.58 3.91a2 2 0 0 0 1.66 0L21 12 M2 17l8.58 3.91a2 2 0 0 0 1.66 0L21 17",
    "sun": "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
    "image": "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z m21 15-5-5L5 21",
    "chevron-down": "m6 9 6 6 6-6",
    "settings": "M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.6a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7V20a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.6a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2Z M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
    "power": "M18.4 6.6a9 9 0 1 1-12.7 0 M12 2v10",
    "terminal": "m4 17 6-6-6-6 M12 19h8",
    "keyboard": "M20 5H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Z M6 9h.01 M10 9h.01 M14 9h.01 M18 9h.01 M6 13h.01 M18 13h.01 M9 13h6",
    "rotate-cw": "M21 12a9 9 0 1 1-2.64-6.36L21 8 M21 3v5h-5",
    "camera": "M14.5 4h-5L7 7H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-3Z M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
    "gauge": "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z m12 12 4-4",
    "radar": "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z M12 12 19 5 M12 12h.01",
    "crosshair": "M12 2v4 M12 18v4 M2 12h4 M18 12h4 M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z",
    "fire": "M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5-1.4-1.1-2-2.7-2-4.5 0-1.4.5-2.7 1-3.5-3 0-6 2-6 5 0 1 .5 2 1 2.5-1.5 0-3 1.5-3 3.5a7 7 0 0 0 7 7Z",
    "play": "m6 3 14 9-14 9Z",
    "info": "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z M12 16v-4 M12 8h.01",
    "search": "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z m21 21-4.3-4.3",
    "film": "M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z M7 3v18 M17 3v18 M3 8h4 M17 8h4 M3 16h4 M17 16h4 M3 12h18",
    "plus": "M12 5v14 M5 12h14",
    "lightbulb": "M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5 M9 18h6 M10 22h4",
    # ------------------------------------------------------------------
    # 以下 13 个取自 webui/frontend/src/ui.ts 的 ICONS（真实前端同源路径），
    # 2026-09-22 补：新增的 11 个界面用到了它们，缺了会渲染成空白/info 兜底。
    "folder": "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",
    "sparkles": "M9.9 2.6 8.5 6.2 4.9 7.6 8.5 9 9.9 12.6 11.3 9 14.9 7.6 11.3 6.2Z M18 14l-.9 2.4-2.4.9 2.4.9.9 2.4.9-2.4 2.4-.9-2.4-.9Z M5 15l-.7 1.8-1.8.7 1.8.7L5 20l.7-1.8 1.8-.7-1.8-.7Z",
    "workflow": "M3 3v18h18 M7 16v-5 M11 16V8 M15 16v-3 M19 16V6",
    "wand": "m15 4 5 5L9 20H4v-5Z M14 5l5 5",
    "puzzle": "M15.4 3a3 3 0 0 1 3 3v.5h.6a2 2 0 0 1 2 2v.6H21a3 3 0 0 1 0 6h-.5v.6a2 2 0 0 1-2 2H18v.6a3 3 0 0 1-6 0v-.6h-.6a2 2 0 0 1-2-2v-.6H9a3 3 0 0 1 0-6h.4v-.6a2 2 0 0 1 2-2h.5V6a3 3 0 0 1 3-3Z",
    "palette": "M12 22a10 10 0 1 1 0-20c5.5 0 10 4 10 9 0 2.2-1.8 4-4 4h-1.6a1.9 1.9 0 0 0-1.4 3.2A1.9 1.9 0 0 1 12 22Z M7.5 11.5h.01 M11 8h.01 M15.5 9.5h.01",
    "sliders": "M4 21v-7 M4 10V3 M12 21v-9 M12 8V3 M20 21v-5 M20 12V3 M1 14h6 M9 8h6 M17 16h6",
    # sliders-horizontal 在 ui.ts 里没有（.pen 的导轨来自 Pen 的 lucide 集合），
    # 用 lucide 官方路径补齐，避免导轨「调光台」按钮画成 info 兜底。
    "sliders-horizontal": "M21 4h-7 M10 4H3 M21 12h-9 M8 12H3 M21 20h-5 M12 20H3 M14 2v4 M8 10v4 M16 18v4",
    "download": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3",
    "upload": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
    "check": "m4 12 5 5L20 6",
    "trash": "M3 6h18 M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2 M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6",
    "rotate": "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5",
    "star": "m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1Z",
}

FONT_UI = 'Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif'
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, "Microsoft YaHei", monospace'

VARS = {}


def color(value):
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("type") == "gradient":
            cols = value.get("colors") or []
            stops = ", ".join("%s %g%%" % (c["color"], c["position"] * 100) for c in cols)
            return "linear-gradient(%gdeg, %s)" % (value.get("rotation", 180), stops)
        return None
    if isinstance(value, str) and value.startswith("$"):
        return VARS.get(value[1:])
    return value


def num(v):
    return "%gpx" % v if isinstance(v, (int, float)) else None


def padding_css(pad):
    if pad is None:
        return None
    if isinstance(pad, (int, float)):
        return num(pad)
    if len(pad) == 2:
        return "%gpx %gpx" % (pad[0], pad[1])
    if len(pad) == 4:
        return "%gpx %gpx %gpx %gpx" % tuple(pad)
    if len(pad) == 1:
        return num(pad[0])
    return None


def border_css(node):
    color_v = color(node.get("stroke"))
    sw = node.get("strokeWidth")
    if not color_v or sw is None:
        return []
    if isinstance(sw, (int, float)):
        return ["border:%gpx solid %s" % (sw, color_v)]
    out = []
    for side in ("top", "right", "bottom", "left"):
        if isinstance(sw, dict) and sw.get(side):
            out.append("border-%s:%gpx solid %s" % (side, sw[side], color_v))
    return out


ALIGN = {"center": "center", "start": "flex-start", "end": "flex-end",
         "flex-start": "flex-start", "flex-end": "flex-end", "stretch": "stretch",
         "baseline": "baseline"}
JUSTIFY = {"center": "center", "start": "flex-start", "end": "flex-end",
           "space-between": "space-between", "space-around": "space-around"}


def size_css(node, parent_layout):
    """Resolve width/height into flex sizing for the parent's axis."""
    row = parent_layout == "horizontal"
    main_axis = "w" if row else "h"

    def axis_css(axis, key, v):
        if v == "fill_container":
            if axis == main_axis:
                return ["flex:1 1 0", "min-%s:0" % ("width" if axis == "w" else "height")]
            return ["width:100%"] if axis == "w" else ["align-self:stretch"]
        if v == "fit_content":
            # Pen 的「适应内容」：按内容尺寸取宽，不参与父容器拉伸。
            # 竖排父容器里必须显式 align-self，否则会被默认的 align-items:stretch
            # 拉满整行（chip / 小按钮会变成整行色条）。
            if axis == "w" and not row:
                return ["width:auto", "align-self:flex-start"]
            return ["width:auto"] if axis == "w" else ["height:auto"]
        if isinstance(v, (int, float)):
            return ["%s:%gpx" % (key, v)]
        return ["width:auto"] if axis == "w" else ["height:auto"]

    st = axis_css("w", "width", node.get("width"))
    st += axis_css("h", "height", node.get("height"))
    if "flex:1 1 0" not in st:
        st.append("flex:0 0 auto")
    return st


def style_attr(node, parent_layout):
    st = size_css(node, parent_layout)
    t = node.get("type")
    if t == "frame":
        layout = node.get("layout") or "horizontal"
        st.append("display:flex")
        st.append("flex-direction:" + ("column" if layout == "vertical" else "row"))
        if node.get("gap") is not None:
            st.append("gap:%gpx" % node["gap"])
        ai = ALIGN.get(node.get("alignItems") or "")
        if ai:
            st.append("align-items:" + ai)
        jc = JUSTIFY.get(node.get("justifyContent") or "")
        if jc:
            st.append("justify-content:" + jc)
        if node.get("clip"):
            st.append("overflow:hidden")
    pad = padding_css(node.get("padding"))
    if pad:
        st.append("padding:" + pad)
    if t in ("frame", "rectangle", "ellipse"):
        bg = color(node.get("fill"))
        if bg and bg != "transparent":
            st.append("background:" + bg)
    st.extend(border_css(node))
    r = num(node.get("cornerRadius"))
    if r:
        st.append("border-radius:" + r)
    if t == "ellipse":
        st.append("border-radius:9999px")
    return ";".join(s for s in st if s)


def esc(s):
    return html.escape(str(s), quote=True)


def svg_icon(paths_d, size, stroke):
    subs = [p.strip() for p in paths_d.replace("M", "\x00M").split("\x00") if p.strip()]
    body = "".join('<path d="%s"/>' % esc(p) for p in subs)
    return (
        '<svg viewBox="0 0 24 24" width="%g" height="%g" fill="none" stroke="%s" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        'style="flex:0 0 auto">%s</svg>' % (size, size, esc(stroke or "currentColor"), body)
    )


def apply_descendants(target, overrides):
    """Return a copy of `target` with per-id property overrides applied."""
    def rec(n):
        out = dict(n)
        ov = overrides.get(n.get("id"))
        if ov:
            out.update(ov)
        if n.get("children"):
            out["children"] = [rec(c) for c in n["children"]]
        return out
    return rec(target)


def render(node, parent_layout="horizontal"):
    t = node.get("type")
    if t == "ref":
        target = BY_ID.get(node.get("ref"))
        if target is None:
            return ""
        merged = apply_descendants(target, node.get("descendants") or {})
        for k in ("width", "height", "fill"):
            if k in node:
                merged[k] = node[k]
        return render(merged, parent_layout)

    style = style_attr(node, parent_layout)
    layout = node.get("layout") or "horizontal"

    if t == "text":
        ff = node.get("fontFamily")
        if ff == "$font-mono" or ff == "JetBrains Mono":
            ff = FONT_MONO
        else:
            ff = FONT_UI
        extra = [
            "font-family:" + ff,
            "font-size:%gpx" % node.get("fontSize", 12),
            "font-weight:" + str(node.get("fontWeight", "normal")),
            "color:" + (color(node.get("fill")) or "#e9eef8"),
            "line-height:1.45",
            "white-space:pre-wrap",
            "word-break:break-word",
            "min-width:0",
        ]
        if node.get("letterSpacing") is not None:
            extra.append("letter-spacing:%gpx" % node["letterSpacing"])
        return '<div style="%s;%s">%s</div>' % (esc(style), esc(";".join(extra)), esc(node.get("content", "")))

    if t == "icon":
        size = node.get("width") or 14
        d = ICONS.get(node.get("icon") or "", ICONS["info"])
        return '<div style="%s">%s</div>' % (
            esc(style), svg_icon(d, size, color(node.get("fill"))))

    if t in ("rectangle", "ellipse"):
        return '<div style="%s"></div>' % esc(style)

    kids = "".join(render(c, layout) for c in node.get("children") or [])
    return '<div style="%s">%s</div>' % (esc(style), kids)


def main():
    global VARS, BY_ID
    pen_path = sys.argv[1] if len(sys.argv) > 1 else PEN
    screen_id = sys.argv[2]
    out_path = sys.argv[3]

    doc = json.load(io.open(pen_path, encoding="utf-8"))
    for k, v in (doc.get("variables") or {}).items():
        if isinstance(v, dict) and v.get("type") == "color":
            VARS[k] = v.get("value")

    def walk(n):
        yield n
        for c in n.get("children") or []:
            yield from walk(c)

    BY_ID = {}
    for top in doc["children"]:
        for n in walk(top):
            BY_ID[n.get("id")] = n

    screen = BY_ID[screen_id]
    w = screen.get("width") or 1440
    h = screen.get("height") or 900
    body = render(screen, "horizontal")
    title = screen.get("name", "Pen")

    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>%s</title>
<style>
  html,body{margin:0;padding:0;background:%s;}
  *{box-sizing:border-box;-webkit-font-smoothing:antialiased;}
</style></head>
<body><div style="width:%gpx;height:%gpx;overflow:hidden;position:relative">%s</div></body></html>
""" % (esc(title), VARS.get("bg-root", "#0a0d13"), w, h, body)

    with io.open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    print("wrote", out_path, len(page), "bytes; screen", screen_id, "%gx%g" % (w, h))


if __name__ == "__main__":
    main()
