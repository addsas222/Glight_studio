# -*- coding: utf-8 -*-
"""凌日光影棚 · 插件系统（纯数据、零代码执行）。

安全属性（重要）
----------------
插件是**纯 JSON 数据包**：本模块只做三件事 —— 解析 JSON、按已知字段白名单
校验、把合法贡献汇总成普通 dict。它**绝不** import / exec / eval / pickle
任何插件内容，也不会因为 ``kind`` 去加载某个模块。``backend`` 类型目前只是
预留的声明式标签（用于将来标注外部算力后端），同样不会被加载或调用。

因此：一个恶意插件最多只能污染它自己声明的数据，无法在宿主进程中执行代码。
插件目录里的文件可以被任意应用（含病毒扫描、签名校验）当作只读清单核对。

目录布局
--------
- 插件目录默认为 ``core.paths.subdir("plugins")``（即 ``~/.horizon_light_studio/plugins``），
  也可通过 ``PluginRegistry(plugins_dir)`` 指向任意目录（测试/便携模式）。
- 一个插件 = 目录下的一个 ``*.json`` 清单文件，文件名任意；解析出的 ``id`` 是唯一键。
- 启用状态持久化在 ``<plugins_dir>/state.json``，形如 ``{"<id>": true}``。
  状态**不写回清单**，因此清单可以只读分发、可校验签名；``state.json`` 自身
  不是插件清单，扫描时会被跳过。

贡献汇总
--------
``contributions()`` 只收集**已启用**插件的数据，并逐条校验：

- ``light-rig``：``data.lights`` 必须是对象数组，每个对象只保留已知的
  :class:`core.types.Light` 字段，数值按引擎范围收敛（强度 0~4、色温
  1500~12000、半径 0.05~1.5），未知字段直接丢弃。
- ``preset``：``data`` 是 :class:`core.types.RenderParams` 的字段集合
  （可含 ``lights``），未知字段忽略、非法取值使该条目整体跳过。
- ``theme``：``data`` 中（或 ``data.tokens`` 中）的 ``#RRGGBB`` 颜色表，
  非法颜色丢弃。

任何一条贡献非法都只跳过该条目并记一条警告日志，**不抛异常**，避免一个坏插件
拖垮整个应用。仓库里的 ``plugins/examples/`` 提供三份可直接安装的示例清单。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import Light, RenderParams

logger = logging.getLogger(__name__)

__all__ = [
    "PLUGIN_KINDS",
    "STATE_FILE",
    "PluginManifest",
    "PluginRegistry",
]

#: 合法的插件类型（backend 为预留的声明式标签，不做任何加载）
PLUGIN_KINDS = ("preset", "theme", "light-rig", "backend")

#: 启用状态文件名（位于插件目录内，不是插件清单）
STATE_FILE = "state.json"

# 引擎使用的取值范围（与 core.render / core.workflow 保持一致）
INTENSITY_RANGE = (0.0, 4.0)
KELVIN_RANGE = (1500.0, 12000.0)
RADIUS_RANGE = (0.05, 1.5)
AMBIENT_INTENSITY_RANGE = (0.0, 2.0)
UNIT_RANGE = (0.0, 1.0)
EXPOSURE_RANGE = (0.2, 2.5)
SIZE_RANGE = (64, 4096)

LIGHT_FIELDS = frozenset(Light.__dataclass_fields__)
RENDER_FIELDS = frozenset(RenderParams.__dataclass_fields__)

_MODES = {"shadow_mode": ("hard", "soft"), "lighting_mode": ("linear", "hq")}
_RANGES = {
    "ambient_intensity": AMBIENT_INTENSITY_RANGE,
    "ambient_kelvin": KELVIN_RANGE,
    "shadow_strength": UNIT_RANGE,
    "specular_strength": UNIT_RANGE,
    "tone_preserve": UNIT_RANGE,
    "exposure": EXPOSURE_RANGE,
}
#: 需要是纯数字的光源几何字段（不做范围收敛，方向可正可负）
_LIGHT_GEOMETRY = ("dx", "dy", "dz", "px", "py", "pz")
#: 需要收敛到引擎范围的光源数值字段
_LIGHT_RANGES = {
    "intensity": INTENSITY_RANGE,
    "kelvin": KELVIN_RANGE,
    "radius": RADIUS_RANGE,
}
_LIGHT_KINDS = ("directional", "point")
_INT_FIELDS = ("preview_size", "lighting_size")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_UNSAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._-]+")


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #
@dataclass
class PluginManifest:
    """一个插件清单（纯数据）。

    ``data`` 保存清单中的贡献载荷（如灯组的光源列表），``enabled`` 来自
    ``state.json`` 而不是清单本身。
    """

    id: str
    name: str
    version: str
    kind: str
    description: str = ""
    author: str = ""
    enabled: bool = True
    path: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "description": self.description,
            "author": self.author,
            "enabled": bool(self.enabled),
            "path": self.path,
            "data": self.data,
        }


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _as_float(value) -> float:
    """把 JSON 数值转成 float；bool 不算数值。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为数值")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip())
    raise ValueError("不是数值")


def _clean_light(raw) -> Optional[dict]:
    """校验单个光源字典，返回仅含已知 Light 字段的字典；非法返回 None。

    未知字段直接丢弃；已知字段类型错误（几何量非数字、kind 未知等）则整个
    光源作废 —— 与其带一个会在渲染阶段炸掉的坏值，不如少一盏灯。
    """
    if not isinstance(raw, dict):
        return None
    cleaned: dict = {}
    for key, value in raw.items():
        if key not in LIGHT_FIELDS:
            continue  # 未知字段直接丢弃
        try:
            if key in _LIGHT_GEOMETRY:
                cleaned[key] = _as_float(value)
            elif key in _LIGHT_RANGES:
                cleaned[key] = _clamp(_as_float(value), *_LIGHT_RANGES[key])
            elif key == "name":
                if not isinstance(value, str):
                    raise ValueError("name 必须是字符串")
                cleaned[key] = value
            elif key == "kind":
                if value not in _LIGHT_KINDS:
                    raise ValueError(f"kind={value!r} 未知")
                cleaned[key] = value
            elif key == "visible":
                if not isinstance(value, bool):
                    raise ValueError("visible 必须是布尔值")
                cleaned[key] = value
        except (TypeError, ValueError) as exc:
            logger.warning("插件光源字段 %s 非法，整个光源跳过：%s", key, exc)
            return None
    if not cleaned:
        return None
    return Light(**cleaned).to_dict()


def _clean_lights(raw) -> List[dict]:
    """校验光源数组；非数组或全部非法时返回空列表。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        light = _clean_light(item)
        if light is not None:
            out.append(light)
    return out


def _clean_preset(data: dict) -> Optional[dict]:
    """把插件载荷校验成 RenderParams 字典；非法返回 None。"""
    if not isinstance(data, dict):
        return None
    kwargs: dict = {}
    for key, value in data.items():
        if key not in RENDER_FIELDS or key == "lights":
            continue  # 未知字段忽略；lights 单独处理
        try:
            if key in _MODES:
                if value not in _MODES[key]:
                    raise ValueError(f"{key}={value!r} 不是合法取值")
                kwargs[key] = value
            elif key in _INT_FIELDS:
                kwargs[key] = int(_clamp(_as_float(value), *SIZE_RANGE))
            elif key in _RANGES:
                kwargs[key] = _clamp(_as_float(value), *_RANGES[key])
            elif key == "reference_offset":
                if value is None:
                    kwargs[key] = None
                elif isinstance(value, dict):
                    kwargs[key] = dict(value)
                else:
                    raise ValueError("reference_offset 必须是对象或 null")
        except (TypeError, ValueError) as exc:
            logger.warning("插件预设字段 %s 非法，整条跳过：%s", key, exc)
            return None
    lights = _clean_lights(data.get("lights")) if "lights" in data else []
    if "lights" in data and not lights:
        logger.warning("插件预设的 lights 全部非法，整条跳过")
        return None
    params = RenderParams(lights=[Light.from_dict(l) for l in lights], **kwargs)
    return params.to_dict()


def _clean_tokens(data: dict) -> Dict[str, str]:
    """从插件载荷提取合法颜色 token；非法颜色丢弃。"""
    if not isinstance(data, dict):
        return {}
    source = data.get("tokens")
    if not isinstance(source, dict):
        source = data
    out: Dict[str, str] = {}
    for name, value in source.items():
        if isinstance(name, str) and isinstance(value, str) and _COLOR_RE.match(value.strip()):
            out[name] = value.strip().upper()
    return out


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class PluginRegistry:
    """扫描 / 安装 / 启停插件，并汇总它们的贡献（全部是纯数据操作）。"""

    def __init__(self, plugins_dir: str = ""):
        if plugins_dir:
            self.dir = os.path.abspath(os.path.expanduser(plugins_dir))
        else:
            # 延迟到实例化时才解析数据目录（避免 import 期触发目录迁移）
            from .paths import subdir

            self.dir = subdir("plugins")

    # -- 基础 ------------------------------------------------------------- #
    @property
    def state_path(self) -> str:
        return os.path.join(self.dir, STATE_FILE)

    def _load_state(self) -> Dict[str, bool]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as exc:  # 损坏的状态文件按"全部默认启用"处理
            logger.warning("插件状态文件无法读取，忽略：%s", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): bool(v) for k, v in raw.items()}

    def _save_state(self, state: Dict[str, bool]) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)

    # -- 扫描 ------------------------------------------------------------- #
    def scan(self) -> List[PluginManifest]:
        """读取插件目录下的 ``*.json`` 清单；目录不存在会创建，损坏文件跳过。"""
        try:
            os.makedirs(self.dir, exist_ok=True)
            filenames = sorted(os.listdir(self.dir))
        except OSError as exc:
            logger.warning("插件目录不可用，按无插件处理：%s (%s)", self.dir, exc)
            return []
        state = self._load_state()
        found: List[PluginManifest] = []
        seen = set()
        for filename in filenames:
            if not filename.lower().endswith(".json") or filename == STATE_FILE:
                continue
            path = os.path.join(self.dir, filename)
            if not os.path.isfile(path):
                continue
            manifest = _parse_manifest(path)
            if manifest is None:
                logger.warning("跳过无法解析的插件清单：%s", path)
                continue
            if manifest.id in seen:
                logger.warning("插件 id 重复，忽略：%s (%s)", manifest.id, path)
                continue
            seen.add(manifest.id)
            manifest.enabled = bool(state.get(manifest.id, True))
            found.append(manifest)
        return found

    def list(self) -> List[dict]:
        return [m.to_dict() for m in self.scan()]

    # -- 安装 / 启停 / 卸载 ------------------------------------------------ #
    def install(self, manifest_json_path: str) -> PluginManifest:
        """安装一个 ``.json`` 清单：校验 → 复制进插件目录 → 返回清单对象。

        校验失败（文件不存在、JSON 损坏、缺少 id/name/version/kind、kind 未知）
        抛出 :class:`ValueError`。
        """
        src = os.path.abspath(os.path.expanduser(manifest_json_path))
        manifest = _parse_manifest(src)
        if manifest is None:
            raise ValueError(
                f"无效的插件清单：{manifest_json_path}"
                f"（需为合法 JSON 且包含 id/name/version/kind，kind 属于 {PLUGIN_KINDS}）"
            )
        os.makedirs(self.dir, exist_ok=True)
        dest = None
        for existing in self.scan():
            if existing.id == manifest.id and existing.path:
                dest = existing.path
                break
        if dest is None:
            dest = os.path.join(self.dir, _safe_filename(manifest.id) + ".json")
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copyfile(src, dest)
        manifest.path = dest
        manifest.enabled = bool(self._load_state().get(manifest.id, True))
        return manifest

    def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        """启用/停用插件并持久化；插件不存在返回 False。"""
        if not any(m.id == plugin_id for m in self.scan()):
            return False
        state = self._load_state()
        state[plugin_id] = bool(enabled)
        self._save_state(state)
        return True

    def remove(self, plugin_id: str) -> bool:
        """删除插件清单文件及其状态；未找到返回 False。"""
        removed = False
        for manifest in self.scan():
            if manifest.id == plugin_id and manifest.path:
                try:
                    os.remove(manifest.path)
                    removed = True
                except OSError as exc:
                    logger.warning("删除插件文件失败：%s", exc)
        if removed:
            state = self._load_state()
            state.pop(plugin_id, None)
            self._save_state(state)
        return removed

    # -- 贡献 ------------------------------------------------------------- #
    def contributions(self) -> dict:
        """汇总**已启用**插件的合法贡献；非法条目跳过并记日志。"""
        out = {"presets": {}, "themes": {}, "light_rigs": {}}
        for manifest in self.scan():
            if not manifest.enabled:
                continue
            key = manifest.name or manifest.id
            try:
                if manifest.kind == "light-rig":
                    lights = _clean_lights(manifest.data.get("lights"))
                    if lights:
                        out["light_rigs"][key] = lights
                    else:
                        logger.warning("灯组插件 %s 没有合法光源，跳过", manifest.id)
                elif manifest.kind == "preset":
                    params = _clean_preset(manifest.data)
                    if params:
                        out["presets"][key] = params
                    else:
                        logger.warning("预设插件 %s 载荷非法，跳过", manifest.id)
                elif manifest.kind == "theme":
                    tokens_found = _clean_tokens(manifest.data)
                    if tokens_found:
                        out["themes"][manifest.id] = tokens_found
                    else:
                        logger.warning("主题插件 %s 没有合法颜色，跳过", manifest.id)
                # backend 类型不贡献运行时数据
            except Exception as exc:  # 任何插件都不应拖垮宿主
                logger.warning("处理插件 %s 失败，已跳过：%s", manifest.id, exc)
        return out

    def export_state(self) -> dict:
        """导出插件列表、已启用 id 与全部类型（供设置页/迁移使用）。"""
        plugins = self.list()
        return {
            "plugins": plugins,
            "enabled": [p["id"] for p in plugins if p["enabled"]],
            "kinds": list(PLUGIN_KINDS),
        }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _parse_manifest(path: str) -> Optional[PluginManifest]:
    """解析并校验清单文件；失败返回 None（绝不执行文件内容）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("插件清单 JSON 无法解析：%s (%s)", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("插件清单必须是 JSON 对象：%s", path)
        return None
    required = ("id", "name", "version", "kind")
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            logger.warning("插件清单缺少合法字段 %s：%s", key, path)
            return None
    if raw["kind"] not in PLUGIN_KINDS:
        logger.warning("插件清单 kind=%r 未知：%s", raw["kind"], path)
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    return PluginManifest(
        id=raw["id"].strip(),
        name=raw["name"].strip(),
        version=raw["version"].strip(),
        kind=raw["kind"],
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        path=os.path.abspath(path),
        data=data,
    )


def _safe_filename(plugin_id: str) -> str:
    name = _UNSAFE_FILENAME_RE.sub("_", plugin_id).strip("._")
    return name or "plugin"


# --------------------------------------------------------------------------- #
# 自检：python -m core.plugins
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile

    passed = 0

    def check(label: str, condition: bool) -> None:
        global passed
        if not condition:
            raise AssertionError("FAIL " + label)
        passed += 1
        print(f"PASS  {label}")

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    examples_dir = os.path.join(repo_root, "plugins", "examples")

    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "plugins")
        inbox = os.path.join(tmp, "inbox")
        os.makedirs(inbox)

        # 1) 空目录
        reg = PluginRegistry(work)
        check("空目录 scan() == []", reg.scan() == [])
        check("空目录 list() == []", reg.list() == [])
        check("空目录 contributions() 为空", reg.contributions() ==
              {"presets": {}, "themes": {}, "light_rigs": {}})

        # 1b) 目录不可用（同名文件占位）时降级为空，不抛异常
        blocked = os.path.join(tmp, "not-a-dir")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("x")
        blocked_reg = PluginRegistry(blocked)
        check("不可用目录 scan() 降级为 []", blocked_reg.scan() == [])
        check("不可用目录 contributions() 为空", blocked_reg.contributions()["light_rigs"] == {})

        # 2) 安装示例清单
        names = sorted(os.listdir(examples_dir))
        check("仓库示例清单共 3 个", len([n for n in names if n.endswith('.json')]) == 3)
        installed = {}
        for filename in names:
            if not filename.endswith(".json"):
                continue
            manifest = reg.install(os.path.join(examples_dir, filename))
            installed[manifest.kind] = manifest
        check("安装返回 3 种类型", set(installed) == {"light-rig", "preset", "theme"})
        check("文件已复制进插件目录", len([f for f in os.listdir(work) if f.endswith(".json")]) == 3)
        for manifest in installed.values():
            check(f"安装路径在插件目录内 ({manifest.id})",
                  os.path.dirname(os.path.abspath(manifest.path)) == os.path.abspath(work))
            check(f"安装后默认启用 ({manifest.id})", manifest.enabled is True)
        check("安装后 scan() 反映 3 个插件", len(reg.scan()) == 3)
        check("list() 含 enabled 字段",
              all("enabled" in item for item in reg.list()))

        # 3) 贡献汇总
        contrib = reg.contributions()
        rig = installed["light-rig"]
        check("contributions 含灯组", rig.name in contrib["light_rigs"])
        rig_lights = contrib["light_rigs"][rig.name]
        check("灯组光源全部为已知 Light 字段",
              all(set(l) <= LIGHT_FIELDS for l in rig_lights))
        check("灯组光源数量正确", len(rig_lights) == 2)
        check("预设贡献存在", installed["preset"].name in contrib["presets"])
        check("主题贡献存在", installed["theme"].id in contrib["themes"])
        check("主题贡献均为合法颜色",
              all(_COLOR_RE.match(v) for v in contrib["themes"][installed["theme"].id].values()))

        # 4) 停用状态跨实例持久化，且贡献随之消失
        check("set_enabled 返回 True", reg.set_enabled(rig.id, False) is True)
        check("state.json 已写入", os.path.isfile(reg.state_path))
        with open(reg.state_path, encoding="utf-8") as fh:
            check("state.json 内容为 id->bool", json.load(fh) == {rig.id: False})
        reg2 = PluginRegistry(work)
        check("新实例读到停用状态",
              [m.enabled for m in reg2.scan() if m.id == rig.id] == [False])
        contrib2 = reg2.contributions()
        check("停用插件不再贡献灯组", rig.name not in contrib2["light_rigs"])
        check("其余插件贡献不受影响", installed["preset"].name in contrib2["presets"])
        check("set_enabled 未知 id 返回 False", reg2.set_enabled("com.nope", True) is False)
        check("重新启用生效", reg2.set_enabled(rig.id, True) is True)
        check("重新启用后贡献恢复",
              rig.name in PluginRegistry(work).contributions()["light_rigs"])

        # 5) 损坏 JSON 被跳过
        with open(os.path.join(work, "broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{ 这不是合法 JSON")
        check("损坏 JSON 不影响扫描", len(reg.scan()) == 3)

        # 6) 未知光源字段被丢弃
        src_unknown = os.path.join(inbox, "unknown-field.json")
        with open(src_unknown, "w", encoding="utf-8") as fh:
            json.dump({
                "id": "com.test.unknown-field", "name": "未知字段灯组", "version": "1.0.0",
                "kind": "light-rig", "description": "测试", "author": "self-check",
                "data": {"lights": [{"name": "冷光", "dx": 0.4, "dy": -0.5, "dz": 0.6,
                                     "intensity": 9.0, "kelvin": 100, "bogus": 123,
                                     "another_unknown": "x"}]},
            }, fh, ensure_ascii=False)
        m_unknown = reg.install(src_unknown)
        check("含未知字段的清单仍可加载", m_unknown.kind == "light-rig")
        light = reg.contributions()["light_rigs"]["未知字段灯组"][0]
        check("未知光源字段被丢弃", "bogus" not in light and "another_unknown" not in light)
        check("未知字段清单的其它光源字段保留", light["dx"] == 0.4 and light["name"] == "冷光")
        check("越界强度被收敛到 4.0", light["intensity"] == 4.0)
        check("越界色温被收敛到 1500", light["kelvin"] == 1500.0)

        # 6b) 已知字段类型错误的光源被整盏丢弃，同灯组其它光源保留
        src_typed = os.path.join(inbox, "bad-type.json")
        with open(src_typed, "w", encoding="utf-8") as fh:
            json.dump({
                "id": "com.test.bad-type", "name": "坏类型灯组", "version": "1.0.0",
                "kind": "light-rig",
                "data": {"lights": [{"name": "好灯", "dx": 0.2},
                                    {"name": "坏灯", "dx": "不是数字"}]},
            }, fh, ensure_ascii=False)
        reg.install(src_typed)
        typed_lights = reg.contributions()["light_rigs"]["坏类型灯组"]
        check("类型错误的光源被丢弃", [l["name"] for l in typed_lights] == ["好灯"])

        # 7) 非法载荷被跳过而非抛异常
        src_bad_preset = os.path.join(inbox, "bad-preset.json")
        with open(src_bad_preset, "w", encoding="utf-8") as fh:
            json.dump({
                "id": "com.test.bad-preset", "name": "坏预设", "version": "0.1.0",
                "kind": "preset",
                "data": {"ambient_intensity": 0.5, "shadow_mode": "超硬"},
            }, fh, ensure_ascii=False)
        reg.install(src_bad_preset)
        src_bad_theme = os.path.join(inbox, "bad-theme.json")
        with open(src_bad_theme, "w", encoding="utf-8") as fh:
            json.dump({
                "id": "com.test.bad-theme", "name": "坏主题", "version": "0.1.0",
                "kind": "theme", "data": {"tokens": {"accent": "not-a-color", "border": "#12345"}},
            }, fh, ensure_ascii=False)
        reg.install(src_bad_theme)
        contrib3 = reg.contributions()
        check("非法预设被跳过", "坏预设" not in contrib3["presets"])
        check("非法主题被跳过", "com.test.bad-theme" not in contrib3["themes"])

        # 8) 安装错误路径
        for label, path in (("不存在的文件", os.path.join(inbox, "nope.json")),
                            ("损坏的 JSON", os.path.join(work, "broken.json"))):
            try:
                reg.install(path)
            except ValueError:
                check(f"install 拒绝{label}", True)
            else:
                check(f"install 拒绝{label}", False)
        missing_field = os.path.join(inbox, "missing-version.json")
        with open(missing_field, "w", encoding="utf-8") as fh:
            json.dump({"id": "com.test.x", "name": "缺字段", "kind": "preset"}, fh,
                      ensure_ascii=False)
        src_bad_kind = os.path.join(inbox, "bad-kind.json")
        with open(src_bad_kind, "w", encoding="utf-8") as fh:
            json.dump({"id": "com.test.y", "name": "坏类型", "version": "1.0.0",
                       "kind": "executable"}, fh, ensure_ascii=False)
        for label, path in (("缺少 version", missing_field), ("未知 kind", src_bad_kind)):
            try:
                reg.install(path)
            except ValueError:
                check(f"install 拒绝{label}", True)
            else:
                check(f"install 拒绝{label}", False)

        # 9) 卸载与导出
        check("remove 返回 True", reg.remove("com.test.unknown-field") is True)
        check("remove 后插件消失",
              "com.test.unknown-field" not in [m.id for m in reg.scan()])
        check("重复 remove 返回 False", reg.remove("com.test.unknown-field") is False)
        exported = reg.export_state()
        check("export_state 结构正确",
              set(exported) == {"plugins", "enabled", "kinds"} and
              exported["kinds"] == list(PLUGIN_KINDS))
        check("export_state.enabled 与 plugins 一致",
              exported["enabled"] == [p["id"] for p in exported["plugins"] if p["enabled"]])
        check("停用的预设/主题仍在列表中",
              {p["id"] for p in exported["plugins"]} >= {installed["preset"].id,
                                                         installed["theme"].id})

    print(f"\n全部通过：{passed} 项检查")
