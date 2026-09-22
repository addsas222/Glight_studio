# -*- coding: utf-8 -*-
"""专业模式「节点图」：用户连线搭出光照流程，图求值为一个 RenderParams。

设计约束（务必保持）：
  * 本模块是纯数据 / 纯计算模块：不依赖 Qt、FastAPI、网络，也不写任何文件。
  * ``evaluate`` 是确定性的且无副作用：绝不修改传入的 ``base``，也不修改工作流本身；
    每次求值都从 ``copy.deepcopy`` 的副本开始。
  * 拓扑求值使用 Kahn 算法；同层（入度同时归零）的节点按其在 ``wf.nodes`` 中的
    下标升序取出，因此同一个图无论 JSON 里边的书写顺序如何，结果都完全一致。

求值语义：
  * 起点：``base``（默认 ``get_preset("标准棚拍")``）的深拷贝。
  * ``light``：把 ``node.params`` 组装成一个 ``Light`` 追加到 ``params.lights``
    （只认 ``Light`` 的已知字段，未知键忽略；数值越界按引擎范围收敛：
    强度 0~4、色温 1500~12000K、半径 0.05~1.5，另有方向 / 位置的安全范围）。
  * ``auto_light``：把 ``params["prompt"]``（或 ``params["text"]``）交给
    ``core.autolight`` 生成光源（优先确定性的 ``parse_offline``，且强制关闭
    云端调用）。该模块 **惰性导入**：不存在、入口缺失或返回为空时安静地不贡献
    任何光源，绝不抛异常（这样本模块可以先于 autolight 落地）。
  * ``global``：把 ``params`` 中存在的标量渲染字段写回（环境光强度 / 色温、曝光、
    阴影风格、光照模式、阴影强度、高光强度、原图保真度），同样限幅。
  * ``reference``：``params["features"]`` 交给 ``core.reference.apply_reference_offset``
    做保守迁移；特征不完整时安静跳过。
  * ``input`` / ``depth`` / ``output`` / ``pick`` 是结构节点，不修改 RenderParams
    （``pick`` 由界面交互层消费，批量求值时不动参数）。

校验：``validate`` 返回中文错误列表，``[]`` 表示可以求值。存在未知节点类型、
重复 id、悬空连线、自环或环时，``evaluate`` 直接抛 ``ValueError``（附带同样的
中文消息），绝不进入无限循环。
"""
from __future__ import annotations

import copy
import heapq
import inspect
import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .presets import get_preset
from .types import Light, RenderParams

# ---------------------------------------------------------------- 节点类型表

NODE_TYPES: Dict[str, dict] = {
    "input": {
        "name": "图像/视频输入",
        "category": "输入",
        "inputs": [],
        "outputs": [{"id": "out", "label": "图像"}],
    },
    "depth": {
        "name": "AI 深度",
        "category": "AI",
        "inputs": [{"id": "in", "label": "图像"}],
        "outputs": [{"id": "out", "label": "深度"}],
    },
    "auto_light": {
        "name": "AI 自动打光",
        "category": "AI",
        "inputs": [{"id": "in", "label": "深度"}],
        "outputs": [{"id": "out", "label": "光照"}],
    },
    "light": {
        "name": "光源",
        "category": "灯光",
        "inputs": [{"id": "in", "label": "光照"}],
        "outputs": [{"id": "out", "label": "光照"}],
    },
    "global": {
        "name": "全局光照",
        "category": "灯光",
        "inputs": [{"id": "in", "label": "光照"}],
        "outputs": [{"id": "out", "label": "光照"}],
    },
    "pick": {
        "name": "点击拾取锚定",
        "category": "交互",
        "inputs": [{"id": "in", "label": "图像"}],
        "outputs": [{"id": "out", "label": "锚定"}],
    },
    "reference": {
        "name": "参考图迁移",
        "category": "参考",
        "inputs": [{"id": "in", "label": "光照"}, {"id": "ref", "label": "参考图"}],
        "outputs": [{"id": "out", "label": "光照"}],
    },
    "output": {
        "name": "输出",
        "category": "输出",
        "inputs": [{"id": "in", "label": "光照"}],
        "outputs": [],
    },
}

# 与引擎一致的光源数值范围（types.Light 的字段注释 / render.py 的实际用法）
LIGHT_RANGES = {
    "dx": (-1.0, 1.0),
    "dy": (-1.0, 1.0),
    "dz": (-1.0, 1.0),
    "px": (0.0, 1.0),
    "py": (0.0, 1.0),
    "pz": (0.0, 3.0),
    "intensity": (0.0, 4.0),
    "kelvin": (1500.0, 12000.0),
    "radius": (0.05, 1.5),
}

# 全局节点可写的标量字段 -> (下限, 上限)
GLOBAL_RANGES = {
    "ambient_intensity": (0.0, 2.0),
    "ambient_kelvin": (1500.0, 12000.0),
    "exposure": (0.2, 2.5),
    "shadow_strength": (0.0, 1.0),
    "specular_strength": (0.0, 1.0),
    "tone_preserve": (0.0, 1.0),
}

# 全局节点可写的枚举字段 -> 允许取值
GLOBAL_ENUMS = {
    "shadow_mode": ("hard", "soft"),
    "lighting_mode": ("linear", "hq"),
}

_LIGHT_FIELDS = frozenset(Light.__dataclass_fields__)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Node:
    """节点图中的单个节点。``params`` 的键由节点类型决定。"""

    id: str
    type: str
    name: str = ""
    params: dict = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "params": copy.deepcopy(self.params),
            "x": self.x,
            "y": self.y,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        raw_params = d.get("params")
        return cls(
            id=str(d.get("id", "")),
            type=str(d.get("type", "")),
            name=str(d.get("name", "")),
            params=dict(raw_params) if isinstance(raw_params, dict) else {},
            x=_to_float(d.get("x"), 0.0),
            y=_to_float(d.get("y"), 0.0),
        )


@dataclass
class Edge:
    """一条连线：``src`` 的输出端口 ``src_port`` 接到 ``dst`` 的输入端口 ``dst_port``。"""

    src: str
    dst: str
    src_port: str = "out"
    dst_port: str = "in"

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "src_port": self.src_port,
            "dst_port": self.dst_port,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(
            src=str(d.get("src", "")),
            dst=str(d.get("dst", "")),
            src_port=str(d.get("src_port", "out")),
            dst_port=str(d.get("dst_port", "in")),
        )


@dataclass
class Workflow:
    """一张节点图。``nodes`` 的顺序参与拓扑排序的并列决胜，请勿随意打乱。"""

    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    name: str = "未命名工作流"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        """从 dict 构造；缺失字段用默认值，非法条目被跳过而不是抛异常。

        结构性问题（未知类型、悬空连线、环）留给 :func:`validate` 报告。
        """
        if not isinstance(d, dict):
            raise ValueError("工作流 JSON 顶层必须是对象")
        raw_nodes = d.get("nodes") or []
        raw_edges = d.get("edges") or []
        nodes = [Node.from_dict(n) for n in raw_nodes if isinstance(n, dict)]
        edges = [Edge.from_dict(e) for e in raw_edges if isinstance(e, dict)]
        return cls(nodes=nodes, edges=edges, name=str(d.get("name", "未命名工作流")))

    @classmethod
    def from_json(cls, s: str) -> "Workflow":
        return cls.from_dict(json.loads(s))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 小工具


def _to_float(v, default: Optional[float] = None) -> Optional[float]:
    """尽最大努力把 JSON 里的值转成有限浮点数；失败返回 default。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else default
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return default
        return f if math.isfinite(f) else default
    return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _sanitize_light(light: Light) -> Light:
    """返回一个把所有数值收敛到引擎范围内的 Light 副本。"""
    out = Light.from_dict(light.to_dict())
    if out.kind not in ("directional", "point"):
        out.kind = "directional"
    for fname, (lo, hi) in LIGHT_RANGES.items():
        val = _to_float(getattr(out, fname, None))
        if val is None:
            val = getattr(Light(), fname)
        setattr(out, fname, _clamp(val, lo, hi))
    out.visible = bool(out.visible)
    return out


def _light_from_params(params: dict, fallback_name: str = "") -> Light:
    """从节点参数组装光源：只认 Light 的已知字段，未知键忽略。

    光源名优先级：``params["name"]`` > 节点名 > ``"光源"``。
    """
    raw = {k: v for k, v in params.items() if k in _LIGHT_FIELDS and k != "name"}
    light = Light.from_dict(raw)
    given_name = params.get("name")
    if isinstance(given_name, str) and given_name.strip():
        light.name = given_name
    else:
        light.name = fallback_name or "光源"
    return _sanitize_light(light)


def _coerce_lights(raw) -> List[Light]:
    """把 core.autolight 的返回值（LightRig / Light / dict / RenderParams / 列表）规整成光源列表。"""
    if raw is None:
        return []
    if isinstance(raw, RenderParams):
        items = list(raw.lights)
    elif isinstance(raw, Light):
        items = [raw]
    elif isinstance(raw, dict):
        items = list(raw.get("lights") or [])
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif hasattr(raw, "lights"):  # core.autolight.LightRig 等「带 lights 的结果对象」
        items = list(getattr(raw, "lights") or [])
    else:
        return []

    out: List[Light] = []
    for item in items:
        if isinstance(item, Light):
            out.append(_sanitize_light(item))
        elif isinstance(item, dict):
            out.append(_light_from_params(item))
    return out


def _autolight_from_prompt(prompt: str) -> List[Light]:
    """惰性调用 core.autolight 生成光源；任何缺失 / 不兼容都安静降级为空列表。

    优先使用确定性的离线解析 ``parse_offline``；若该模块只提供别的入口
    （``rig_from_text`` 等），也一并尝试。若入口带 ``use_cloud`` 参数则强制
    关掉，保证批量求值不发网络请求。
    """
    if not (isinstance(prompt, str) and prompt.strip()):
        return []
    try:
        from . import autolight  # 惰性导入：该模块可能尚未存在
    except Exception:
        return []

    for fname in ("parse_offline", "rig_from_text", "lights_from_prompt",
                  "from_prompt", "generate_lights", "lights"):
        fn = getattr(autolight, fname, None)
        if not callable(fn):
            continue
        try:
            kwargs = {}
            try:
                names = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                names = {}
            if "use_cloud" in names:
                kwargs["use_cloud"] = False
            raw = fn(prompt, **kwargs)
        except Exception:
            continue
        lights = _coerce_lights(raw)
        if lights:
            return lights
    return []


# ---------------------------------------------------------------- 公开 API


def node_types() -> List[dict]:
    """给界面调色板用的节点类型清单（保持 NODE_TYPES 的声明顺序）。"""
    return [
        {
            "type": t,
            "name": spec["name"],
            "category": spec["category"],
            "inputs": copy.deepcopy(spec["inputs"]),
            "outputs": copy.deepcopy(spec["outputs"]),
        }
        for t, spec in NODE_TYPES.items()
    ]


def _topo_order(wf: Workflow) -> List[int]:
    """Kahn 拓扑排序，返回节点下标的求值顺序。

    并列（入度同时为 0）时按下标升序，保证结果可复现。存在环时返回的顺序会
    少于节点数 —— 调用方应先跑 :func:`validate`。
    """
    n = len(wf.nodes)
    index = {node.id: i for i, node in enumerate(wf.nodes)}
    # 自环 / 重复 id 在这里被自然消解：只统计两端都存在且指向不同下标的边
    indeg = [0] * n
    succ: Dict[int, List[int]] = {}
    for e in wf.edges:
        s = index.get(e.src)
        d = index.get(e.dst)
        if s is None or d is None or s == d:
            continue
        succ.setdefault(s, []).append(d)
        indeg[d] += 1

    heap = [i for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    order: List[int] = []
    while heap:
        i = heapq.heappop(heap)
        order.append(i)
        for j in succ.get(i, ()):
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(heap, j)
    return order


def validate(wf: Workflow) -> List[str]:
    """结构校验，返回中文错误列表；``[]`` 表示可以求值。"""
    errors: List[str] = []
    if not wf.nodes:
        errors.append("工作流为空：至少需要一个节点")
        return errors

    # 重复 id
    seen: Dict[str, int] = {}
    for node in wf.nodes:
        if not node.id:
            errors.append("存在缺少 id 的节点")
            continue
        if node.id in seen:
            errors.append("节点 id 重复：%s" % node.id)
        seen[node.id] = seen.get(node.id, 0) + 1

    # 未知类型
    for node in wf.nodes:
        if node.type not in NODE_TYPES:
            errors.append("节点「%s」类型未知：%s" % (node.id or "(无 id)", node.type))

    # 悬空连线 / 自环
    known = {node.id for node in wf.nodes if node.id}
    for e in wf.edges:
        if e.src not in known:
            errors.append("连线起点不存在：%s" % (e.src or "(空)"))
        if e.dst not in known:
            errors.append("连线终点不存在：%s" % (e.dst or "(空)"))
        if e.src and e.src == e.dst:
            errors.append("节点「%s」存在自环" % e.src)

    # 环 / 自环
    if len(_topo_order(wf)) != len(wf.nodes):
        errors.append("工作流存在环路，无法求值")
    return errors


def evaluate(wf: Workflow, base: Optional[RenderParams] = None) -> RenderParams:
    """按拓扑顺序求值，返回一份全新的 :class:`RenderParams`。

    ``base`` 不会被修改（默认取「标准棚拍」预设）。图非法时抛 ``ValueError``。
    """
    errors = validate(wf)
    if errors:
        raise ValueError("；".join(errors))

    params = copy.deepcopy(base if base is not None else get_preset("标准棚拍"))
    params.lights = list(params.lights)

    for i in _topo_order(wf):
        node = wf.nodes[i]
        ntype = node.type

        if ntype == "light":
            params.lights.append(_light_from_params(node.params, node.name))

        elif ntype == "auto_light":
            prompt = node.params.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                prompt = node.params.get("text")
            params.lights.extend(_autolight_from_prompt(prompt))

        elif ntype == "global":
            for key, (lo, hi) in GLOBAL_RANGES.items():
                if key not in node.params:
                    continue
                val = _to_float(node.params[key])
                if val is not None:
                    setattr(params, key, _clamp(val, lo, hi))
            for key, allowed in GLOBAL_ENUMS.items():
                val = node.params.get(key)
                if val in allowed:
                    setattr(params, key, val)

        elif ntype == "reference":
            features = node.params.get("features")
            if isinstance(features, dict):
                from .reference import apply_reference_offset
                try:
                    params = apply_reference_offset(params, features)
                except (KeyError, TypeError, ValueError):
                    pass  # 参考特征不完整：安静跳过，不影响其余节点

        # input / depth / pick / output 为结构节点，不修改渲染参数

    return params


def default_workflow() -> Workflow:
    """默认工作流：输入 → 深度 → 主光 → 环境光 → 输出（已连好，且通过校验）。"""
    nodes = [
        Node(id="input", type="input", name="图像/视频输入", x=0.0, y=160.0),
        Node(id="depth", type="depth", name="AI 深度", x=220.0, y=160.0),
        Node(
            id="light_main",
            type="light",
            name="主光",
            params={
                "name": "主光",
                "kind": "directional",
                "dx": -0.5,
                "dy": -0.6,
                "dz": 0.7,
                "intensity": 1.2,
                "kelvin": 5200,
                "radius": 0.5,
            },
            x=440.0,
            y=160.0,
        ),
        Node(
            id="global_amb",
            type="global",
            name="环境光",
            params={
                "ambient_intensity": 0.5,
                "ambient_kelvin": 6500,
                "shadow_mode": "soft",
                "lighting_mode": "linear",
            },
            x=660.0,
            y=160.0,
        ),
        Node(id="output", type="output", name="输出", x=880.0, y=160.0),
    ]
    edges = [
        Edge(src="input", dst="depth"),
        Edge(src="depth", dst="light_main"),
        Edge(src="light_main", dst="global_amb"),
        Edge(src="global_amb", dst="output"),
    ]
    return Workflow(nodes=nodes, edges=edges, name="默认工作流")


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":  # python -m core.workflow
    import sys

    _failures = []

    def _check(cond, label):
        print(("PASS  " if cond else "FAIL  ") + label)
        if not cond:
            _failures.append(label)

    # 1. 默认工作流：校验通过、可求值、结果 JSON 往返
    wf = default_workflow()
    _check(validate(wf) == [], "default_workflow 通过 validate")
    p = evaluate(wf)
    _check(isinstance(p, RenderParams), "evaluate 返回 RenderParams")
    _check(len(p.lights) >= 1, "默认工作流至少产出一个光源")
    rt = RenderParams.from_json(p.to_json())
    _check(rt.to_dict() == p.to_dict(), "渲染参数 JSON 往返一致")
    _check(p.ambient_intensity == 0.5 and p.ambient_kelvin == 6500,
           "默认 global 节点写入环境光参数")

    # 2. light 节点：强度 99 → 收敛到 4，未知键忽略
    b0 = RenderParams(lights=[])
    wf_light = Workflow(nodes=[Node(id="l", type="light", name="猛光",
                                    params={"intensity": 99, "kelvin": 99999,
                                            "radius": -3, "bogus": "x"})],
                        edges=[])
    p2 = evaluate(wf_light, b0)
    _check(len(p2.lights) == 1 and p2.lights[0].intensity == 4.0, "强度 99 → 4.0")
    _check(p2.lights[0].kelvin == 12000.0, "色温 99999 → 12000")
    _check(p2.lights[0].radius == 0.05, "半径 -3 → 0.05")
    _check(p2.lights[0].name == "猛光", "节点名作为光源名兜底")
    _check(not hasattr(p2.lights[0], "bogus"), "未知参数键被忽略")

    # 2b. 非法光源载荷（非数值 / bool）不抛异常
    wf_bad = Workflow(nodes=[Node(id="l", type="light",
                                  params={"intensity": "abc", "kind": "laser",
                                          "visible": 0})], edges=[])
    p_bad = evaluate(wf_bad, RenderParams(lights=[]))
    _check(p_bad.lights[0].intensity == Light().intensity, "非数值强度回退默认值")
    _check(p_bad.lights[0].kind == "directional", "未知 kind 回退 directional")
    _check(p_bad.lights[0].visible is False, "visible 0 → False")

    # 3. global 节点：写入并限幅
    wf_glob = Workflow(nodes=[Node(id="g", type="global",
                                   params={"ambient_intensity": 9,
                                           "ambient_kelvin": 10,
                                           "exposure": 100,
                                           "shadow_strength": -1,
                                           "shadow_mode": "硬核",
                                           "lighting_mode": "hq"})], edges=[])
    p3 = evaluate(wf_glob, RenderParams(lights=[]))
    _check(p3.ambient_intensity == 2.0, "环境光强度 9 → 2.0")
    _check(p3.ambient_kelvin == 1500.0, "环境光色温 10 → 1500")
    _check(p3.exposure == 2.5, "曝光 100 → 2.5")
    _check(p3.shadow_strength == 0.0, "阴影强度 -1 → 0.0")
    _check(p3.shadow_mode == "soft", "非法枚举被忽略（保持原值）")
    _check(p3.lighting_mode == "hq", "合法枚举被写入")

    # 4. 两个 light 节点累加，且与节点书写顺序无关
    def mk(pairs):
        return Workflow(
            nodes=[Node(id="n%d" % i, type="light", params={"name": n, "kelvin": k})
                   for i, (n, k) in enumerate(pairs)],
            edges=[])
    pa = evaluate(mk([("甲", 3000), ("乙", 9000)]), RenderParams(lights=[]))
    pb = evaluate(mk([("乙", 9000), ("甲", 3000)]), RenderParams(lights=[]))
    key = lambda pr: sorted((l.name, l.kelvin) for l in pr.lights)
    _check(len(pa.lights) == 2, "两个 light 节点累加为 2 个光源")
    _check(key(pa) == key(pb), "光源累加结果与节点书写顺序无关")
    _check(key(pa) == [("乙", 9000.0), ("甲", 3000.0)], "累加内容正确")

    # 5. 环：validate 非空 且 evaluate 抛 ValueError
    cyc = Workflow(nodes=[Node(id="a", type="light"), Node(id="b", type="global")],
                   edges=[Edge(src="a", dst="b"), Edge(src="b", dst="a")])
    _check(bool(validate(cyc)), "环被 validate 报出")
    _check(any("环" in m for m in validate(cyc)), "环的错误消息为中文")
    try:
        evaluate(cyc)
        _check(False, "环上 evaluate 抛 ValueError")
    except ValueError as exc:
        _check("环" in str(exc), "环上 evaluate 抛 ValueError（%s）" % exc)
    # 自环同样被拦下
    selfloop = Workflow(nodes=[Node(id="a", type="light")], edges=[Edge(src="a", dst="a")])
    _check(bool(validate(selfloop)), "自环被 validate 报出")

    # 6. 未知节点类型
    unk = Workflow(nodes=[Node(id="u", type="teleport")], edges=[])
    msgs = validate(unk)
    _check(bool(msgs) and any("未知" in m for m in msgs), "未知节点类型被 validate 报出")
    try:
        evaluate(unk)
        _check(False, "未知类型 evaluate 抛 ValueError")
    except ValueError:
        _check(True, "未知类型 evaluate 抛 ValueError")
    # 悬空连线
    dang = Workflow(nodes=[Node(id="a", type="light")], edges=[Edge(src="a", dst="ghost")])
    _check(any("不存在" in m for m in validate(dang)), "悬空连线被 validate 报出")
    # 空图
    _check(bool(validate(Workflow())), "空工作流被 validate 报出")
    # 重复 id
    dup = Workflow(nodes=[Node(id="a", type="light"), Node(id="a", type="light")], edges=[])
    _check(any("重复" in m for m in validate(dup)), "重复 id 被 validate 报出")

    # 7. evaluate 不修改 base，也不修改工作流
    base = get_preset("标准棚拍")
    before = base.to_dict()
    evaluate(wf, base)
    _check(base.to_dict() == before, "evaluate 不修改传入的 base")
    wf_before = wf.to_dict()
    evaluate(wf)
    _check(wf.to_dict() == wf_before, "evaluate 不修改工作流本身")
    # 返回的对象与 base 不共享光源列表
    res = evaluate(wf, base)
    res.lights.append(Light(name="外部添加"))
    _check(base.to_dict() == before, "evaluate 返回值与 base 无共享引用")

    # 8. reference 节点：特征不完整时安静跳过，完整时生效
    wf_ref = Workflow(nodes=[Node(id="r", type="reference", params={"features": {"oops": 1}})],
                      edges=[])
    _check(evaluate(wf_ref, RenderParams(lights=[])).lights == [], "残缺参考特征安静跳过")
    wf_ref2 = Workflow(nodes=[Node(id="r", type="reference",
                                   params={"features": {"light_dx": 0.6, "light_dy": -0.4,
                                                        "kelvin": 9000.0, "brightness": 0.72}})],
                       edges=[])
    p4 = evaluate(wf_ref2, get_preset("标准棚拍"))
    _check(p4.reference_offset is not None and p4.lights, "完整参考特征被应用")

    # 9. auto_light：core.autolight 存在时产出光源；不存在 / 出错时安静降级
    import core as _core
    try:
        from . import autolight as _autolight_mod  # noqa: F401
        _auto_ok = True
    except ImportError:
        _auto_ok = False

    wf_auto = Workflow(nodes=[Node(id="a", type="auto_light", params={"prompt": "黄昏逆光"})],
                       edges=[])
    if _auto_ok:
        p_auto = evaluate(wf_auto, RenderParams(lights=[]))
        _check(len(p_auto.lights) >= 1, "auto_light 经 core.autolight 产出光源")
        _check(all(0.0 <= l.intensity <= 4.0 and 1500.0 <= l.kelvin <= 12000.0
                   and 0.05 <= l.radius <= 1.5 for l in p_auto.lights),
               "auto_light 产出的光源全部在引擎范围内")
    else:
        print("SKIP  core.autolight 尚未落地，跳过正向用例")
        _check(evaluate(wf_auto, RenderParams(lights=[])).lights == [],
               "autolight 缺失时安静降级（不抛异常）")

    # 模拟 autolight 缺失：即便本机已有该模块，也必须能安静降级
    _saved_mod = sys.modules.pop("core.autolight", None)
    _saved_attr = getattr(_core, "autolight", None)
    _had_attr = hasattr(_core, "autolight")
    if _had_attr:
        delattr(_core, "autolight")
    sys.modules["core.autolight"] = None  # None 会让 import 抛 ImportError
    try:
        _check(evaluate(wf_auto, RenderParams(lights=[])).lights == [],
               "autolight 缺失时安静降级（不抛异常）")
    finally:
        sys.modules.pop("core.autolight", None)
        if _saved_mod is not None:
            sys.modules["core.autolight"] = _saved_mod
        if _had_attr:
            setattr(_core, "autolight", _saved_attr)

    wf_auto2 = Workflow(nodes=[Node(id="a", type="auto_light", params={})], edges=[])
    _check(evaluate(wf_auto2, RenderParams(lights=[])).lights == [], "无 prompt 时不贡献光源")

    # autolight 入口抛异常也要安静降级
    class _Boom:
        def parse_offline(self, text):
            raise RuntimeError("模型崩溃")

        def rig_from_text(self, *a, **k):
            raise RuntimeError("模型崩溃")

    _boom = _Boom()
    _saved_mod2 = sys.modules.get("core.autolight")
    _saved_attr2 = getattr(_core, "autolight", None)
    _had_attr2 = hasattr(_core, "autolight")
    sys.modules["core.autolight"] = _boom
    setattr(_core, "autolight", _boom)
    try:
        _check(evaluate(wf_auto, RenderParams(lights=[])).lights == [],
               "autolight 抛异常时安静降级")
    finally:
        if _saved_mod2 is None:
            sys.modules.pop("core.autolight", None)
        else:
            sys.modules["core.autolight"] = _saved_mod2
        if _had_attr2:
            setattr(_core, "autolight", _saved_attr2)
        elif hasattr(_core, "autolight"):
            delattr(_core, "autolight")

    # 10. JSON 往返 / 结构节点不产生副作用
    wf2 = Workflow.from_json(wf.to_json())
    _check(wf2.to_dict() == wf.to_dict(), "工作流 JSON 往返一致")
    solo = Workflow(nodes=[Node(id="p", type="pick"), Node(id="o", type="output")],
                    edges=[Edge(src="p", dst="o")])
    _check(evaluate(solo, get_preset("冷色月夜")).to_dict()
           == get_preset("冷色月夜").to_dict(), "pick/output 不修改渲染参数")
    try:
        Workflow.from_json("{不是 JSON")
        _check(False, "损坏 JSON 抛异常")
    except ValueError:
        _check(True, "损坏 JSON 抛 ValueError/json 解码错误")
    try:
        Workflow.from_json("[1, 2]")
        _check(False, "非对象 JSON 抛异常")
    except ValueError:
        _check(True, "非对象 JSON 抛 ValueError")
    _check(Workflow.from_dict({"nodes": ["坏条目", {"type": "light"}], "edges": [7]}).to_dict()
           == {"name": "未命名工作流",
               "nodes": [{"id": "", "type": "light", "name": "", "params": {}, "x": 0.0, "y": 0.0}],
               "edges": []},
           "非法条目被跳过而非崩溃")
    _check(bool(validate(Workflow.from_dict({"nodes": [{"type": "light"}]}))),
           "缺 id 的节点被 validate 报出")

    # 11. 节点类型表
    nts = node_types()
    _check([t["type"] for t in nts] == list(NODE_TYPES), "node_types 顺序稳定")
    _check(all({"type", "name", "category", "inputs", "outputs"} <= set(t) for t in nts),
           "node_types 字段完整")
    _check(json.loads(json.dumps(nts, ensure_ascii=False)) == nts, "node_types 可 JSON 序列化")

    print("-" * 48)
    if _failures:
        print("FAILED %d 项：" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)
