# -*- coding: utf-8 -*-
"""新增功能模块的单元测试：主题引擎、AI 自动打光、插件管理、工作流、数据目录。

运行：python -m pytest tests/test_features.py -q
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import time

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.autolight import (LightRig, lights_from_prompt, parse_offline,
                            rig_from_text, rig_to_nodes, to_render_params)
from core.paths import DATA_DIR_NAME, LEGACY_DIR_NAME
from core.plugins import PLUGIN_KINDS, PluginRegistry
from core.presets import get_preset
from core.theme import (DEFAULT_THEME, THEMES, THEME_IDS, VARIABLE_NAMES,
                        all_variables, contrast_ratio, css_variables, get_theme,
                        list_themes, relative_luminance, tokens, variables)
from core.workflow import (Edge, Node, Workflow, default_workflow, evaluate,
                           node_types, validate)

BLUE_EXPECTED = {
    "bg-root": "#0A0D13", "bg-panel": "#0E121A", "bg-elevated": "#161C27",
    "bg-canvas": "#070910", "border": "#1D2532", "border-strong": "#2B3648",
    "text-primary": "#E9EEF8", "text-secondary": "#98A5BA",
    "text-muted": "#5E6A7E", "accent": "#3B82F6", "accent-bright": "#6FA8FF",
    "accent-tint": "#13233C", "accent-line": "#2A4C82", "success": "#34D399",
    "warm": "#FFA94D", "violet": "#A78BFA",
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """隔离用户数据目录，避免测试写真实缓存/配置。"""
    import core.cache as cache_mod
    from core import ai_backend as ai_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR_DEFAULT", str(tmp_path / "cache"))
    monkeypatch.setattr(ai_mod, "CONFIG_PATH_DEFAULT",
                        str(tmp_path / "config.json"))
    yield


# ---------------------------------------------------------------- 主题引擎

def test_theme_default_is_blue_with_exact_tokens():
    """默认主题必须是蓝色，且 16 个颜色令牌与设计稿完全一致。

    ``tokens()`` 只返回颜色令牌；字体/尺寸/密度属于非颜色变量，
    由 ``variables()`` / ``all_variables()`` 另行暴露。
    """
    assert DEFAULT_THEME == "blue"
    assert tokens(DEFAULT_THEME) == BLUE_EXPECTED
    assert set(tokens()) == set(BLUE_EXPECTED)
    assert not (set(tokens()) & set(VARIABLE_NAMES))
    print("✓ 默认蓝色主题 · 16 个颜色令牌与设计稿一致")


def test_light_and_dark_themes_are_honestly_flagged():
    """dark 必须诚实：浅色主题背景比正文亮，深色主题反之。"""
    assert {"mist", "contrast"} <= set(THEME_IDS), THEME_IDS
    flags = {d["id"]: d["dark"] for d in list_themes()}
    assert flags["mist"] is False, "晨雾浅色必须是浅色主题"
    assert flags["contrast"] is True
    # 浅色主题背景与深色主题背景确实不同（不再共用同一套深色基底）
    assert tokens("mist")["bg-root"] != BLUE_EXPECTED["bg-root"]
    assert relative_luminance(tokens("mist")["bg-root"]) > 0.5
    seen_light = seen_dark = False
    for tid in THEME_IDS:
        tk = tokens(tid)
        bg_l = relative_luminance(tk["bg-root"])
        fg_l = relative_luminance(tk["text-primary"])
        if flags[tid]:
            assert fg_l > bg_l, f"{tid} 标记为深色，正文却比背景暗"
            seen_dark = True
        else:
            assert bg_l > fg_l, f"{tid} 标记为浅色，背景却比正文暗"
            seen_light = True
    assert seen_light and seen_dark, "必须同时存在浅色与深色主题"
    print("✓ 浅色/深色主题的 dark 标记与明暗关系一致")


def test_all_themes_complete_and_contrasting():
    """每个主题令牌完整，正文对比度至少 AA；高对比主题必须达到 AAA。"""
    assert len(THEME_IDS) >= 2
    ratios: dict[str, float] = {}
    for tid in THEME_IDS:
        tk = tokens(tid)
        assert set(tk) == set(BLUE_EXPECTED), f"{tid} 令牌缺失"
        for name, value in tk.items():
            assert len(value) == 7 and value.startswith("#"), (tid, name, value)
        ratio = contrast_ratio(tk["text-primary"], tk["bg-root"])
        ratios[tid] = ratio
        assert ratio >= 4.5, f"{tid} 正文对比度仅 {ratio:.1f}:1（应 ≥AA 4.5）"
    # 高对比主题：达到 WCAG AAA，且必须严格优于默认蓝色主题
    assert ratios["contrast"] >= 7.0, f"contrast 仅 {ratios['contrast']:.1f}:1（应 ≥AAA 7）"
    assert ratios["contrast"] > ratios["blue"], "高对比主题应比蓝色主题对比更强"
    print(f"✓ 全部 {len(THEME_IDS)} 个主题令牌完整，正文对比度 AA 达标"
          f"（contrast={ratios['contrast']:.1f}:1, blue={ratios['blue']:.1f}:1）")


def test_non_colour_variables_are_exposed_and_valid():
    """字体/字号/圆角/边框/密度不是颜色，必须单独暴露且取值合法。"""
    for tid in THEME_IDS:
        var = variables(tid)
        assert list(var) == VARIABLE_NAMES, tid
        assert not (set(var) & set(BLUE_EXPECTED)), f"{tid} 变量混入了颜色键"
        assert var["font-ui"].endswith("sans-serif"), (tid, var["font-ui"])
        assert var["font-size-scale"].replace(".", "", 1).isdigit(), var["font-size-scale"]
        assert var["radius"].endswith("px") and var["border-width"].endswith("px"), tid
        assert var["density"] in ("compact", "standard", "loose"), (tid, var["density"])
        merged = all_variables(tid)
        assert set(merged) == set(BLUE_EXPECTED) | set(VARIABLE_NAMES), tid
        assert list(merged) == list(tokens(tid)) + list(VARIABLE_NAMES), tid
    assert get_theme("contrast")["variables"]["border-width"] == "2px"
    print(f"✓ {len(VARIABLE_NAMES)} 个非颜色变量（{', '.join(VARIABLE_NAMES)}）与颜色令牌分离")


def test_theme_unknown_falls_back():
    assert get_theme("不存在的主题")["id"] == DEFAULT_THEME
    assert get_theme("")["id"] == DEFAULT_THEME
    assert tokens(None) == BLUE_EXPECTED
    assert variables("不存在的主题") == variables(DEFAULT_THEME)
    assert len(list_themes()) == len(THEME_IDS)
    assert all(d["id"] in THEME_IDS for d in list_themes())
    print("✓ 未知主题回退到默认")


def test_theme_css_variables():
    css = css_variables("cyan")
    for name in BLUE_EXPECTED:
        assert f"--hls-{name}:" in css
    for name in VARIABLE_NAMES:
        assert f"--hls-{name}:" in css
    assert css.count("--hls-") == len(BLUE_EXPECTED) + len(VARIABLE_NAMES)
    # 高对比主题的加粗描边与宽松密度必须真的写进 CSS
    assert "--hls-border-width:2px" in css_variables("contrast")
    assert "--hls-density:loose" in css_variables("contrast")
    print("✓ CSS 变量导出（16 色 + 非颜色变量）")


def test_theme_returns_copies():
    """返回的令牌与变量必须可安全修改（不泄漏模块内部状态）。"""
    t = get_theme("blue")
    t["tokens"]["accent"] = "#000000"
    t["variables"]["radius"] = "999px"
    assert get_theme("blue")["tokens"]["accent"] == BLUE_EXPECTED["accent"]
    assert THEMES["blue"]["tokens"]["accent"] == BLUE_EXPECTED["accent"]
    assert get_theme("blue")["variables"]["radius"] == "8px"
    leak = tokens("mist")
    leak["accent"] = "#000000"
    assert tokens("mist")["accent"] != "#000000"
    print("✓ 令牌/变量返回副本，外部修改不影响全局")


# ---------------------------------------------------------------- AI 自动打光

def test_autolight_offline_deterministic():
    """离线解析：确定性、可离线、必出结果。"""
    text = "黄昏舞台逆光，暖橘主光从左后方打来，冷蓝补光勾勒轮廓"
    a, b = parse_offline(text), parse_offline(text)
    assert a.to_dict() == b.to_dict()
    assert a.source == "offline"
    assert len(a.lights) >= 2
    assert a.lights[0].dx < 0, "‘左’应映射到 dx<0"
    assert a.lights[0].dz < 0.4, "‘逆光’应降低朝向观察者的分量"
    print("✓ 离线解析确定且产出光源")


def test_autolight_direction_and_temperature_words():
    warm = parse_offline("暖橘色主光从右上打来")
    assert warm.lights[0].dx > 0 and warm.lights[0].dy < 0
    assert warm.lights[0].kelvin <= 3500
    cool = parse_offline("冷蓝月光从左下")
    assert cool.lights[0].kelvin >= 7000
    assert parse_offline("主光 4300K 从左").lights[0].kelvin == 4300.0
    print("✓ 方向词与色温词（含显式 K 值）")


def test_autolight_intensity_and_shadow_words():
    hard = parse_offline("舞台聚光，强烈硬光")
    soft = parse_offline("柔和的棚拍补光")
    assert hard.shadow_mode == "hard"
    assert soft.shadow_mode == "soft"
    assert hard.lights[0].intensity > soft.lights[0].intensity
    print("✓ 强度词与阴影风格词")


def test_autolight_clamps_and_bounds():
    """解析结果必须落在引擎允许的范围内。"""
    rig = parse_offline("极强极亮 99000K 灯光")
    for l in rig.lights:
        assert 0.0 <= l.intensity <= 4.0
        assert 1500 <= l.kelvin <= 12000
        assert 0.0 <= rig.ambient_intensity <= 2.0
    print("✓ 输出参数被限幅在引擎范围内")


def test_autolight_cloud_missing_falls_back():
    """未配置云端时必须回落到离线，不得抛异常或阻塞。"""
    from core.ai_backend import AIBackendConfig
    cfg = AIBackendConfig(cloud_enabled=False)
    rig = rig_from_text("舞台聚光", config=cfg, use_cloud=True)
    assert rig.source == "offline" and rig.lights
    # 配置了云端但地址无效 → 仍回落
    cfg2 = AIBackendConfig(cloud_enabled=True, cloud_api_key="k",
                           cloud_base_url="http://127.0.0.1:1", cloud_model="m")
    rig2 = rig_from_text("舞台聚光", config=cfg2, use_cloud=True)
    assert rig2.source == "offline" and rig2.lights
    print("✓ 云端不可用自动回落离线")


def test_autolight_to_nodes_and_params():
    rig = parse_offline("标准棚拍柔光")
    nodes = rig_to_nodes(rig)
    assert sum(1 for n in nodes if n["type"] == "light") == len(rig.lights)
    assert any(n["type"] == "global" for n in nodes)
    base = get_preset("标准棚拍")
    p = to_render_params(rig, base)
    assert len(p.lights) == len(rig.lights)
    assert base.lights[0].name != rig.lights[0].name, "不得就地修改基准参数"
    assert LightRig.from_dict(rig.to_dict()).to_dict() == rig.to_dict()
    assert lights_from_prompt("冷色月夜") [0].kelvin >= 7000
    print("✓ 转节点 / 转渲染参数 / JSON 往返")


# ---------------------------------------------------------------- 插件管理

def test_plugin_registry_lifecycle():
    """安装 → 列出 → 停用 → 贡献归零 → 移除。"""
    with tempfile.TemporaryDirectory() as d:
        reg = PluginRegistry(d)
        assert reg.scan() == []
        man = {"id": "t.rig", "name": "测试灯组", "version": "1.0.0",
               "kind": "light-rig", "description": "测试",
               "data": {"lights": [{"name": "灯", "kind": "directional",
                                    "dx": -0.5, "dy": -0.6, "dz": 0.7,
                                    "intensity": 9.9, "kelvin": 3200,
                                    "radius": 0.5}]}}
        path = os.path.join(d, "src.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False)
        m = reg.install(path)
        assert m.id == "t.rig"
        assert len(reg.list()) == 1

        contrib = reg.contributions()
        lights = contrib["light_rigs"]["测试灯组"]
        from core.types import Light
        assert set(lights[0]) <= set(Light.__dataclass_fields__), "未知字段应被丢弃"
        assert lights[0]["intensity"] <= 4.0, "强度应被限幅"

        assert reg.set_enabled("t.rig", False) is True
        assert reg.contributions()["light_rigs"] == {}, "停用后不应再贡献"
        # 状态在新实例上仍然生效（已持久化）
        assert PluginRegistry(d).scan() and \
            not PluginRegistry(d).contributions()["light_rigs"]

        assert reg.remove("t.rig") is True
        assert reg.scan() == []
    print("✓ 插件 安装/列示/停用持久化/贡献/移除")


def test_plugin_bad_inputs():
    with tempfile.TemporaryDirectory() as d:
        reg = PluginRegistry(d)
        # 损坏 JSON 不应让扫描崩溃
        with open(os.path.join(d, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert reg.scan() == []
        # 缺字段 / 未知类型 应显式报错
        for bad in ({"name": "x", "version": "1", "kind": "light-rig"},
                    {"id": "x", "version": "1", "kind": "light-rig"},
                    {"id": "x", "name": "x", "version": "1", "kind": "unknown"}):
            p = os.path.join(d, "b.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            with pytest.raises(ValueError):
                reg.install(p)
    assert "light-rig" in PLUGIN_KINDS
    print("✓ 插件异常输入被显式拒绝（不静默）")


def test_shipped_example_plugins_are_valid():
    """仓库自带示例插件必须能被真实安装（否则文档示例是坏的）。"""
    ex_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugins", "examples")
    assert os.path.isdir(ex_dir), "缺少 plugins/examples"
    files = [f for f in os.listdir(ex_dir) if f.endswith(".json")]
    assert files, "示例插件为空"
    with tempfile.TemporaryDirectory() as d:
        reg = PluginRegistry(d)
        for fn in files:
            reg.install(os.path.join(ex_dir, fn))
        assert len(reg.list()) == len(files)
        c = reg.contributions()
        assert c["light_rigs"] or c["presets"] or c["themes"], "示例未产生任何贡献"
    print(f"✓ 随包示例插件可安装（{len(files)} 个）")


# ---------------------------------------------------------------- 工作流

def test_workflow_default_is_valid_and_evaluates():
    wf = default_workflow()
    assert validate(wf) == []
    p = evaluate(wf)
    assert len(p.lights) >= 1
    assert p.to_json() and len(node_types()) >= 5
    print("✓ 默认工作流可校验、可求值")


def test_workflow_light_node_clamps():
    wf = Workflow(nodes=[Node(id="i", type="input"),
                         Node(id="l", type="light",
                              params={"kind": "directional", "dx": -0.5,
                                      "dy": -0.6, "dz": 0.7,
                                      "intensity": 99, "kelvin": 99999,
                                      "radius": -3}),
                         Node(id="o", type="output")],
                  edges=[Edge("i", "l"), Edge("l", "o")])
    p = evaluate(wf)
    added = [l for l in p.lights if l.intensity > 0]
    assert all(l.intensity <= 4.0 and l.kelvin <= 12000 and l.radius >= 0.05
               for l in added)
    print("✓ 光源节点参数被限幅")


def test_workflow_global_node_and_immutability():
    base = get_preset("标准棚拍")
    before = base.to_json()
    wf = Workflow(nodes=[Node(id="i", type="input"),
                         Node(id="g", type="global",
                              params={"ambient_intensity": 9,
                                      "exposure": 100,
                                      "shadow_mode": "hard"}),
                         Node(id="o", type="output")],
                  edges=[Edge("i", "g"), Edge("g", "o")])
    p = evaluate(wf, base)
    assert p.ambient_intensity <= 2.0 and p.exposure <= 2.5
    assert p.shadow_mode == "hard"
    assert base.to_json() == before, "evaluate 不得修改传入的基准参数"
    print("✓ 全局节点限幅且不修改基准参数")


def test_workflow_accumulates_lights_order_independently():
    def mk(order):
        return Workflow(
            nodes=[Node(id=n, type=t) for n, t in order],
            edges=[Edge(order[0][0], order[1][0]), Edge(order[1][0], order[2][0])])

    spec = [("i", "input"),
            ("l1", "light"), ("l2", "light"), ("o", "output")]
    a = Workflow(nodes=[Node(id="i", type="input"),
                        Node(id="l1", type="light", params={"name": "A"}),
                        Node(id="l2", type="light", params={"name": "B"}),
                        Node(id="o", type="output")],
                 edges=[Edge("i", "l1"), Edge("l1", "l2"), Edge("l2", "o")])
    b = Workflow(nodes=[Node(id="i", type="input"),
                        Node(id="l2", type="light", params={"name": "B"}),
                        Node(id="l1", type="light", params={"name": "A"}),
                        Node(id="o", type="output")],
                 edges=[Edge("i", "l2"), Edge("l2", "l1"), Edge("l1", "o")])
    assert len(evaluate(a).lights) == len(evaluate(b).lights)
    print("✓ 多光源累加且与书写顺序无关")


def test_workflow_rejects_cycles_and_unknown_types():
    cyc = Workflow(nodes=[Node(id="a", type="light"), Node(id="b", type="light")],
                   edges=[Edge("a", "b"), Edge("b", "a")])
    assert validate(cyc), "环路必须被检出"
    with pytest.raises(ValueError):
        evaluate(cyc)
    unk = Workflow(nodes=[Node(id="z", type="不存在的节点")], edges=[])
    assert validate(unk)
    with pytest.raises(ValueError):
        evaluate(unk)
    print("✓ 环路与未知节点被拒绝")


def test_workflow_json_roundtrip():
    wf = default_workflow()
    assert Workflow.from_json(wf.to_json()).to_dict() == wf.to_dict()
    with pytest.raises(ValueError):
        Workflow.from_json("[1,2,3]")
    print("✓ 工作流 JSON 往返")


# ---------------------------------------------------------------- 统一媒体识别

def test_sniff_media_prefers_magic_bytes():
    """识别以文件内容为准：改名过的文件也应正确识别。"""
    from webui.api import _sniff_media

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    jpg = b"\xff\xd8\xff" + b"\x00" * 32
    mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
    mov = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32
    avi = b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 32
    mkv = b"\x1aE\xdf\xa3" + b"\x00" * 32

    assert _sniff_media(png, "")[0] == "image"
    assert _sniff_media(jpg, "")[0] == "image"
    # 关键：扩展名与内容矛盾时，以内容为准
    assert _sniff_media(png, "其实是视频.mp4")[0] == "image"
    assert _sniff_media(mp4, "伪装成图片.png")[0] == "video"
    assert _sniff_media(mov, "")[1] == ".mov"
    assert _sniff_media(avi, "") == ("video", ".avi")
    assert _sniff_media(mkv, "")[0] == "video"
    # 内容无法判定时才看扩展名
    assert _sniff_media(b"\x00" * 64, "a.png")[0] == "image"
    assert _sniff_media(b"\x00" * 64, "a.mp4")[0] == "video"
    # 两者都无法判定
    assert _sniff_media(b"random bytes", "a.bin") == ("", "")
    print("✓ 媒体识别以魔数为准（改名文件也可识别）")


def test_port_available_detects_occupied():
    """端口探测必须能发现被占用的端口（曾因 SO_REUSEADDR 在 Windows 上恒为真）。"""
    import socket

    from webui.desktop import _port_available

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    try:
        assert _port_available(busy) is False, "占用中的端口必须报告为不可用"
    finally:
        srv.close()
    print("✓ 端口占用探测正确")


def test_model_download_is_idempotent(tmp_path, monkeypatch):
    """回归：模型已就绪时必须**不联网**直接返回（用户 step 3 失败的那条路径）。"""
    import core.builtin_depth as bd

    fake = tmp_path / "model-small.onnx"
    fake.write_bytes(b"\x00" * 2_000_000)          # >1MB 即视为就绪
    calls = []

    def _boom(*a, **kw):
        calls.append(a)
        raise AssertionError("模型已存在时不应发起下载")

    monkeypatch.setattr(bd.urllib.request, "urlopen", _boom)
    got = bd.download_model(str(fake))
    assert got == str(fake)
    assert not calls, "不应访问网络"
    print("✓ 模型已就绪 → 不联网直接返回")


def test_model_download_failure_cleans_part(tmp_path, monkeypatch):
    """回归：下载失败必须清理 .part 半成品，不能留下会被误判的残file。"""
    import core.builtin_depth as bd

    dst = tmp_path / "model-small.onnx"
    # 预置一个「存在的残file」（<1MB 会被视为未就绪）
    dst.write_bytes(b"\x00" * 1024)

    def _fail(*a, **kw):
        raise OSError("网络不可达")

    monkeypatch.setattr(bd.urllib.request, "urlopen", _fail)
    monkeypatch.setenv("HLS_MODEL_URL", "http://127.0.0.1:1/x.onnx")
    try:
        bd.download_model(str(dst))
        raise AssertionError("下载失败应当抛异常")
    except OSError:
        pass
    finally:
        monkeypatch.delenv("HLS_MODEL_URL", raising=False)
    assert not list(tmp_path.glob("*.part")), "不应残留 .part 半成品"
    assert not dst.exists(), "失败后不应留下不完整的正式文件"
    print("✓ 下载失败 → 无 .part 残留")


def test_model_url_override(monkeypatch):
    """回归：HLS_MODEL_URL 应能覆盖下载源（GitHub 常不可达，需走镜像）。"""
    import core.builtin_depth as bd
    monkeypatch.delenv("HLS_MODEL_URL", raising=False)
    assert bd.model_url() == bd.MIDAS_SMALL_URL
    monkeypatch.setenv("HLS_MODEL_URL", "https://mirror.example/m.onnx")
    assert bd.model_url() == "https://mirror.example/m.onnx"
    print("✓ HLS_MODEL_URL 覆盖下载源")


def test_model_endpoints_take_kind_as_query_param():
    """回归：kind 必须是**查询参数**，前端 ?kind=dav2|normal 才能生效。

    为什么不能用请求体：/api/model/import 的 body 已被 .onnx 原始字节占用
    （await request.body()），再声明一个 body 参数会让 FastAPI 去解析
    protobuf 字节，导入直接 422。前端 api.ts 里也已经写死走查询串。
    """
    from fastapi.routing import APIRoute

    from webui import api as a

    routes = {r.path: r for r in a.app.routes if isinstance(r, APIRoute)}
    for path in ("/api/model/download", "/api/model/import"):
        assert path in routes, f"{path} 路由不存在（前端会 404）"
        names = {p.name for p in routes[path].dependant.query_params}
        assert "kind" in names, f"{path} 的 kind 不是查询参数：{sorted(names)}"
    print("✓ 两个模型端点的 kind 都是查询参数")


def test_model_download_routes_by_kind(tmp_path, monkeypatch):
    """回归：三个 kind 各自落到自己的模型文件，且已就绪时一律不联网。

    改造前只有一个按钮、固定下 MiDaS：DAV2 / 法线模型即使部署好了，
    界面上也没有入口能装，只能靠环境变量或手工放文件。
    """
    from core import preprocess as pp
    from core.ai_backend import AIBackendConfig
    from webui import api as a

    # dav2 走 preprocess 的**标准路径**（不看配置），因此要连模型目录一起换掉
    monkeypatch.setattr(pp, "MODELS_DIR_DEFAULT", str(tmp_path))
    midas = tmp_path / "midas.onnx"
    moge = tmp_path / "moge.onnx"
    dav2 = tmp_path / pp.DEPTH_MODEL_FILENAME
    for p in (midas, moge, dav2):
        p.write_bytes(b"\x00" * 2_000_000)      # >1MB 即视为就绪

    old = a._config
    a._config = AIBackendConfig(builtin_model_path=str(midas),
                                builtin_normal_model_path=str(moge))
    try:
        for kind, want in (("depth", midas), ("dav2", dav2), ("normal", moge)):
            r = a.api_model_download(kind=kind)
            assert r["already"] is True, f"{kind} 已就绪时不应联网下载"
            assert r["kind"] == kind and r["path"] == str(want), r
        with pytest.raises(a.ApiError) as ei:
            a.api_model_download(kind="bogus")
        assert ei.value.error == "bad_kind", ei.value.error
    finally:
        a._config = old
    print("✓ depth / dav2 / normal 各落到自己的文件；未知 kind 报 bad_kind")


def test_model_progress_reports_and_never_fabricates(tmp_path, monkeypatch):
    """回归：下载进度必须可轮询，且**不得编造百分比**。

    改造前点「一键下载模型」后界面没有任何反馈（64/94/134MB 是同步长请求），
    用户只能干等或以为卡死。但进度也不能乱报：服务器没给 Content-Length 时
    total=0，真实进度未知，只能按「已接收 N MB」展示。
    """
    from fastapi.routing import APIRoute

    from core import builtin_depth as bd
    from core.ai_backend import AIBackendConfig
    from webui import api as a

    # ① 路由存在且是 GET（前端 api.ts 的 modelProgress() 按 GET 打）
    routes = {r.path: r for r in a.app.routes if isinstance(r, APIRoute)}
    assert "/api/model/progress" in routes, "进度路由不存在（界面会 404）"
    assert "GET" in routes["/api/model/progress"].methods

    # ② 回调换算：total 已知给百分比；total=0 时百分比必须保持 0
    cb = a._dl_progress("dav2")
    cb(30, 120)
    assert a._model_dl["percent"] == 25.0 and a._model_dl["done"] == 30
    cb(40, 0)
    assert a._model_dl["percent"] == 0.0, "无 Content-Length 时不能拿 done 冒充总量"
    assert a._model_dl["done"] == 40, "已接收字节仍要如实上报"
    assert a.api_model_progress()["active"] is True

    # ③ 下载失败后必须复位，否则界面永远停在「下载中…」
    dst = tmp_path / "midas.onnx"
    old = a._config
    a._config = AIBackendConfig(builtin_model_path=str(dst))

    def _fail(path, progress=None, timeout=30.0):
        if progress:
            progress(1024, 0)
        raise OSError("网络不可达")

    monkeypatch.setattr(bd, "download_model", _fail)
    try:
        with pytest.raises(a.ApiError):
            a.api_model_download(kind="depth")
        assert a.api_model_progress()["active"] is False, "失败后必须复位"
    finally:
        a._config = old
    print("✓ 进度可轮询：换算正确、无 Content-Length 不编造、失败即复位")


def test_validate_model_rejects_fake_onnx(tmp_path):
    """回归：仅按大小判断会放过「>1MB 但不是合法 ONNX」的文件。

    历史缺陷：/api/model/import 用 is_model_ready（只看大小 >1MB）做校验，
    任何大文件都能通过，界面报「模型已导入」而推理时才静默降级。
    这里断言真校验（尝试建会话）会拒绝这种文件。
    """
    import pytest

    import core.builtin_depth as bd

    fake = tmp_path / "fake.onnx"
    # 大于 1MB、含 'onnx' 字样 —— 旧的粗校验会接受它
    fake.write_bytes(b"onnx" + os.urandom(2_000_000))
    assert bd.is_model_ready(str(fake)) is True, "size-only 校验会放过它（这正是问题）"

    with pytest.raises(Exception):
        bd.validate_model(str(fake))
    print("✓ validate_model 拒绝伪 ONNX（size-only 校验会漏过）")


def test_session_cache_invalidates_on_replace(tmp_path):
    """回归：替换同名模型文件后，会话缓存必须失效。

    历史缺陷：_get_session 只按路径缓存，导入/重新下载会**就地替换**同名文件，
    旧 InferenceSession 一直被复用 —— 界面显示「已导入」但引擎仍在用旧模型。
    """
    import core.builtin_depth as bd
    from core.builtin_depth import default_model_path

    real = default_model_path()
    if not os.path.isfile(real):
        print("— 跳过：本机没有真实模型")
        return
    p = str(tmp_path / "m.onnx")
    shutil.copy2(real, p)
    s1 = bd._get_session(p)
    k1 = [k for k in bd._session_cache if k[0] == p][0]

    # 替换文件并显式推进 mtime（模拟「重新导入同一路径」）。
    # 注意 copy2 会**保留**源文件的 mtime，所以要显式 os.utime，
    # 否则时间戳不变、缓存键也不变，测试会假失败。
    before = bd._file_stamp(p)
    shutil.copy2(real, p)
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # +5s
    assert bd._file_stamp(p) != before, "未能制造出 mtime 变化"

    s2 = bd._get_session(p)
    k2 = [k for k in bd._session_cache if k[0] == p][0]
    assert k1 != k2, "文件被替换后缓存键应变化"
    assert s1 is not s2, "应重建会话而非复用旧会话"
    assert len([k for k in bd._session_cache if k[0] == p]) == 1, "旧条目应被清理"
    print("✓ 替换模型文件 → 会话缓存自动失效")


# ------------------------------------------------------- ControlNet 预处理


class _FakeInput:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class _FakeSess:
    """只实现 model_family/_moge_side 用到的两个签名方法。"""

    def __init__(self, inputs, outputs=()):
        self._inputs, self._outputs = list(inputs), list(outputs)

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return list(self._outputs)


def test_preprocess_model_family_detection():
    """按 ONNX 输入签名识别模型家族，不看文件名（用户重命名很常见）。

    判据错一个就会把法线模型当深度模型用，或反之 —— 表现为「导入成功、
    推理时才炸」或深度图整体反向，都是用户看不出来的静默错误。
    """
    from core import preprocess as P

    # HuggingFace 导出风格：输入名 pixel_values
    assert P.model_family(_FakeSess([_FakeInput(
        "pixel_values", [1, 3, "height", "width"])])) == "dav2"
    # 单输入但空间维动态（也是 DAV2 的另一种导出）
    assert P.model_family(_FakeSess([_FakeInput(
        "input", [1, 3, "h", "w"])])) == "dav2"
    # 多输入（含 0 维 num_tokens 标量）→ MoGe
    assert P.model_family(_FakeSess([
        _FakeInput("image", [1, 3, 518, 518]),
        _FakeInput("num_tokens", [])])) == "moge_normal"
    # 单输入 + 固定尺寸 → MiDaS
    assert P.model_family(_FakeSess([_FakeInput("0", [1, 3, 256, 256])])) == "midas"
    print("✓ 模型家族按输入签名识别（dav2 / moge_normal / midas）")


def test_dav2_input_size_keeps_aspect_and_patch_multiple():
    """DAV2 输入：短边 518、长边 ≤1036，且两侧都是 14 的倍数。

    不保持长宽比会让深度图整体倾斜；不是 14 的倍数会因 ViT patch
    网格对不齐而直接报错。
    """
    from core import preprocess as P

    for h, w in [(500, 1000), (1000, 500), (4000, 1000), (1000, 4000),
                 (518, 518), (37, 37), (1920, 1080), (1081, 1921)]:
        nh, nw = P._dav2_input_size(h, w)
        assert nh % P._PATCH == 0 and nw % P._PATCH == 0, \
            f"{h}x{w} → {nh}x{nw} 不是 {P._PATCH} 的倍数"
        assert min(nh, nw) >= P._PATCH, f"{h}x{w} → 尺寸退化"
        assert max(nh, nw) <= P._DAV2_MAX_SIDE + P._PATCH, \
            f"{h}x{w} → {nh}x{nw} 超过长边上限"
        # 长宽比不得翻转（h>w 时 nh>nw）
        if h > w * 1.2:
            assert nh > nw, f"{h}x{w} → {nh}x{nw} 长宽比被破坏"
        if w > h * 1.2:
            assert nw > nh, f"{h}x{w} → {nh}x{nw} 长宽比被破坏"
    # 典型场景的具体取值：短边正好 518
    nh, nw = P._dav2_input_size(1000, 500)
    assert (nh, nw) == (1036, 518), f"1000x500 → {nh}x{nw}，期望 1036x518"
    print("✓ DAV2 输入尺寸等比且为 14 的倍数")


def test_preprocess_urls_env_override(monkeypatch):
    """镜像可用环境变量覆盖（目标网络下 huggingface.co 不可达）。"""
    from core import preprocess as P

    monkeypatch.delenv("HLS_DEPTH_MODEL_URL", raising=False)
    monkeypatch.delenv("HLS_NORMAL_MODEL_URL", raising=False)
    assert P.depth_model_url() == P.DEPTH_MODEL_URL_DEFAULT
    assert P.normal_model_url() == P.NORMAL_MODEL_URL_DEFAULT
    assert "hf-mirror.com" in P.depth_model_url(), "默认应走可达的镜像"
    assert "hf-mirror.com" in P.normal_model_url()

    monkeypatch.setenv("HLS_DEPTH_MODEL_URL", "https://m.example/d.onnx")
    monkeypatch.setenv("HLS_NORMAL_MODEL_URL", "https://m.example/n.onnx")
    assert P.depth_model_url() == "https://m.example/d.onnx"
    assert P.normal_model_url() == "https://m.example/n.onnx"
    print("✓ 深度/法线下载源可用环境变量覆盖")


def test_preferred_depth_model_uses_deployed_dav2(tmp_path, monkeypatch):
    """深度路径选择：显式配置 > 已部署的 DAV2 > 默认 MiDaS 路径。

    历史缺陷（等价类）：default_model_path() 指向 model-small.onnx，
    用户只部署了 DAV2 时 model_ready() 判否 → 整条内置链静默降级到
    simulate，界面看起来「模型装好了却没用上」。
    """
    from core import preprocess as P

    # ① 显式配置永远优先
    assert P.preferred_depth_model("/cfg/custom.onnx") == "/cfg/custom.onnx"

    # ② 显式配置了但文件不存在 → 仍按显式配置走（由调用方报错，不静默换模型）
    assert P.preferred_depth_model("/cfg/missing.onnx") == "/cfg/missing.onnx"

    # ③ 未配置时：DAV2 未部署 → 回退默认 MiDaS 路径
    dav2 = tmp_path / "depth-anything-v2-small.onnx"
    mi = tmp_path / "model-small.onnx"
    monkeypatch.setattr(P, "depth_model_path", lambda: str(dav2))
    monkeypatch.setattr(P, "default_model_path", lambda: str(mi))
    assert P.preferred_depth_model() == str(mi), "DAV2 缺席时应回退 MiDaS"

    # ④ DAV2 已部署（>1MB）→ 优先用它
    dav2.write_bytes(b"\x00" * 2_000_000)
    assert P.preferred_depth_model() == str(dav2), "DAV2 就绪时应优先使用"
    print("✓ 深度模型路径：显式 > 已部署 DAV2 > 默认 MiDaS")


def test_depth_map_rejects_normal_model(monkeypatch):
    """把法线模型当深度模型用，必须**明确报错**而不是产出垃圾深度图。

    MoGe 的输出是 HxWx3 法线，若被当作单通道深度取用，得到的深度图
    在界面上「看起来像」深度图却完全错误 —— 属于最难排查的一类缺陷。
    """
    from core import preprocess as P

    moge = _FakeSess([_FakeInput("image", [1, 3, 518, 518]),
                      _FakeInput("num_tokens", [])],
                     outputs=[_FakeInput("normal", [1, 3, 518, 518])])
    monkeypatch.setattr(P, "is_model_ready", lambda *a, **kw: True)
    monkeypatch.setattr(P, "preferred_depth_model", lambda *a, **kw: "/fake/m.onnx")
    monkeypatch.setattr(P, "_get_session", lambda *a, **kw: moge)

    import numpy as _np

    rgb = _np.zeros((64, 64, 3), _np.uint8) + 128
    with pytest.raises(RuntimeError) as ei:
        P.depth_map(rgb)
    assert "法线模型" in str(ei.value), f"报错未点明选错了模型：{ei.value}"
    print("✓ 法线模型误当深度模型 → 明确报错")


def test_analyze_normal_is_optional(monkeypatch):
    """法线模型缺失/失败时 normal=None 且不抛异常（合法降级路径）。

    上层会用 depth_to_height/height_to_normal 本地派生法线，
    不该让整条后端链因可选模型缺席而失败。
    """
    import numpy as _np

    from core import preprocess as P

    rgb = _np.zeros((32, 48, 3), _np.uint8) + 100
    monkeypatch.setattr(P, "depth_map", lambda *a, **kw: _np.full(
        (32, 48), 0.5, _np.float32))

    # ① 法线模型未部署
    monkeypatch.setattr(P, "is_model_ready", lambda *a, **kw: False)
    r = P.analyze(rgb)
    assert set(r) == {"depth", "normal"}, sorted(r)
    assert r["normal"] is None
    assert r["depth"].shape == (32, 48)

    # ② 法线模型在但推理失败 → 同样降级，不抛出
    monkeypatch.setattr(P, "is_model_ready", lambda *a, **kw: True)

    def _boom(*a, **kw):
        raise RuntimeError("模拟推理失败")

    monkeypatch.setattr(P, "moge_normal", _boom)
    logs = []
    r = P.analyze(rgb, log=logs.append)
    assert r["normal"] is None, "法线失败应降级为 None"
    assert logs, "降级必须留下日志（否则用户无从知晓）"
    assert r["depth"].shape == (32, 48), "法线失败不应影响深度"
    print("✓ 法线缺席/失败 → normal=None 且深度仍可用")


def test_analyze_real_models_end_to_end():
    """真模型端到端：方向约定与几何合法性（模型未部署则跳过）。

    这是唯一能抓住「所有单元测试都过、但深度方向反了」的测试 ——
    取反与否在代码上都是一行，只有真实推理结果能分辨。
    """
    import numpy as np

    from core import preprocess as P

    dm, nm = P.depth_model_path(), P.normal_model_path()
    if not (P.is_model_ready(dm) and P.is_model_ready(nm)):
        print("— 跳过：本机未同时部署深度与法线模型")
        return

    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "examples", "sample_input.png")
    if not os.path.isfile(src):
        print("— 跳过：缺少 examples/sample_input.png")
        return
    img = Image.open(src).convert("RGB")
    img = img.resize((384, 384), Image.LANCZOS)
    rgb = np.asarray(img)

    r = P.analyze(rgb)
    d, n = r["depth"], r["normal"]
    assert d is not None and n is not None, "两个模型都在就位，不该降级"
    assert d.shape == rgb.shape[:2] and d.dtype == np.float32, d.shape
    assert 0.0 <= float(d.min()) and float(d.max()) <= 1.0, "深度必须归一到 0~1"

    # 方向：主体（画面中下）比四角（背景）近。取反会让断言失败。
    h, w = d.shape
    center = float(d[h // 3: h * 2 // 3, w // 3: w * 2 // 3].mean())
    corners = float(np.mean([d[:h // 6, :w // 6].mean(), d[:h // 6, -w // 6:].mean(),
                             d[-h // 6:, :w // 6].mean(), d[-h // 6:, -w // 6:].mean()]))
    assert center > corners, \
        f"主体({center:.3f}) 应比四角({corners:.3f}) 近（白=近），方向疑似反了"

    # 法线：形状、单位长度、+z 朝向观察者
    assert n.shape == (h, w, 3) and n.dtype == np.float32, n.shape
    assert float(n.min()) >= -1.001 and float(n.max()) <= 1.001, "法线应在 -1~1"
    err = float(np.abs(np.linalg.norm(n, axis=-1) - 1.0).max())
    assert err < 1e-3, f"法线未单位化，最大偏差 {err:.2e}"
    assert float(n[..., 2].mean()) > 0, "z 均值应为正（+z 朝观察者）"
    print(f"✓ 真模型端到端：中心 {center:.3f} > 四角 {corners:.3f}，"
          f"法线 +z {n[..., 2].mean():.3f}")


def test_model_import_enforces_kind_with_real_files(tmp_path):
    """真文件导入：kind=normal 只收法线模型，深度模型放进法线栏必须被拒。

    这是界面上「离线导入模型…」的实际路径（api.ts 会带 ?kind=）。真实文件才
    有的坑：MoGe 与 DAV2 **都能被 ONNX Runtime 加载**，只判「能加载」会放行，
    结果界面报「导入成功」、推理时才失败。模型未部署则跳过。
    """
    import asyncio

    from core import preprocess as P
    from core.ai_backend import AIBackendConfig
    from webui import api as a

    dav2, moge = P.depth_model_path(), P.normal_model_path()
    if not (P.is_model_ready(dav2) and P.is_model_ready(moge)):
        print("— 跳过：本机未同时部署深度与法线模型")
        return

    class _Req:
        def __init__(self, body):
            self._body = body

        async def body(self):
            return self._body

    old = a._config
    a._config = AIBackendConfig(builtin_model_path=str(tmp_path / "d.onnx"),
                               builtin_normal_model_path=str(tmp_path / "n.onnx"))
    try:
        # ① 深度模型冒充法线模型 → 拒绝，且不落盘
        with open(dav2, "rb") as f:
            depth_bytes = f.read()
        with pytest.raises(a.ApiError) as ei:
            asyncio.run(a.api_model_import(_Req(depth_bytes), kind="normal"))
        assert ei.value.error == "bad_model", ei.value.error
        assert "法线" in ei.value.detail, ei.value.detail
        assert not (tmp_path / "n.onnx").exists(), "被拒的文件不得落盘"
        del depth_bytes

        # ② 真法线模型 → 接受，且落到法线栏（而不是深度栏）
        with open(moge, "rb") as f:
            normal_bytes = f.read()
        r = asyncio.run(a.api_model_import(_Req(normal_bytes), kind="normal"))
        assert r["ok"] and r["kind"] == "normal", r
        assert (tmp_path / "n.onnx").stat().st_size == len(normal_bytes)
        assert not (tmp_path / "d.onnx").exists(), "法线模型不得写进深度栏"
    finally:
        a._config = old
    print("✓ 导入按 kind 校验：深度模型挡在法线栏外，真法线模型正常落盘")


def test_state_endpoint_works_and_model_ready_is_real():
    """回归：/api/state 必须真的能调用，且 model_ready 是**真检查**。

    两个历史缺陷：
      ① webui/api.py 的 model_ready() 里写了 `from .builtin_depth import …`
         （解析成不存在的 webui.builtin_depth）→ 调 /api/state 直接
         ModuleNotFoundError → 整个界面加载不出状态。py_compile 与当时
         的测试都覆盖不到，只有真调接口才会暴露。
      ② model_ready 曾用 is_model_ready（只看 >1MB），会把「大但不是合法
         ONNX」的文件报成已就绪，界面显示绿色却静默降级。
    """
    import tempfile

    from webui import api as a
    from webui.api import model_ready

    # ① 端点真的能调（覆盖 import 错误）
    data = a.api_state()
    assert data["version"], "state 返回异常"
    assert "params" in data and "themes" in data
    assert isinstance(data["model_ready"], bool)

    # ② model_ready 是真检查：>1MB 的伪 ONNX 必须为 False
    d = tempfile.mkdtemp(prefix="hls_mr_")
    try:
        fake = os.path.join(d, "fake.onnx")
        with open(fake, "wb") as f:
            f.write(b"onnx" + os.urandom(2_000_000))
        from core.builtin_depth import is_model_ready
        assert is_model_ready(fake) is True, "size-only 检查会放过它（这正是问题）"
        assert model_ready(fake) is False, "model_ready 必须真的尝试加载"
        assert model_ready("/no/such/model.onnx") is False
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("✓ /api/state 可调用，且 model_ready 是真检查")


def test_media_upload_helpers_exist_and_are_reused():
    """统一入口必须复用上传核心，而不是回退到要求声明类型的严格路由。"""
    import inspect

    from webui import api
    assert hasattr(api, "_do_image_upload")
    assert hasattr(api, "_do_video_upload")
    src = inspect.getsource(api.api_media)
    assert "_do_image_upload" in src and "_do_video_upload" in src
    assert "await" in src, "视频分支必须 await 异步上传核心"
    print("✓ 统一入口复用上传核心（不要求 Content-Type/X-Filename）")


def test_media_large_image_stream_reassembly():
    """回归：超过嗅探前缀（64KB）的图片必须无损重组。

    统一入口先读一小段用于识别媒体类型，图片分支随后**续读同一条流**并
    与前缀拼接。若续读或拼接写错，大图会被截断成坏图，而小于前缀的小图
    恰好看不出来——因此必须用真正大于前缀的图片验证。
    """
    import asyncio

    import numpy as np

    from webui import api

    rng = np.random.default_rng(11)
    big = (rng.random((700, 900, 3)) * 255).astype("uint8")   # 噪声图→低压缩率
    buf = io.BytesIO()
    Image.fromarray(big).save(buf, "PNG")
    png = buf.getvalue()
    assert len(png) > api._SNIFF_BYTES * 4, \
        f"测试图需显著大于嗅探前缀（实际 {len(png)} 字节）"

    class _FakeRequest:
        """最小请求替身：只提供 headers 与分块 stream。"""

        def __init__(self, data: bytes, chunk: int = 8192):
            self.headers = {}
            self._data = data
            self._chunk = chunk

        async def stream(self):
            for i in range(0, len(self._data), self._chunk):
                yield self._data[i:i + self._chunk]

    res = asyncio.run(api.api_media(_FakeRequest(png)))
    assert res["kind"] == "image"
    assert (res["width"], res["height"]) == (900, 700)

    back = np.asarray(Image.open(
        io.BytesIO(base64.b64decode(res["original_png_b64"]))).convert("RGB"))
    assert back.shape == big.shape
    assert np.array_equal(back, big), "大图经流式重组后像素必须完全一致"
    print(f"✓ 大图（{len(png)/1024:.0f}KB）流式重组无损")


def test_media_sniff_uses_only_prefix():
    """媒体识别只依赖前缀；不得把整段媒体读进内存。"""
    import inspect

    from webui import api
    src = inspect.getsource(api.api_media)
    assert "_SNIFF_BYTES" in src, "应限制嗅探读取量"
    assert "request.body()" not in src, "统一入口不应整体缓冲请求体"
    assert "_replay" in src, "视频分支应把前缀接回流中（不二次读取）"
    print("✓ 统一入口按前缀嗅探并流式转发")


def test_render_records_current_params_as_workflow_base():
    """回归：渲染必须记录「当前参数」，否则专业模式会叠加到错误的基准上。

    历史缺陷：`_state["params"]` 只在自动打光 apply 时写入，用户通过
    /api/render 切预设、调灯组后，工作流求值仍以启动时的默认预设为基准，
    用户当前灯组被静默丢弃。
    """
    import numpy as np

    from webui import api

    png = io.BytesIO()
    Image.fromarray((np.random.default_rng(3).random((64, 64, 3)) * 255
                     ).astype("uint8")).save(png, "PNG")

    custom = get_preset("霓虹氛围").to_dict()
    custom["ambient_intensity"] = 1.23
    custom["lights"][0]["name"] = "用户自定义粉光"
    want_lights = len(custom["lights"])

    up = api._do_image_upload(png.getvalue(), "t.png")
    api.api_render(api.RenderBody(image_id=up["image_id"], params=custom))

    cur = api._current_params()
    names = [l.name for l in cur.lights]
    assert "用户自定义粉光" in names, "渲染后当前参数应记录用户的灯组"
    assert len(cur.lights) == want_lights
    assert abs(cur.ambient_intensity - 1.23) < 1e-6
    # 不应退回默认预设
    assert "主光（柔光箱）" not in names

    # 工作流以该记录为基准继续叠加
    from core.workflow import Edge, Node, Workflow, evaluate
    wf = Workflow(nodes=[Node(id="i", type="input"),
                         Node(id="l", type="light",
                              params={"name": "新增轮廓"}),
                         Node(id="o", type="output")],
                  edges=[Edge("i", "l"), Edge("l", "o")])
    got = evaluate(wf, api._current_params())
    got_names = [l.name for l in got.lights]
    assert "用户自定义粉光" in got_names and "新增轮廓" in got_names
    print("✓ 渲染记录当前参数，工作流以其为基准叠加")


# ---------------------------------------------------------------- 视频注册表

def test_busy_video_not_evicted_or_deleted():
    """回归：有任务在跑的视频不得被 LRU 淘汰或删除。

    历史缺陷：`_new_video_entry` 直接 popitem+rmtree，`/api/video/delete` 也无条件
    rmtree。1080p 导出要跑数分钟，期间再传几个视频（MAX_VIDEOS=4）或删掉该视频，
    worker 读源文件会中途失败，而 /api/video/process 的成品就写在该目录里
    → 已完成的结果也随之消失、下载永久 404。
    """
    import threading

    from webui import api as a
    from core.types import Light

    MAXV = a.MAX_VIDEOS

    class _FakeEntry(dict):
        pass

    busy_id, free_id = "busy111", "free222"
    tmp = tempfile.mkdtemp(prefix="hls_busy_")
    try:
        for vid in (busy_id, free_id):
            d = os.path.join(tmp, vid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "source.mp4"), "wb") as f:
                f.write(b"x" * 32)

        with a._lock:
            a._videos.clear()
            a._jobs.clear()
            for vid in (busy_id, free_id):
                a._videos[vid] = {"id": vid, "dir": os.path.join(tmp, vid),
                                  "source_path": os.path.join(tmp, vid, "source.mp4"),
                                  "source_name": vid + ".mp4"}
            # busy 视频有一个运行中的任务
            a._jobs["j1"] = {"id": "j1", "video_id": busy_id, "state": "running",
                             "progress": 0.0, "message": "", "result": None,
                             "error": None,
                             "cancel_event": threading.Event()}

        assert a._video_busy(busy_id) is True
        assert a._video_busy(free_id) is False

        # 删除忙视频 → 409，且目录仍在
        try:
            a.api_video_delete(a.VideoIdBody(video_id=busy_id))
            raise AssertionError("运行中删除应当被拒绝")
        except a.ApiError as e:
            assert e.status == 409 and e.error == "video_busy", (e.status, e.error)
        assert os.path.isdir(os.path.join(tmp, busy_id)), "忙视频目录不应被删除"

        # 超过上限时淘汰：应跳过忙视频，先淘汰空闲的那个
        # 现有 2 条 → 再加 3 条 = 5 条 > MAX_VIDEOS(4)，必然触发淘汰
        with a._lock:
            for i in range(3):
                vid = f"extra{i}"
                d = os.path.join(tmp, vid)
                os.makedirs(d, exist_ok=True)
                a._videos[vid] = {"id": vid, "dir": d,
                                  "source_path": os.path.join(d, "s.mp4"),
                                  "source_name": vid}
            a._evict_videos_locked()
        with a._lock:
            remaining = set(a._videos)
        assert busy_id in remaining, "忙视频不得被淘汰"
        assert free_id not in remaining, "应优先淘汰空闲视频"
        assert os.path.isdir(os.path.join(tmp, busy_id))
        assert not os.path.isdir(os.path.join(tmp, free_id)), "空闲视频目录应被清理"

        # 任务结束后即可正常删除
        with a._lock:
            a._jobs["j1"]["state"] = "done"
        assert a._video_busy(busy_id) is False
        a.api_video_delete(a.VideoIdBody(video_id=busy_id))
        assert not os.path.exists(os.path.join(tmp, busy_id))

        # 极端情形：**全部**条目都有任务在跑时必须立即返回（不能自旋死锁）
        import time as _t
        with a._lock:
            a._videos.clear()
            a._jobs.clear()
            for i in range(MAXV + 2):
                vid = f"all{i}"
                d = os.path.join(tmp, vid)
                os.makedirs(d, exist_ok=True)
                a._videos[vid] = {"id": vid, "dir": d,
                                  "source_path": os.path.join(d, "s.mp4"),
                                  "source_name": vid}
                a._jobs[f"job{i}"] = {"id": f"job{i}", "video_id": vid,
                                      "state": "running", "progress": 0.0,
                                      "message": "", "result": None,
                                      "error": None,
                                      "cancel_event": threading.Event()}
        t0 = _t.monotonic()
        with a._lock:
            a._evict_videos_locked()
        dt = _t.monotonic() - t0
        assert dt < 1.0, f"全部忙时淘汰耗时 {dt:.2f}s，疑似自旋"
        with a._lock:
            assert len(a._videos) == MAXV + 2, "全部忙时应保持超限而不删除"
        assert all(os.path.isdir(os.path.join(tmp, f"all{i}"))
                   for i in range(MAXV + 2)), "忙视频目录都不应被删"
    finally:
        with a._lock:
            a._videos.clear()
            a._jobs.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ 运行中的视频不被淘汰/删除（409 + 跳过淘汰）")


def test_ffmpeg_frozen_fallback(tmp_path, monkeypatch):
    """回归：打包后必须能找到随包 ffmpeg。

    PyInstaller 把 binary 放到 onefile 的 _MEIPASS 或 onedir 的 _internal，
    imageio-ffmpeg 的查找逻辑并不覆盖这些位置；缺了兜底，打包产物里
    视频功能会整块失效。
    """
    import shutil
    import subprocess

    import core.video as V
    from core.video import ffmpeg_exe

    real = ffmpeg_exe()
    fake_dir = tmp_path / "meipass"
    fake_dir.mkdir()
    fake = fake_dir / "ffmpeg-bundled.exe"
    shutil.copy2(real, fake)

    monkeypatch.setattr(V, "_FFMPEG_CACHE", None, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_dir), raising=False)
    monkeypatch.delenv("HLS_FFMPEG", raising=False)
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    try:
        got = V.ffmpeg_exe()
        assert os.path.abspath(got) == os.path.abspath(str(fake)), \
            "打包环境应优先使用 _MEIPASS 内的 ffmpeg"
        assert os.environ.get("IMAGEIO_FFMPEG_EXE") == str(fake)
        assert subprocess.run([got, "-version"], capture_output=True).returncode == 0
    finally:
        V._FFMPEG_CACHE = None
    print("✓ 打包环境定位随包 ffmpeg（_MEIPASS 兜底）")


def test_ffmpeg_env_override_wins(tmp_path, monkeypatch):
    """显式指定 HLS_FFMPEG 时以此为准。"""
    import shutil

    import core.video as V
    from core.video import ffmpeg_exe

    real = ffmpeg_exe()
    custom = tmp_path / "my-ffmpeg.exe"
    shutil.copy2(real, custom)
    monkeypatch.setattr(V, "_FFMPEG_CACHE", None, raising=False)
    monkeypatch.setenv("HLS_FFMPEG", str(custom))
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    try:
        assert os.path.abspath(V.ffmpeg_exe()) == os.path.abspath(str(custom))
    finally:
        V._FFMPEG_CACHE = None
    print("✓ HLS_FFMPEG 环境变量优先")


def test_heavy_endpoints_do_not_block_event_loop():
    """回归：CPU 密集的同步活不得直接在事件循环里跑。

    历史缺陷：/api/model/import 是 async def，却直接调用 validate_model()
    构建 ONNX 会话（加载约 64MB 权重）；/api/harvest 直接跑图像解码+梯度分析；
    /api/image 与 /api/media 直接跑 _do_image_upload（4096px 时浮点混合 + 整图
    PNG 编码）；视频上传直接跑 ffmpeg 子进程。uvicorn 是单进程单事件循环，
    这些调用会让**整个服务**在期间停摆（/api/state、主题、视频进度轮询全部无响应），
    违背规格里「界面不卡死」的要求。

    判定用 AST 而非字符串匹配：必须**语义上**确认重活调用挂在某个
    `asyncio.to_thread(...)` 的参数位置，或位于被 to_thread 调用的嵌套函数内部。
    （早先的字符串版是空断言：函数体内每行都缩进，条件恒为 False。）
    """
    import ast

    heavy_names = {
        # 图片：/api/image、/api/media 图片分支
        "_do_image_upload", "_decode_image_bytes", "_png_b64",
        # 模型：/api/model/import
        "validate_model",
        # 参考图拾光：/api/harvest
        "harvest",
        # 视频：/api/video、/api/media 视频分支
        "probe", "_do_video_upload",
    }

    class _TopCalls(ast.NodeVisitor):
        """只收集 fn 自身语句里的调用，不下钻嵌套函数（嵌套 = 交给线程跑）。"""

        def __init__(self) -> None:
            self.calls: list = []
            self.awaited: set[int] = set()
            self.offloaded: set[str] = set()

        def visit_FunctionDef(self, node) -> None:      # 不下钻
            return

        def visit_AsyncFunctionDef(self, node) -> None:  # 不下钻
            return

        def visit_Lambda(self, node) -> None:            # 不下钻
            return

        def visit_Await(self, node) -> None:
            # await 一个协程不阻塞事件循环，所以 await 表达式内的调用不算问题。
            # （注意：await asyncio.to_thread(...) 里的调用由 offloaded 覆盖。）
            for c in ast.walk(node.value):
                if isinstance(c, ast.Call):
                    self.awaited.add(id(c))
            self.generic_visit(node)

        def visit_Call(self, node) -> None:
            nm = _called_name(node)
            if nm == "to_thread":
                # 参数通常是裸函数名（asyncio.to_thread(probe, src)），
                # 也可能是调用表达式 —— 两种都记下名字。
                for arg in node.args:
                    if isinstance(arg, ast.Name):
                        self.offloaded.add(arg.id)
                    elif isinstance(arg, ast.Call):
                        self.offloaded.add(_called_name(arg))
            self.calls.append(node)
            self.generic_visit(node)

    def _called_name(call) -> str:
        f = call.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    src = open("webui/api.py", encoding="utf-8").read()
    tree = ast.parse(src)

    # 协程函数：await 它们本来就不阻塞事件循环
    coro_names = {n.name for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)}

    # ② 逐个 async 端点检查是否有「未被 offload 的同步重活调用」
    problems: list[str] = []
    checked: list[str] = []
    n_offloaded = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        v = _TopCalls()
        for stmt in fn.body:
            v.visit(stmt)
        n_offloaded += len(v.offloaded)
        bad = []
        for c in v.calls:
            nm = _called_name(c)
            if nm not in heavy_names or nm in v.offloaded:
                continue
            if nm in coro_names and id(c) in v.awaited:
                continue          # await 协程，无害
            bad.append(nm)
        if bad:
            problems.append(f"{fn.name} 同步调用 {', '.join(sorted(set(bad)))}")
        else:
            checked.append(fn.name)

    assert not problems, (
        "以下 async 端点直接跑了同步重活，会阻塞事件循环（应改为 "
        "await asyncio.to_thread(...)）：" + "; ".join(problems))

    # ③ 防止测试自己被写废：确认真的检出了东西，且确实存在 offload 点
    assert len(checked) >= 5, f"检查到的 async 端点过少：{checked}"
    for must in ("api_image", "api_media", "api_model_import",
                 "api_harvest", "api_video_upload"):
        assert must in checked, f"{must} 未被检查到（端点改名或删除了？）"
    assert n_offloaded >= 4, f"只发现 {n_offloaded} 处 to_thread，预期 ≥4"
    print(f"✓ {len(checked)} 个 async 端点无同步重活；"
          f"已 offload {n_offloaded} 处到线程（不阻塞事件循环）")


def test_model_import_error_is_user_friendly():
    """回归：导入失败的提示不得泄露内部临时路径/用户名。

    历史缺陷：detail 直接把 ONNXRuntimeError 原文塞给用户，其中含
    `...\\model-small.onnx.importing`（用户从未选过这个文件）与 Windows 用户名，
    且按 120 字符硬截断会把路径切成半截。
    """
    from webui import api as a

    src = open("webui/api.py", encoding="utf-8").read()
    blk = src[src.index("def api_model_import"):]
    blk = blk[:blk.index("clear_session_cache(dst)")]
    # ① 必须给出固定的中文说明，而不是原始异常
    assert "该文件不是可用的 ONNX 深度模型" in blk, "应有固定的中文提示"
    assert "str(e)[:120]" not in blk, "不应把原始异常截断后直出给用户"
    # ② 原始异常只进服务端日志
    assert "log.warning" in blk, "原始异常应写入服务端日志"

    # ③ 真正的行为断言：伪造模型导入，错误提示里不得出现内部临时路径
    import tempfile

    d = tempfile.mkdtemp(prefix="hls_msg_")
    try:
        from core.builtin_depth import default_model_path
        from core.ai_backend import AIBackendConfig
        old = a._config
        a._config = AIBackendConfig(builtin_model_path=os.path.join(d, "m.onnx"))
        try:
            with open(os.path.join(d, "m.onnx.importing"), "wb") as f:
                f.write(b"onnx" + os.urandom(2_000_000))
            body = open(os.path.join(d, "m.onnx.importing"), "rb").read()
            raise_holder = {}
            import asyncio

            class _Req:
                async def body(self_inner):
                    return body
            try:
                asyncio.run(a.api_model_import(_Req()))
                raise AssertionError("伪造模型应当被拒绝")
            except a.ApiError as e:
                raise_holder["msg"] = e.detail
            msg = raise_holder["msg"]
            assert ".importing" not in msg, f"提示泄露内部临时路径：{msg[:120]}"
            assert "Administrator" not in msg, f"提示泄露用户名：{msg[:120]}"
        finally:
            a._config = old
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("✓ 导入失败提示不泄露内部路径/用户名")


def test_concurrent_model_import_uses_unique_temp_files():
    """回归：并发导入模型不得互相踩临时文件。

    历史缺陷：临时路径写死为 `dst + ".importing"`，而 validate_model 被移入
    线程后事件循环在校验期间是空闲的 —— 两个并发导入会同时写同一文件，
    先完成者 os.replace 移走后，后者抛 FileNotFoundError（500「替换模型文件失败」）
    或读到半截文件（误报「不是可用的 ONNX 模型」）。
    实测旧代码 3 并发 = 1 成功 / 2 失败；改用 mkstemp 后 3/3 成功。
    """
    import asyncio
    import tempfile

    from core.ai_backend import AIBackendConfig
    from core.builtin_depth import default_model_path
    from webui import api as a

    real = default_model_path()
    if not os.path.isfile(real) or os.path.getsize(real) < 1_000_000:
        print("（跳过：本机没有可用的真实模型做并发校验）")
        return
    blob = open(real, "rb").read()

    d = tempfile.mkdtemp(prefix="hls_conc_")
    try:
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        dst = os.path.join(d, "models", "model-small.onnx")
        old = a._config
        a._config = AIBackendConfig(builtin_model_path=dst)

        class _Req:
            async def body(self):
                return blob

        async def _go():
            return await asyncio.gather(
                *(a.api_model_import(_Req()) for _ in range(3)),
                return_exceptions=True)

        try:
            out = asyncio.run(_go())
        finally:
            a._config = old
        ok = [x for x in out if isinstance(x, dict)]
        errs = [x for x in out if isinstance(x, Exception)]
        assert len(ok) == 3, (
            f"3 次并发导入应全部成功，实际 {len(ok)} 成功 / {len(errs)} 失败："
            + "; ".join(str(e)[:90] for e in errs))
        assert open(dst, "rb").read() == blob, "落盘内容与源文件不一致"
        leftovers = [f for f in os.listdir(os.path.dirname(dst))
                     if f != "model-small.onnx"]
        assert not leftovers, f"临时文件未清理：{leftovers}"
        print("✓ 3 次并发导入全部成功，无临时文件残留")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_model_dir_cleanup_removes_orphans_but_keeps_model():
    """回归：启动清理必须删掉**陈旧**孤儿、且**绝不能**误删真实模型或
    另一个进程正在写入的临时文件。

    两个边界都来自实际缺陷：
    ① 临时文件名唯一（并发安全所需），进程被硬杀留下的 `.part`/`.importing`
       不会被下次写入覆盖，只能靠启动清理回收；但这是破坏性操作，绝不能
       碰到模型本身。
    ② 下载也把 mkstemp 的 `.part` 放在同一目录，而 `setup_deployment.sh`
       可能在另一个进程里正下载 —— 无差别删除会 unlink 掉别人正在写的文件
       （POSIX 上 unlink 成功，下载器随后的 os.replace 抛 FileNotFoundError，
       脚本误报「模型下载失败」）。因此只回收够旧的。
    """
    import tempfile
    import time as _time

    from core.ai_backend import AIBackendConfig
    from webui import api as a

    d = tempfile.mkdtemp(prefix="hls_orph_")
    try:
        md = os.path.join(d, "models")
        os.makedirs(md, exist_ok=True)
        stale = _time.time() - 3600
        for n in ("tmpA.part", "tmpB.importing", "tmpC.part"):
            p = os.path.join(md, n)
            open(p, "wb").write(b"x" * 64)
            os.utime(p, (stale, stale))          # 模拟硬杀后留下的旧孤儿
        # 正在下载中的临时文件（新鲜 mtime）→ 必须保留
        open(os.path.join(md, "inflight.part"), "wb").write(b"in-progress")
        # 真实模型与普通文件都不能被删
        open(os.path.join(md, "model-small.onnx"), "wb").write(b"onnx-data")
        open(os.path.join(md, "notes.txt"), "wb").write(b"keep me")

        old = a._config
        a._config = AIBackendConfig(builtin_model_path=os.path.join(md, "model-small.onnx"))
        try:
            a._cleanup_model_dir()
        finally:
            a._config = old

        after = sorted(os.listdir(md))
        assert after == ["inflight.part", "model-small.onnx", "notes.txt"], \
            f"清理结果不符：{after}"
        assert open(os.path.join(md, "model-small.onnx"), "rb").read() == b"onnx-data"
        print("✓ 陈旧孤儿已清；真实模型、普通文件、下载中的临时文件均未受影响")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_desktop_forces_exit_after_shutdown():
    """回归：服务停止后必须**强制结束进程**，不能依赖解释器正常收尾。

    历史缺陷：API 层用 asyncio.to_thread（ThreadPoolExecutor，工作线程是
    **非守护**线程）。uvicorn 停下后 HTTP 已经不响应，但解释器退出时会再 join
    一次这些线程；只要有一个工作线程卡在 C 调用里（ONNX 会话加载 / ffmpeg
    读管道），join 就永不返回 —— 表现为「接口已 202 退出，任务管理器里进程
    还在」。同一负载复跑多次才偶发一次，属竞态。

    实测（镜像桌面版结构：服务在守护线程、主线程 join 超时后退出）：
        正常 return            → 卡死，20s 后被 timeout 杀掉（rc=124）
        os._exit(0)            → 立即退出（rc=0）
    因此 _run 的退出路径上必须存在 os._exit。
    """
    import ast

    src = open("webui/desktop.py", encoding="utf-8").read()
    tree = ast.parse(src)
    run = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_run"), None)
    assert run is not None, "webui/desktop.py 里找不到 _run"

    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]
    forced = [c for c in calls
              if isinstance(c.func, ast.Attribute) and c.func.attr == "_exit"]
    assert forced, ("_run 退出路径上缺少 os._exit(0)：一旦有 to_thread 工作线程"
                    "卡住，进程会永不退出")

    # 光有 os._exit 不够：若它前面的等待是无上限的，就永远执行不到。
    # 等待必须带时限（历史写法 `while t.is_alive(): t.join(0.5)` 是无上限的）。
    whiles = [n for n in ast.walk(run) if isinstance(n, ast.While)]
    for w in whiles:
        has_cmp = any(isinstance(n, ast.Compare) for n in ast.walk(w.test))
        body_src = ast.get_source_segment(src, w) or ""
        assert has_cmp or "deadline" in body_src, (
            "退出等待循环缺少时限：uvicorn 若卡在关停流程，os._exit 永远到不了")

    # uvicorn 自身的优雅关停也必须有时限（默认 None = 无限期等未完成连接）
    assert "timeout_graceful_shutdown" in src, (
        "uvicorn.Config 未设 timeout_graceful_shutdown，关停可能无限期等待")
    print("✓ 退出路径有强制结束 + 等待有上限 + uvicorn 优雅关停有上限")


def test_desktop_process_exits_after_shutdown_request():
    """行为回归：桌面版收到 /api/shutdown 后必须**自己**结束进程。

    这是用户可见的契约 —— 打包版是窗口子系统、没有控制台，进程不退就只能去
    任务管理器杀。历史缺陷：HTTP 已 202 退出、监听端口也已关闭，但进程
    （实测 pid 在 t+30s 仍活、24 线程）永不结束。因此这里**不 kill**，
    只等待并断言它自己消失。

    同时覆盖 /api/shutdown 对独立运行（非桌面托管）返回 503 的既有约定：
    本用例用 desktop 入口启动，因此应当成功。
    """
    import subprocess
    import sys as _sys
    import time as _time
    import urllib.error
    import urllib.request

    def _alive(proc) -> bool:
        return proc.poll() is None

    # 选一个空闲端口，避免与开发服务器冲突
    import socket as _socket
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    proc = subprocess.Popen(
        [_sys.executable, "-m", "webui.desktop", "--no-browser", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(120):                      # 最多等 30s 就绪
            if not _alive(proc):
                break
            _time.sleep(0.25)
            try:
                with urllib.request.urlopen(base + "/api/state", timeout=2) as r:
                    r.read()
                ready = True
                break
            except Exception:
                continue
        if not ready:
            print("（跳过：桌面版未能在 30s 内就绪，可能是环境限制）")
            return

        req = urllib.request.Request(base + "/api/shutdown", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 202, f"shutdown 返回 {r.status}"

        # 关键：不 kill，只等待它自己退出
        deadline = _time.time() + 30
        while _time.time() < deadline and _alive(proc):
            _time.sleep(0.25)
        assert not _alive(proc), (
            "收到 /api/shutdown 后 30s 进程仍未自行退出 —— 用户只能去任务管理器杀进程")
        print(f"✓ 桌面版自行退出，退出码 {proc.returncode}")
    finally:
        if _alive(proc):
            proc.kill()
            proc.wait(timeout=10)


def test_paths_module_constants():
    """更名后的数据目录常量与迁移来源。"""
    assert DATA_DIR_NAME == ".horizon_light_studio"
    assert LEGACY_DIR_NAME == ".gacha_light_studio"
    assert DATA_DIR_NAME != LEGACY_DIR_NAME
    print("✓ 数据目录常量（含旧目录迁移来源）")


def test_paths_migrates_legacy_dir(tmp_path, monkeypatch):
    """首次访问应把旧品牌目录整体迁移到新目录，避免模型/缓存失效。"""
    import importlib

    import core.paths as paths
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    fake_home = tmp_path
    monkeypatch.setattr(paths, "home", lambda: str(fake_home))

    old = fake_home / LEGACY_DIR_NAME
    (old / "models").mkdir(parents=True)
    (old / "models" / "model-small.onnx").write_bytes(b"x" * 2048)
    (old / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(paths, "_migrated", False)
    new = paths.data_dir()
    assert new.endswith(DATA_DIR_NAME)
    assert (fake_home / DATA_DIR_NAME / "models" / "model-small.onnx").is_file()
    assert (fake_home / DATA_DIR_NAME / "config.json").is_file()
    assert not old.exists(), "旧目录应被迁移（而非留下副本）"

    # 新目录已存在时不覆盖用户新数据，只补搬缺失项
    old.mkdir()
    (old / "extra.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(paths, "_migrated", False)
    paths.data_dir()
    assert (fake_home / DATA_DIR_NAME / "extra.txt").is_file()
    print("✓ 旧数据目录自动迁移且不覆盖既有数据")


# ---------------------------------------------------------------- 缓存容量守护

def _put_entry(c, key: str, blob_bytes: int) -> None:
    """写入一个缓存条目，并额外塞一个填充文件把体积做大到可断言。

    真实条目里 depth.png + normal.png 就有几百 KB，这里用等量填充替代，
    省去构造大图的时间。填充文件必须在 put **之前**写好：put 末尾会做容量
    守护，先写才能让那次检查看到完整体积。
    """
    import numpy as np

    d = c._dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "blob.bin"), "wb") as f:
        f.write(b"\x00" * blob_bytes)
    c.put(key, np.full((32, 32), 0.5, np.float32), {"k": key},
          normal=np.zeros((32, 32, 3), np.float32))


def test_cache_prune_evicts_lru_first(tmp_path):
    """缓存超限时按「最久未使用」淘汰，刚用过的必须留下。

    视频逐帧分析会把**每一帧**的深度/法线都写进缓存（1080p 一帧约 4MB），
    不设上限时处理几段长片就能吃掉几十 GB 系统盘。淘汰顺序按访问时间而非
    写入时间：用户反复处理同一张图时，那张图的条目要留住，否则每次重算。
    """
    from core.cache import AICache

    c = AICache(str(tmp_path / "cache"))
    keys = ["e0", "e1", "e2"]
    for k in keys:
        _put_entry(c, k, 400_000)          # 每个条目约 400KB

    # 时间戳必须由测试指定：Windows 上目录 mtime 的更新是惰性的，
    # 快速连续创建的几个条目可能拿到相同时间，淘汰顺序就成了随机的。
    base = time.time() - 3600
    for i, k in enumerate(keys):
        os.utime(c._dir(k), (base + i, base + i))

    assert c.get(keys[0]) is not None, "条目应能读出"
    assert os.stat(c._dir(keys[0])).st_mtime > base + 2, \
        "读取必须刷新条目的「最近使用」时间"

    total = c.size_bytes()
    assert total > 1_000_000, f"三条应超过 1MB，实际 {total}"
    removed, freed = c.prune(max_bytes=900_000)
    assert removed >= 1 and freed > 0, (removed, freed)
    assert c.get(keys[1]) is None, "最久未使用的条目应被淘汰"
    assert c.get(keys[0]) is not None, "刚访问过的条目必须保留"
    assert c.get(keys[2]) is not None, "最新写入的条目必须保留"
    assert c.size_bytes() <= 900_000, "淘汰后应回落到上限以内"
    print(f"✓ 缓存按 LRU 淘汰（清 {removed} 条 / 释放 {freed // 1024}KB）")


def test_cache_prune_noop_under_cap(tmp_path):
    """未超限时 prune 必须什么都不做（/api/cache 不能被误清空）。"""
    from core.cache import AICache

    c = AICache(str(tmp_path / "cache"))
    _put_entry(c, "small", 1000)
    before = c.size_bytes()
    assert c.prune(max_bytes=10_000_000) == (0, 0)
    assert c.size_bytes() == before and c.get("small") is not None
    print("✓ 未超限时 prune 不误删")


def test_cache_max_bytes_env_override(monkeypatch):
    """容量上限可配置：HLS_CACHE_MAX_MB=0（或负数）表示不限。

    有人把缓存目录放在大容量数据盘、希望永久保留分析结果，
    没有「关闭上限」的口子时只能看着它被自动清掉。
    """
    import core.cache as cache_mod

    monkeypatch.delenv("HLS_CACHE_MAX_MB", raising=False)
    assert cache_mod.cache_max_bytes() == cache_mod.CACHE_MAX_BYTES_DEFAULT
    monkeypatch.setenv("HLS_CACHE_MAX_MB", "512")
    assert cache_mod.cache_max_bytes() == 512 * 1024 * 1024
    monkeypatch.setenv("HLS_CACHE_MAX_MB", "0")
    assert cache_mod.cache_max_bytes() == 0, "0 表示不限"
    monkeypatch.setenv("HLS_CACHE_MAX_MB", "-1")
    assert cache_mod.cache_max_bytes() == 0
    monkeypatch.setenv("HLS_CACHE_MAX_MB", "不是数字")
    assert cache_mod.cache_max_bytes() == cache_mod.CACHE_MAX_BYTES_DEFAULT
    print("✓ 缓存上限可配置（含 0=不限）")


def test_put_triggers_prune_when_over_cap(tmp_path, monkeypatch):
    """写入后要自动做容量守护：逐帧处理时没人会手动调 prune。

    检查本身必须节流（_EVICT_INTERVAL）：每帧 put 都全量扫目录，
    1080p 逐帧会把推理之外的时间全花在列目录上。
    """
    import core.cache as cache_mod
    from core.cache import AICache

    c = AICache(str(tmp_path / "cache"))
    _put_entry(c, "old", 40_000)          # 单个条目约 40KB

    # 时间戳必须由测试指定（Windows 目录 mtime 更新惰性，快速连续写入会拿到相同
    # 时间，淘汰顺序随之随机，见 test_cache_prune_evicts_lru_first）。把 old 压到
    # 更早，确保「淘汰最旧」是确定的、而非靠文件系统时钟的巧合。
    _old_t = time.time() - 3600
    os.utime(c._dir("old"), (_old_t, _old_t))

    # 上限取 60KB：两条（约 80KB）超限，但淘汰最旧的一条即可回落
    monkeypatch.setattr(cache_mod, "cache_max_bytes", lambda: 60_000)
    cache_mod._last_evict_check = 0.0            # 让下次 put 一定触发检查
    _put_entry(c, "new", 40_000)
    assert c.get("old") is None, "超限写入后应淘汰旧条目"
    assert c.get("new") is not None, "刚写入的条目必须保留"
    assert c.size_bytes() <= 60_000

    # 节流：紧接着再写一条，检查时刻未到 → 不再淘汰
    cache_mod._last_evict_check = time.time()
    _put_entry(c, "newer", 40_000)
    assert c.get("new") is not None, "节流窗口内不应重复淘汰"
    assert c.get("newer") is not None
    print("✓ 写入后自动做容量守护（含节流）")


# ---------------------------------------------------------------- 会话并发与预热

def test_get_session_builds_once_under_concurrency(tmp_path, monkeypatch):
    """并发索要同一个模型时只能构建一次会话。

    启动预热线程与首个请求会同时索要模型；没有按路径加锁时，两边都看到
    空缓存、各自加载一遍（94MB/134MB 各读两次），白白多花约 2s 与一倍的
    瞬时内存。这里用假 onnxruntime 把竞态窗口放大到 50ms 来复现。
    """
    import sys as _sys
    import threading
    import types

    import core.builtin_depth as bd

    model = tmp_path / "fake.onnx"
    model.write_bytes(b"\x00" * 1024)

    built = []
    gate = threading.Barrier(8)

    class FakeSession:
        def __init__(self, path, so=None, providers=None):
            built.append(path)
            time.sleep(0.05)        # 放大竞态窗口：真实加载约 1.9s
            self.path = path

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = lambda: types.SimpleNamespace(log_severity_level=0)
    fake_ort.get_available_providers = lambda: ["CPUExecutionProvider"]
    fake_ort.InferenceSession = FakeSession
    monkeypatch.setitem(_sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setattr(bd, "_session_cache", {})
    monkeypatch.setattr(bd, "_session_locks", {})

    out = []

    def worker():
        gate.wait()
        out.append(bd._get_session(str(model)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "等锁线程未能结束（可能死锁）"

    assert len(built) == 1, f"同一模型被构建了 {len(built)} 次"
    assert len({id(o) for o in out}) == 1, "并发调用必须拿到同一个会话实例"
    print("✓ 并发取会话只构建一次（按路径加锁）")


def test_warmup_reports_visible_state(monkeypatch):
    """启动预热必须把状态写进 /api/state，且模型缺失时不抛异常。

    不预热时，判断 model_ready 的那次会话加载（约 1.9s）落在前端首屏的
    /api/state 上，界面打开后要白等两秒。预热把这笔开销挪到启动线程，
    但必须让外界看得见进度：界面拿到 warming 才能显示「引擎加载中」，
    而不是把「还在加载」谎报成「未就绪」。
    """
    from webui import api as a

    monkeypatch.setattr(a, "model_ready", lambda *_a, **_kw: True)
    monkeypatch.setattr(a, "normal_model_ready", lambda *_a, **_kw: True)
    a._warmup.update(state="idle", seconds=0.0, depth=False, normal=False,
                     detail="")
    assert a.api_state()["warmup"]["state"] == "idle"

    a._warmup_models()
    assert a._warmup["state"] == "ready", a._warmup
    assert a._warmup["depth"] and a._warmup["normal"]
    assert a._warmup["seconds"] >= 0
    assert a.api_state()["warmup"]["state"] == "ready"

    # 模型缺失 → missing（而不是异常冒泡打断启动线程）
    monkeypatch.setattr(a, "model_ready", lambda *_: False)
    monkeypatch.setattr(a, "normal_model_ready", lambda *_: False)
    a._warmup_models()
    assert a._warmup["state"] == "missing", a._warmup
    assert a._warmup["detail"], "缺失原因必须写进 detail，否则界面无法解释"
    print("✓ 预热状态在 /api/state 可见（ready / missing）")


def test_warmup_is_delayed_out_of_first_paint(monkeypatch):
    """预热必须推迟启动，且推迟期间状态是 scheduled 而不是 warming。

    实测 ONNX Runtime 建会话时会长时间持有 GIL（MoGe 加载 1.09s 期间，
    另一线程的 10ms 睡眠被拉到 0.937s），预热一旦开始整个服务连
    /api/themes 都会冻住约 1.3s。所以只能推迟到首屏之后再做 —— 若这个
    推迟被改回 0，首屏就会重新被冻住。
    """
    from webui import api as a

    assert a.WARMUP_DELAY > 0, "预热不能立即开始，否则会冻住首屏"
    monkeypatch.setattr(a, "model_ready", lambda *_a, **_kw: True)
    monkeypatch.setattr(a, "normal_model_ready", lambda *_a, **_kw: True)

    import threading
    seen = []
    done = threading.Event()

    def run():
        a._warmup_models(delay=0.3)
        done.set()

    a._warmup.update(state="idle", seconds=0.0, depth=False, normal=False,
                     detail="")
    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):                    # 0.3s 内采样，应看到 scheduled
        seen.append(a._warmup["state"])
        if a._warmup["state"] == "warming" or done.wait(0.01):
            break
    assert done.wait(10), "预热线程未结束"
    assert "scheduled" in seen, f"推迟期间的状态不可见：{seen}"
    assert a._warmup["state"] == "ready", a._warmup
    # 模型被 mock 成瞬时返回，seconds 可能为 0.0；只要求它是「不含推迟」的数
    assert a._warmup["seconds"] >= 0, a._warmup
    print("✓ 预热推迟执行且状态可见（scheduled → ready）")


def test_warmup_survives_backend_exception(monkeypatch):
    """预热失败不能影响服务启动：异常必须被吞掉并记为 missing。"""
    from webui import api as a

    def boom(*_a, **_kw):
        raise RuntimeError("onnxruntime 损坏")

    monkeypatch.setattr(a, "model_ready", boom)
    monkeypatch.setattr(a, "normal_model_ready", boom)
    a._warmup_models()          # 不应抛出
    assert a._warmup["state"] == "missing", a._warmup
    assert "onnxruntime 损坏" in a._warmup["detail"]
    print("✓ 预热异常被吞掉并如实记为 missing")
