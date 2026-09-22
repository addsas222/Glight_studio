# -*- coding: utf-8 -*-
"""AI 自动打光：输入一句自然语言描述，输出可编辑的**光照节点**（Light 列表）。

设计原则（与产品规格一致）：
  1. **离线优先**：核心能力由本地确定性词表映射完成——中文关键词/场景词 →
     预设基调 + 光源参数。不联网、不需要模型权重，断网也必然出结果。
  2. **云端可选增强**：仅当用户显式启用云端且填了密钥时，才调用 LLM 做更细
     解析（复用 core.ai_backend 的 OpenAI 兼容接口）；失败/超时/不可解析时
     **自动回落**到离线结果，绝不阻塞。
  3. **输出即节点**：结果统一为 `LightRig`，其 `lights` 是普通 `Light` 对象，
     可直接进入「专业模式」的节点图，也可转成 `RenderParams`。

典型用法：
    rig = rig_from_text("黄昏时分的舞台逆光，暖橘色主光从左后方打来，冷蓝补光勾勒轮廓")
    params = to_render_params(rig)      # 直接渲染
    nodes = rig_to_nodes(rig)           # 灌进工作流节点图
"""
from __future__ import annotations

import copy
import json
import math
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .ai_backend import AIBackendConfig
from .presets import get_preset
from .types import Light, RenderParams

# ---------------------------------------------------------------- 词表

# 场景 → 预设基调（预设名必须存在于 core.presets.PRESETS）
SCENE_WORDS: Sequence[Tuple[Sequence[str], str]] = (
    (("舞台", "聚光", "演唱会", "live", "spotlight"), "舞台聚光"),
    (("霓虹", "赛博", "cyber", "夜市", "都市夜", "粉青"), "霓虹氛围"),
    (("月夜", "月光", "夜晚", "黑夜", "深夜", "冷夜"), "冷色月夜"),
    (("黄昏", "夕阳", "日落", "逆光", "傍晚", "落日"), "逆光黄昏"),
    (("棚拍", "摄影棚", "柔光箱", "影棚", "证件", "产品", "标准", "中性"), "标准棚拍"),
)

# 色温词 → 目标开尔文
WARM_WORDS: Sequence[str] = ("暖", "橘", "橙", "黄", "金", "烛", "火", "夕阳",
                             "钨丝", "琥珀", "蜜")
COOL_WORDS: Sequence[str] = ("冷", "蓝", "青", "月", "冰", "靛", "银", "霜",
                             "霓虹青")
NEUTRAL_WORDS: Sequence[str] = ("白", "中性", "日光", "自然", "标准白", "柔白")

K_WARM, K_NEUTRAL, K_COOL = 3000.0, 5600.0, 9200.0

# 方向词（图像平面：dx 向右为正，dy 向下为正，dz 朝观察者）
LEFT_WORDS = ("左", "左侧", "左边", "左上", "左下")
RIGHT_WORDS = ("右", "右侧", "右边", "右上", "右下")
# 注意：只收「上/下」的方向性搭配（右上、上方…），不收裸“上/下”，
# 否则「晚上」「身上」「早上」这类词会被误判成顶光/底光。
UP_WORDS = ("顶部", "上方", "上面", "头顶", "高处", "顶光", "正上", "偏上",
            "左上", "右上", "自上", "从上方")
DOWN_WORDS = ("地面", "下方", "下面", "底部", "地面反光", "脚下", "正下",
              "偏下", "左下", "右下", "自下", "从下方")
BACK_WORDS = ("逆光", "背后", "后方", "后面", "背光", "轮廓光", "rim light")

# 强度词
STRONG_WORDS = ("强", "亮", "强烈", "刺眼", "高亮", "主光", "硬")
SOFT_WORDS = ("柔", "弱", "微弱", "柔和", "轻", "补光", "环境", "淡淡", "薄")

# 常见光源角色
RIM_WORDS = ("轮廓", "边缘光", "轮廓光", "rim", "发丝")
FILL_WORDS = ("补光", "填充", "辅光", "fill")
KEY_WORDS = ("主光", "key", "keylight", "主要")

# 明确写出的开尔文，例如 “5600K”
_RE_KELVIN = re.compile(r"(\d{4,5})\s*[kK]")


def _hit(text: str, words: Sequence[str]) -> bool:
    return any(w in text for w in words)


def _first_scene(text: str) -> str:
    for words, preset in SCENE_WORDS:
        if _hit(text, words):
            return preset
    return "标准棚拍"


def _kelvin_for(text: str, default: float) -> float:
    m = _RE_KELVIN.search(text)
    if m:
        return float(min(max(int(m.group(1)), 1500), 12000))
    if _hit(text, WARM_WORDS):
        return K_WARM
    if _hit(text, COOL_WORDS):
        return K_COOL
    if _hit(text, NEUTRAL_WORDS):
        return K_NEUTRAL
    return default


def _direction_for(text: str) -> Tuple[float, float, float]:
    """把方向词合成一个指向光源的单位向量（图像平面坐标）。"""
    dx = dy = 0.0
    dz = 0.65
    if _hit(text, LEFT_WORDS):
        dx -= 1.0
    if _hit(text, RIGHT_WORDS):
        dx += 1.0
    if _hit(text, UP_WORDS):
        dy -= 1.0
    if _hit(text, DOWN_WORDS):
        dy += 1.0
    if _hit(text, BACK_WORDS):
        # 逆光：光源在主体后方 → 观察者侧分量变小
        dz = 0.28
    if dx == 0.0 and dy == 0.0:
        dx, dy = -0.5, -0.5          # 默认左上前方
    n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    return dx / n, dy / n, max(dz / n, 0.05)


def _intensity_for(text: str, base: float) -> float:
    v = base
    if _hit(text, STRONG_WORDS):
        v *= 1.7
    if _hit(text, SOFT_WORDS):
        v *= 0.6
    return float(min(max(v, 0.05), 4.0))


# ---------------------------------------------------------------- 数据模型


@dataclass
class LightRig:
    """一次自动打光的结果：一组可编辑的光源节点 + 全局光照。"""

    name: str = "AI 自动打光"
    prompt: str = ""
    lights: List[Light] = field(default_factory=list)
    ambient_intensity: float = 0.45
    ambient_kelvin: float = 6500.0
    shadow_mode: str = "soft"
    lighting_mode: str = "linear"
    source: str = "offline"          # offline | cloud
    note: str = ""                   # 中文说明（界面展示）

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lights"] = [l.to_dict() for l in self.lights]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LightRig":
        d = dict(d)
        d["lights"] = [Light.from_dict(x) for x in d.get("lights", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------- 离线解析


def parse_offline(text: str) -> LightRig:
    """把描述文本确定性地映射为一组光源（纯词表，离线可用）。

    解析顺序：场景基调 → 主光（方向/色温/强度）→ 按描述词追加轮廓光/补光 →
    全局环境光。同样的输入永远得到同样的输出。
    """
    t = (text or "").strip()
    preset_name = _first_scene(t)
    base = get_preset(preset_name)

    dx, dy, dz = _direction_for(t)
    kelvin = _kelvin_for(t, base.lights[0].kelvin if base.lights else K_NEUTRAL)
    intensity = _intensity_for(t, 1.0)

    lights: List[Light] = [Light(name="AI 主光", kind="directional",
                                 dx=dx, dy=dy, dz=dz,
                                 intensity=intensity, kelvin=kelvin,
                                 radius=0.45)]

    # 描述里提到冷色 → 补光用冷色；提到暖色 → 补光偏冷做对比
    if _hit(t, COOL_WORDS):
        lights.append(Light(name="AI 暖补光", dx=0.6, dy=0.35, dz=0.5,
                            intensity=min(intensity * 0.35, 1.2),
                            kelvin=3200.0, radius=0.7))
    elif _hit(t, WARM_WORDS):
        lights.append(Light(name="AI 冷补光", dx=0.6, dy=0.35, dz=0.5,
                            intensity=min(intensity * 0.35, 1.2),
                            kelvin=8200.0, radius=0.7))

    if _hit(t, RIM_WORDS):
        lights.append(Light(name="AI 轮廓光", kind="directional",
                            dx=-dx, dy=-abs(dy) * 0.5, dz=0.35,
                            intensity=min(intensity * 0.8, 3.0),
                            kelvin=min(kelvin + 1500.0, 12000.0),
                            radius=0.4))

    if _hit(t, FILL_WORDS) and not _hit(t, RIM_WORDS):
        lights.append(Light(name="AI 补光", dx=0.7, dy=0.2, dz=0.45,
                            intensity=min(intensity * 0.4, 1.5),
                            kelvin=kelvin, radius=0.7))

    # 环境光：逆光/月夜偏暗，棚拍偏亮
    amb_i = {"逆光黄昏": 0.42, "冷色月夜": 0.28, "舞台聚光": 0.18,
             "霓虹氛围": 0.25, "标准棚拍": 0.55}.get(preset_name, 0.45)
    amb_k = {"逆光黄昏": 4200.0, "冷色月夜": 9500.0, "舞台聚光": 6500.0,
             "霓虹氛围": 8000.0, "标准棚拍": 6500.0}.get(preset_name, 6500.0)
    if _hit(t, COOL_WORDS):
        amb_k = min(amb_k + 1200.0, 12000.0)
    elif _hit(t, WARM_WORDS):
        amb_k = max(amb_k - 1200.0, 2000.0)

    shadow = "hard" if _hit(t, ("硬", "赛璐珞", "强烈", "舞台", "聚光")) else "soft"

    return LightRig(
        name=f"AI 自动打光 · {preset_name}",
        prompt=t,
        lights=lights,
        ambient_intensity=amb_i,
        ambient_kelvin=amb_k,
        shadow_mode=shadow,
        source="offline",
        note=explain_rig(lights, preset_name),
    )


def explain_rig(lights: Sequence[Light], preset_name: str = "") -> str:
    """生成一句中文解析说明，供界面回显。"""
    if not lights:
        return "未解析出光源"
    kinds = []
    for l in lights[:3]:
        if l.name.endswith("主光"):
            kinds.append("主光")
        elif "轮廓" in l.name:
            kinds.append("轮廓光")
        elif "补光" in l.name:
            kinds.append("补光")
    dirs = []
    m = lights[0]
    if m.dx < -0.15:
        dirs.append("左")
    elif m.dx > 0.15:
        dirs.append("右")
    if m.dy < -0.15:
        dirs.append("上")
    elif m.dy > 0.15:
        dirs.append("下")
    if m.dz < 0.4:
        dirs.append("后")
    pos = "".join(dirs) or "正前"
    tone = "暖色" if m.kelvin < 4500 else ("冷色" if m.kelvin > 7000 else "中性")
    head = f"解析：主光 {pos} · {tone} {int(m.kelvin)}K"
    if preset_name:
        head = f"{preset_name} · " + head
    if len(kinds) > 1:
        head += " · " + "+".join(kinds[1:])
    return head


# ---------------------------------------------------------------- 云端增强（可选）


_PROMPT_TEMPLATE = (
    "你是专业的摄影灯光师。请把下面这句中文光照描述解析成 JSON 光照方案，"
    "只输出 JSON，不要任何解释文字。\n"
    "格式：{{\"lights\":[{{\"name\":\"中文名\",\"kind\":\"directional\","
    "\"dx\":-0.5,\"dy\":-0.6,\"dz\":0.7,\"intensity\":1.0,\"kelvin\":5500,"
    "\"radius\":0.5}}],\"ambient_intensity\":0.45,\"ambient_kelvin\":6500,"
    "\"shadow_mode\":\"soft\"}}\n"
    "约束：dx/dy 为正表示光源在图像右侧/下方，dz 为朝向观察者的分量(0.05~1)；"
    "intensity 0~4；kelvin 1500~12000；最多 6 个光源。\n"
    "描述：{text}"
)


def _cloud_rig(text: str, config: AIBackendConfig,
               timeout: float = 20.0) -> Optional[LightRig]:
    """调用 OpenAI 兼容接口做 LLM 解析；任何问题返回 None 以便回落到离线。"""
    if not (config.cloud_enabled and config.cloud_api_key and config.cloud_base_url):
        return None
    body = json.dumps({
        "model": config.cloud_model or "qwen-vl-max",
        "messages": [{"role": "user",
                      "content": _PROMPT_TEMPLATE.format(text=text)}],
        "temperature": 0.2,
    }).encode()
    url = config.cloud_base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {config.cloud_api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        content = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        lights = [Light.from_dict(x) for x in data.get("lights", []) if isinstance(x, dict)]
        if not lights:
            return None
        for l in lights:
            l.intensity = float(min(max(l.intensity, 0.05), 4.0))
            l.kelvin = float(min(max(l.kelvin, 1500.0), 12000.0))
            l.radius = float(min(max(l.radius, 0.05), 1.5))
        return LightRig(
            name="AI 自动打光 · 云端解析", prompt=text, lights=lights,
            ambient_intensity=float(min(max(data.get("ambient_intensity", 0.45), 0.0), 2.0)),
            ambient_kelvin=float(min(max(data.get("ambient_kelvin", 6500.0), 2000.0), 12000.0)),
            shadow_mode="hard" if data.get("shadow_mode") == "hard" else "soft",
            source="cloud",
            note=explain_rig(lights))
    except Exception:
        return None


# ---------------------------------------------------------------- 对外入口


def rig_from_text(text: str, base: Optional[RenderParams] = None,
                  config: Optional[AIBackendConfig] = None,
                  use_cloud: bool = False,
                  log: Optional[Callable[[str], None]] = None) -> LightRig:
    """文本 → 光照节点。

    离线确定性解析永远先算（保证一定有结果）；仅当 `use_cloud=True` 且配置
    完整时尝试云端增强，成功则采用云端结果并把 note 标注为云端解析。
    """
    log = log or (lambda m: None)
    rig = parse_offline(text)

    if use_cloud and config is not None:
        cloud = _cloud_rig(text, config)
        if cloud is not None:
            log("AI 自动打光：已采用云端解析")
            if base is not None:
                cloud.ambient_intensity = cloud.ambient_intensity
            return cloud
        log("AI 自动打光：云端不可用或解析失败，已回落本地离线解析")

    if base is not None and base.lights:
        # 保留用户原有基调的阴影/性能模式
        rig.shadow_mode = base.shadow_mode
        rig.lighting_mode = base.lighting_mode
    return rig


def to_render_params(rig: LightRig,
                     base: Optional[RenderParams] = None) -> RenderParams:
    """把光照方案套用到渲染参数（不改动传入对象）。"""
    p = copy.deepcopy(base) if base is not None else RenderParams()
    p.lights = [copy.deepcopy(l) for l in rig.lights]
    p.ambient_intensity = rig.ambient_intensity
    p.ambient_kelvin = rig.ambient_kelvin
    p.shadow_mode = rig.shadow_mode
    p.lighting_mode = rig.lighting_mode
    return p


def lights_from_prompt(text: str,
                       config: Optional[AIBackendConfig] = None,
                       use_cloud: bool = False) -> List[Light]:
    """便捷入口：文本 → Light 列表（供工作流 auto_light 节点调用）。"""
    return rig_from_text(text, config=config, use_cloud=use_cloud).lights


def rig_to_nodes(rig: LightRig) -> List[dict]:
    """把光照方案转成「专业模式」工作流节点（每个光源一个 light 节点）。"""
    nodes: List[dict] = []
    for i, l in enumerate(rig.lights):
        nodes.append({
            "type": "light",
            "name": l.name or f"光源 {i + 1}",
            "params": l.to_dict(),
        })
    nodes.append({
        "type": "global",
        "name": "全局光照",
        "params": {"ambient_intensity": rig.ambient_intensity,
                   "ambient_kelvin": rig.ambient_kelvin,
                   "shadow_mode": rig.shadow_mode,
                   "lighting_mode": rig.lighting_mode},
    })
    return nodes


def apply_to_params(rig: LightRig, params: RenderParams) -> RenderParams:
    """原地套用（返回同一个对象，方便界面直接替换）。"""
    params.lights = [copy.deepcopy(l) for l in rig.lights]
    params.ambient_intensity = rig.ambient_intensity
    params.ambient_kelvin = rig.ambient_kelvin
    params.shadow_mode = rig.shadow_mode
    return params


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    def _ok(cond, label):
        print(("PASS " if cond else "FAIL ") + label)
        assert cond, label

    r1 = parse_offline("黄昏时分的舞台逆光，暖橘色主光从左后方打来，冷蓝补光勾勒人物轮廓")
    _ok(len(r1.lights) >= 2, "解析出多个光源")
    _ok(r1.lights[0].dx < 0, "左后方 → dx<0")
    _ok(r1.lights[0].dz < 0.4, "逆光 → dz 偏低")
    _ok(2500 <= r1.lights[0].kelvin <= 3500, "暖橘 → 约 3000K")
    _ok(any("轮廓" in l.name for l in r1.lights), "识别出轮廓光")
    _ok(r1.source == "offline", "默认离线来源")
    _ok("解析：" in r1.note, "生成中文解析说明")

    r2 = parse_offline("冷色月夜，月光从右上方洒下")
    _ok(r2.lights[0].dx > 0 and r2.lights[0].dy < 0, "右上方")
    _ok(r2.lights[0].kelvin > 7000, "冷色 → 高色温")

    r3 = parse_offline("标准棚拍，柔光箱主光")
    _ok(r3.lights[0].kelvin in (5600.0,) or r3.lights[0].kelvin > 4000, "棚拍中性色温")

    # 确定性：同输入同输出
    a, b = parse_offline("舞台聚光，强烈硬光"), parse_offline("舞台聚光，强烈硬光")
    _ok(a.to_dict() == b.to_dict(), "解析确定性（同输入同输出）")
    _ok(a.shadow_mode == "hard", "强烈/舞台 → 硬阴影")
    _ok(a.lights[0].intensity > 1.0, "强烈 → 提高强度")

    # 显式 K 值优先
    _ok(parse_offline("主光 4300K 从左").lights[0].kelvin == 4300.0, "显式 K 值优先")

    # 转换与节点
    base = get_preset("标准棚拍")
    p = to_render_params(r1, base)
    _ok(len(p.lights) == len(r1.lights), "to_render_params 套用光源")
    _ok(base.lights[0].name != "AI 主光", "base 未被就地修改")
    nodes = rig_to_nodes(r1)
    _ok(sum(1 for n in nodes if n["type"] == "light") == len(r1.lights), "每光源一个节点")
    _ok(any(n["type"] == "global" for n in nodes), "含全局光照节点")

    # JSON 往返
    _ok(LightRig.from_dict(r1.to_dict()).to_dict() == r1.to_dict(), "LightRig JSON 往返")

    # 云端未配置时必须安全回落
    cfg = AIBackendConfig(cloud_enabled=False)
    r4 = rig_from_text("舞台聚光逆光", config=cfg, use_cloud=True)
    _ok(r4.source == "offline", "云端未配置 → 回落离线")

    print("PASS ALL autolight self-check")
