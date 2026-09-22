# -*- coding: utf-8 -*-
"""凌日光影棚 —— 主题引擎。

零依赖（仅标准库），不引入 Qt / Web 框架 / 网络。

设计：
- 基底（background / panel / border / text）现在是**主题自身的属性**
  （``_DARK_BASE`` / ``_LIGHT_BASE`` / ``_CONTRAST_BASE``，由 ``_THEME_BASE``
  指定），不再是一套所有主题共用的深色基底。所以浅色主题可以把背景与文字的
  明暗关系整体反转，而不是把深色面板重新染色；``contrast`` 使用纯黑基底
  （``_CONTRAST_BASE``）把正文对比度推到 WCAG AAA。
- 每种主题仍只在「强调色家族」（``accent`` / ``accent-bright`` / ``accent-tint``
  / ``accent-line``）以及呼应的 ``warm`` / ``violet`` 上做整体色相偏移
  （``_ACCENT_FAMILIES``），这样每个主题读起来都是一套刻意的配色。
- 每个主题另带一组非颜色变量（``_DEFAULT_VARIABLES`` 叠加 ``_THEME_VARIABLES``）：
  字体、字号缩放、圆角、边框宽度与密度。

令牌的两类划分（重要）：
- **颜色令牌**（``COLOR_TOKEN_NAMES``，共 16 个）取值恒为 ``#RRGGBB``。
  ``tokens(theme_id)`` 与 ``get_theme(theme_id)["tokens"]`` **只**返回这一类，
  因此既有的「对颜色循环」调用方（插件贡献、前端换肤）继续原样工作。
- **非颜色变量**（``VARIABLE_NAMES``，共 5 个）为字体/尺寸/密度字符串，
  不是颜色。它们通过 ``get_theme(theme_id)["variables"]``、``variables(theme_id)``
  与 ``all_variables(theme_id)`` 暴露；``css_variables()`` 会把两类一起写成
  ``--hls-*`` 声明，前端既可继续读 ``tokens``，也可直接注入整块 css。

令牌名与 Pen 设计系统一一对应（16 个颜色）：
``bg-root, bg-panel, bg-elevated, bg-canvas, border, border-strong,
text-primary, text-secondary, text-muted, accent, accent-bright,
accent-tint, accent-line, success, warm, violet``

对外 API：``list_themes`` / ``get_theme`` / ``tokens`` / ``variables`` /
``all_variables`` / ``css_variables`` / ``json_theme``，以及对比度工具
``relative_luminance`` / ``contrast_ratio``。所有函数都接受 ``theme_id``，
未知或空值一律回退到 ``DEFAULT_THEME``。
"""
from __future__ import annotations

import json

DEFAULT_THEME = "blue"

THEME_IDS = ["blue", "cyan", "amber", "violet", "emerald", "rose", "mist", "contrast"]

# 16 个颜色令牌的固定顺序：CSS 变量输出、测试与 UI 均依赖该顺序。
COLOR_TOKEN_NAMES = [
    "bg-root",
    "bg-panel",
    "bg-elevated",
    "bg-canvas",
    "border",
    "border-strong",
    "text-primary",
    "text-secondary",
    "text-muted",
    "accent",
    "accent-bright",
    "accent-tint",
    "accent-line",
    "success",
    "warm",
    "violet",
]

# 兼容旧名：``TOKEN_NAMES`` 始终指颜色令牌（非颜色变量见 ``VARIABLE_NAMES``）。
TOKEN_NAMES = COLOR_TOKEN_NAMES

# 非颜色变量：字体 / 尺寸 / 密度，取值不是 ``#RRGGBB``。
VARIABLE_NAMES = [
    "font-ui",
    "font-size-scale",
    "radius",
    "border-width",
    "density",
]

#: 密度变量的合法取值。
DENSITIES = ("compact", "standard", "loose")

# ---------------------------------------------------------------- 基底调色板

# 深色基底：blue / cyan / amber / violet / emerald / rose 共用。
_DARK_BASE = {
    "bg-root": "#0A0D13",
    "bg-panel": "#0E121A",
    "bg-elevated": "#161C27",
    "bg-canvas": "#070910",
    "border": "#1D2532",
    "border-strong": "#2B3648",
    "text-primary": "#E9EEF8",
    "text-secondary": "#98A5BA",
    "text-muted": "#5E6A7E",
    "success": "#34D399",
}

# 浅色基底：真正的浅色（背景亮、文字暗），用于日间办公场景。
_LIGHT_BASE = {
    "bg-root": "#F4F6FA",
    "bg-panel": "#FBFCFE",
    "bg-elevated": "#FFFFFF",
    "bg-canvas": "#E8ECF2",
    "border": "#DCE3EC",
    "border-strong": "#B9C3D1",
    "text-primary": "#16202E",
    "text-secondary": "#48556A",
    "text-muted": "#5F6C80",
    "success": "#0F9D6B",
}

# 高对比基底：纯黑背景 + 纯白正文，正文对比度 21:1（WCAG AAA 上限）。
_CONTRAST_BASE = {
    "bg-root": "#000000",
    "bg-panel": "#000000",
    "bg-elevated": "#0E0E0E",
    "bg-canvas": "#000000",
    "border": "#9AA0A6",
    "border-strong": "#FFFFFF",
    "text-primary": "#FFFFFF",
    "text-secondary": "#E6E6E6",
    "text-muted": "#C9CDD2",
    "success": "#4ADE80",
}

# 主题 → 基底。新增主题只在这里挂一个基底即可。
_THEME_BASE = {
    "blue": _DARK_BASE,
    "cyan": _DARK_BASE,
    "amber": _DARK_BASE,
    "violet": _DARK_BASE,
    "emerald": _DARK_BASE,
    "rose": _DARK_BASE,
    "mist": _LIGHT_BASE,
    "contrast": _CONTRAST_BASE,
}

# ------------------------------------------------------- 每主题的强调色家族

# 每个主题的强调色家族 + 呼应点缀色。tint 为底色，line 为可读的描边色。
# 浅色主题的 bright 变体不再一味「更亮」，而是保持对浅色背景足够可读。
_ACCENT_FAMILIES = {
    "blue": {
        "accent": "#3B82F6",
        "accent-bright": "#6FA8FF",
        "accent-tint": "#13233C",
        "accent-line": "#2A4C82",
        "warm": "#FFA94D",
        "violet": "#A78BFA",
    },
    "cyan": {
        "accent": "#22D3EE",
        "accent-bright": "#67E8F9",
        "accent-tint": "#0E2D38",
        "accent-line": "#1B5B6E",
        "warm": "#FFC06B",
        "violet": "#7EA6FF",
    },
    "amber": {
        "accent": "#F59E0B",
        "accent-bright": "#FFC661",
        "accent-tint": "#2E2007",
        "accent-line": "#6B4A12",
        "warm": "#FF9F45",
        "violet": "#C9A6FF",
    },
    # 石墨灰阶：中性灰强调，不做彩色倾向。
    "violet": {
        "accent": "#9CA3AF",
        "accent-bright": "#C7CDD6",
        "accent-tint": "#22262E",
        "accent-line": "#4A525E",
        "warm": "#D6B48A",
        "violet": "#B9C2CF",
    },
    "emerald": {
        "accent": "#34D399",
        "accent-bright": "#6EE7B7",
        "accent-tint": "#0E2A22",
        "accent-line": "#1E5B48",
        "warm": "#FFC266",
        "violet": "#7DD3FC",
    },
    # 胶片暖褐：偏复古的暖赭强调。
    "rose": {
        "accent": "#C87941",
        "accent-bright": "#E0A170",
        "accent-tint": "#2E1D11",
        "accent-line": "#6B4426",
        "warm": "#E9A85C",
        "violet": "#C2A98F",
    },
    # 晨雾浅色：浅底上的深蓝，tint 为浅蓝底、line 为可读描边。
    "mist": {
        "accent": "#2563EB",
        "accent-bright": "#1D4ED8",
        "accent-tint": "#E1ECFE",
        "accent-line": "#9CBDF6",
        "warm": "#C2410C",
        "violet": "#6D28D9",
    },
    # 高对比：黑底上的亮黄，保证强调元素同样醒目。
    "contrast": {
        "accent": "#FFD400",
        "accent-bright": "#FFE24D",
        "accent-tint": "#1F1A00",
        "accent-line": "#A17F00",
        "warm": "#FFB000",
        "violet": "#C9A6FF",
    },
}

_THEME_NAMES = {
    "blue": "暗夜蓝调",
    "cyan": "霓虹青紫",
    "amber": "暗房琥珀",
    "violet": "石墨灰阶",
    "emerald": "森林绿",
    "rose": "胶片暖褐",
    "mist": "晨雾浅色",
    "contrast": "高对比无障碍",
}

# 主题列表里的短描述（中文），供设置界面辅助说明。
_THEME_NOTES = {
    "blue": "深色 · 跟随系统",
    "cyan": "深色 · 高饱和",
    "amber": "深色 · 暖调",
    "violet": "深色 · 中性灰",
    "emerald": "深色 · 低刺激",
    "rose": "深色 · 复古颗粒",
    "mist": "浅色 · 日间办公",
    "contrast": "深色 · WCAG AAA",
}

# --------------------------------------------------------------- 非颜色变量

# 所有主题共用的默认值（深色家族按标准密度）。
_DEFAULT_VARIABLES = {
    "font-ui": 'Inter, "Noto Sans SC", sans-serif',
    "font-size-scale": "1.00",
    "radius": "8px",
    "border-width": "1px",
    "density": "standard",
}

# 按主题覆盖的变量（浅色主题圆角更柔和；高对比主题加粗描边、放宽密度）。
_THEME_VARIABLES = {
    "mist": {"radius": "10px"},
    "contrast": {
        "font-size-scale": "1.06",
        "radius": "6px",
        "border-width": "2px",
        "density": "loose",
    },
}


def _theme_variables(theme_id: str) -> dict[str, str]:
    """按 ``VARIABLE_NAMES`` 顺序合并默认变量与主题覆盖。"""
    merged = dict(_DEFAULT_VARIABLES)
    merged.update(_THEME_VARIABLES.get(theme_id, {}))
    return {name: merged[name] for name in VARIABLE_NAMES}


def _build_theme(theme_id: str) -> dict:
    colours = dict(_THEME_BASE[theme_id])
    colours.update(_ACCENT_FAMILIES[theme_id])
    return {
        "name": _THEME_NAMES[theme_id],
        # dark 必须诚实：浅色主题为 False，深色主题为 True。
        "dark": theme_id != "mist",
        "note": _THEME_NOTES[theme_id],
        # 顺序与 COLOR_TOKEN_NAMES 对齐，便于 UI 稳定渲染。
        "tokens": {name: colours[name] for name in COLOR_TOKEN_NAMES},
        "variables": _theme_variables(theme_id),
    }


THEMES: dict[str, dict] = {tid: _build_theme(tid) for tid in THEME_IDS}


# ------------------------------------------------------------- 对比度工具

def relative_luminance(color: str) -> float:
    """WCAG 相对亮度（sRGB）。``color`` 形如 ``#RRGGBB``。"""
    value = color.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def _linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linear(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 对比度（1.0 ~ 21.0）。"""
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _resolve(theme_id: str = "") -> str:
    """把任意输入解析为有效主题 id，未知/空值回退到 DEFAULT_THEME。"""
    if not isinstance(theme_id, str):
        return DEFAULT_THEME
    tid = theme_id.strip()
    return tid if tid in THEMES else DEFAULT_THEME


def list_themes() -> list[dict]:
    """主题列表，供设置界面渲染：[{"id","name","dark","accent","note"}]。"""
    return [
        {
            "id": tid,
            "name": THEMES[tid]["name"],
            "dark": THEMES[tid]["dark"],
            "accent": THEMES[tid]["tokens"]["accent"],
            "note": THEMES[tid]["note"],
        }
        for tid in THEME_IDS
    ]


def get_theme(theme_id: str = "") -> dict:
    """主题完整定义：``{"id","name","dark","tokens","variables"}``（均为副本）。

    ``tokens`` 只含 16 个颜色令牌，``variables`` 只含 5 个非颜色变量。
    """
    tid = _resolve(theme_id)
    theme = THEMES[tid]
    return {
        "id": tid,
        "name": theme["name"],
        "dark": theme["dark"],
        "tokens": dict(theme["tokens"]),
        "variables": dict(theme["variables"]),
    }


def tokens(theme_id: str = "") -> dict[str, str]:
    """主题的**颜色**令牌表副本：token -> ``#RRGGBB``（仅 16 个颜色）。"""
    return dict(THEMES[_resolve(theme_id)]["tokens"])


def variables(theme_id: str = "") -> dict[str, str]:
    """主题的**非颜色**变量表副本（字体 / 字号缩放 / 圆角 / 边框宽度 / 密度）。"""
    return dict(THEMES[_resolve(theme_id)]["variables"])


def all_variables(theme_id: str = "") -> dict[str, str]:
    """颜色令牌 + 非颜色变量的合并副本，顺序为先颜色后变量（即 CSS 输出顺序）。"""
    merged = tokens(theme_id)
    merged.update(variables(theme_id))
    return merged


def css_variables(theme_id: str = "") -> str:
    """输出 CSS 变量块，例如 ``:root{--hls-bg-root:#0A0D13;...}``。

    颜色令牌与字体/尺寸/密度变量都会输出。
    """
    body = "".join(
        "--hls-{}:{};".format(name.replace("_", "-"), value)
        for name, value in all_variables(theme_id).items()
    )
    return ":root{" + body + "}"


def json_theme(theme_id: str = "") -> str:
    """主题定义（含 id）的 JSON 字符串。"""
    return json.dumps(get_theme(theme_id), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import re

    BLUE_EXACT = {
        "bg-root": "#0A0D13",
        "bg-panel": "#0E121A",
        "bg-elevated": "#161C27",
        "bg-canvas": "#070910",
        "border": "#1D2532",
        "border-strong": "#2B3648",
        "text-primary": "#E9EEF8",
        "text-secondary": "#98A5BA",
        "text-muted": "#5E6A7E",
        "accent": "#3B82F6",
        "accent-bright": "#6FA8FF",
        "accent-tint": "#13233C",
        "accent-line": "#2A4C82",
        "success": "#34D399",
        "warm": "#FFA94D",
        "violet": "#A78BFA",
    }

    # 0. 对比度工具自检（否则后续对比度结论不可信）
    assert abs(contrast_ratio("#FFFFFF", "#000000") - 21.0) < 1e-9
    assert abs(contrast_ratio("#000000", "#FFFFFF") - 21.0) < 1e-9
    assert relative_luminance("#FFFFFF") > relative_luminance("#808080")
    assert contrast_ratio("#777777", "#777777") == 1.0
    print("PASS 0  对比度工具（黑白 = 21:1）")

    # 1. 默认主题与契约常量
    assert DEFAULT_THEME == "blue"
    assert list(THEMES) == THEME_IDS
    assert len(THEME_IDS) == 8 and len(set(THEME_IDS)) == 8
    assert set(THEME_IDS) == {"blue", "cyan", "amber", "violet",
                              "emerald", "rose", "mist", "contrast"}
    assert "mist" in THEME_IDS and "contrast" in THEME_IDS
    print("PASS 1  默认主题与 THEME_IDS（8 个，含 mist / contrast）")

    # 2. blue 的 16 个颜色令牌必须逐字匹配契约
    assert tokens() == BLUE_EXACT, tokens()
    assert tokens("blue") == BLUE_EXACT
    assert get_theme()["tokens"] == BLUE_EXACT
    assert set(BLUE_EXACT) == set(COLOR_TOKEN_NAMES) and len(COLOR_TOKEN_NAMES) == 16
    assert set(tokens()) == set(BLUE_EXACT)
    print("PASS 2  blue 16 个颜色令牌与契约精确一致（tokens 仍只含颜色）")

    # 3. 每个主题颜色令牌完整、合法；基底不再共用（浅色/深色确实不同）
    for tid in THEME_IDS:
        t = tokens(tid)
        assert len(t) == 16, (tid, len(t))
        assert set(t) == set(BLUE_EXACT), (tid, set(BLUE_EXACT) - set(t))
        assert list(t) == COLOR_TOKEN_NAMES, tid
        for k, v in t.items():
            assert re.fullmatch(r"#[0-9A-F]{6}", v), (tid, k, v)
    assert tokens("mist")["bg-root"] != tokens("blue")["bg-root"]
    assert THEMES["mist"]["dark"] is False
    assert THEMES["contrast"]["dark"] is True
    assert all(THEMES[tid]["dark"] is True
               for tid in ("blue", "cyan", "amber", "violet", "emerald", "rose", "contrast"))
    print("PASS 3  8 个主题各 16 个颜色令牌，dark 标记诚实，基底按主题切换")

    # 4. 浅色真的浅、深色真的深（比较背景与正文的亮度关系）
    for tid in THEME_IDS:
        t = tokens(tid)
        bg_l = relative_luminance(t["bg-root"])
        fg_l = relative_luminance(t["text-primary"])
        if THEMES[tid]["dark"]:
            assert fg_l > bg_l, (tid, bg_l, fg_l)
        else:
            assert bg_l > fg_l, (tid, bg_l, fg_l)
    assert relative_luminance(tokens("mist")["bg-root"]) > 0.5
    assert relative_luminance(tokens("mist")["text-primary"]) < 0.1
    print("PASS 4  浅色主题背景比文字亮，深色主题相反")

    # 5. 对比度：全部 ≥ AA(4.5)，contrast 主题 ≥ AAA(7) 且高于 blue
    ratios = {}
    for tid in THEME_IDS:
        t = tokens(tid)
        r = contrast_ratio(t["text-primary"], t["bg-root"])
        ratios[tid] = r
        assert r >= 4.5, (tid, r)
    assert ratios["contrast"] >= 7.0, ratios["contrast"]
    assert ratios["contrast"] > ratios["blue"], (ratios["contrast"], ratios["blue"])
    print("PASS 5  正文对比度全部 ≥4.5，contrast ≥7 且高于 blue "
          "(contrast={:.2f} blue={:.2f})".format(ratios["contrast"], ratios["blue"]))

    # 6. 非颜色变量：完整、合法，且不混进颜色令牌
    for tid in THEME_IDS:
        v = variables(tid)
        assert list(v) == VARIABLE_NAMES, tid
        assert re.fullmatch(r"\d\.\d\d", v["font-size-scale"]), (tid, v["font-size-scale"])
        assert v["font-ui"].endswith("sans-serif"), (tid, v["font-ui"])
        assert re.fullmatch(r"\d+px", v["radius"]), (tid, v["radius"])
        assert re.fullmatch(r"\d+px", v["border-width"]), (tid, v["border-width"])
        assert v["density"] in DENSITIES, (tid, v["density"])
        assert not (set(v) & set(tokens(tid)))
    assert get_theme("mist")["variables"]["density"] == "standard"
    assert get_theme("contrast")["variables"]["border-width"] == "2px"
    merged = all_variables("contrast")
    assert set(merged) == set(BLUE_EXACT) | set(VARIABLE_NAMES)
    assert list(merged) == COLOR_TOKEN_NAMES + VARIABLE_NAMES
    print("PASS 6  非颜色变量完整且与颜色令牌分离（variables / all_variables）")

    # 7. list_themes / get_theme 结构
    listing = list_themes()
    assert len(listing) == 8
    assert [d["id"] for d in listing] == THEME_IDS
    assert all(set(d) == {"id", "name", "dark", "accent", "note"} for d in listing)
    assert listing[0]["name"] == "暗夜蓝调" and listing[0]["accent"] == "#3B82F6"
    names = {d["id"]: d["name"] for d in listing}
    assert names["mist"] == "晨雾浅色" and names["contrast"] == "高对比无障碍"
    flags = {d["id"]: d["dark"] for d in listing}
    assert flags["mist"] is False and flags["contrast"] is True
    assert all(d["note"] for d in listing)
    for tid in THEME_IDS:
        theme = get_theme(tid)
        assert set(theme) == {"id", "name", "dark", "tokens", "variables"}
        assert theme["id"] == tid and theme["tokens"] == tokens(tid)
        assert theme["variables"] == variables(tid)
    print("PASS 7  list_themes / get_theme 结构（含 note / variables）")

    # 8. 未知 / 空 / 非字符串回退到 blue
    for bad in ["", "   ", "nope", "BLUE", "blue ", "dark", None, 7]:
        assert get_theme(bad)["id"] == "blue", bad
        assert tokens(bad) == BLUE_EXACT, bad
        assert variables(bad) == variables("blue"), bad
    print("PASS 8  未知主题回退到 blue（含 None / 非字符串）")

    # 9. 返回值是副本，调用方修改不会污染模块状态
    leaked = tokens()
    leaked["accent"] = "#000000"
    assert tokens()["accent"] == "#3B82F6"
    get_theme()["tokens"]["accent"] = "#111111"
    assert THEMES["blue"]["tokens"]["accent"] == "#3B82F6"
    leaked_vars = variables()
    leaked_vars["radius"] = "999px"
    assert variables()["radius"] == "8px"
    get_theme("mist")["variables"]["radius"] = "1px"
    assert THEMES["mist"]["variables"]["radius"] == "10px"
    print("PASS 9  返回值隔离（颜色与变量都不泄漏内部状态）")

    # 10. css_variables：16 色 + 5 变量，共 21 条 --hls-*，blue 首条为 bg-root
    css = css_variables()
    assert css.startswith(":root{") and css.endswith("}"), css[:20]
    body = css[len(":root{"):-1]
    decls = [d for d in body.split(";") if d]
    assert len(decls) == 21, len(decls)
    assert decls[0] == "--hls-bg-root:#0A0D13", decls[0]
    assert "--hls-accent-bright:#6FA8FF" in decls
    assert "--hls-text-secondary:#98A5BA" in decls
    assert '--hls-font-ui:Inter, "Noto Sans SC", sans-serif' in decls
    assert "--hls-font-size-scale:1.00" in decls
    assert "--hls-radius:8px" in decls
    assert "--hls-border-width:1px" in decls
    assert "--hls-density:standard" in decls
    assert not any("_" in d.split(":")[0] for d in decls)
    assert body.count("--hls-") == 21
    for tid in THEME_IDS:
        c = css_variables(tid)
        assert c.count("--hls-") == 21, tid
        for name in COLOR_TOKEN_NAMES:
            assert "--hls-{}:".format(name) in c, (tid, name)
        for name in VARIABLE_NAMES:
            assert "--hls-{}:".format(name) in c, (tid, name)
    assert "--hls-border-width:2px" in css_variables("contrast")
    assert "--hls-density:loose" in css_variables("contrast")
    assert "--hls-font-size-scale:1.06" in css_variables("contrast")
    assert css_variables("nope") == css
    print("PASS 10 css_variables 21 条 --hls-*（16 色 + 5 变量）")

    # 11. json_theme 可往返 json.loads，且与 get_theme 一致
    raw = json_theme("emerald")
    assert json.loads(raw) == get_theme("emerald")
    assert json.loads(raw)["tokens"]["accent"] == "#34D399"
    assert json.loads(raw)["variables"]["density"] == "standard"
    assert json.loads(json_theme("mist"))["dark"] is False
    assert json.loads(json_theme("nope"))["id"] == "blue"
    assert "暗夜蓝调" in json_theme()  # 中文未被转义
    print("PASS 11 json_theme JSON 往返一致（含 variables / dark）")

    print("PASS ALL theme self-check")
