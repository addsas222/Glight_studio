# -*- coding: utf-8 -*-
"""CUE 预设：命名灯光场景（含淡变时间）。

与预设（presets.py）不同，CUE 由用户保存，落在用户数据目录的
``cues.json`` 中，可以理解为「灯光台的场景记忆」：每个 CUE 记录整套
RenderParams 以及一个淡变时长 fade（秒）。演出/拍摄时按 CUE 切换，
``interpolate`` 负责在 fade 时间内把当前灯光平滑过渡到目标 CUE。

设计约束：
- 任何磁盘异常（文件缺失、JSON 损坏、结构不对）都只表现为「没有 CUE」，
  绝不向调用方抛异常——灯光台不能因为一个坏文件而黑屏。
- 写入采用「临时文件 + os.replace」的原子替换，中途崩溃不会写坏原文件。
- ``interpolate`` 是纯函数：不修改传入的 current，返回全新的 RenderParams。
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field

from .paths import subdir
from .types import Light, RenderParams

# 淡变时长上限（秒），0 表示硬切
FADE_MAX = 30.0
# 未命名 CUE 的占位名
DEFAULT_CUE_NAME = "未命名 CUE"

# 光源参数的引擎取值范围（与 core/types.py、core/harvest.py 保持一致）
_INTENSITY_RANGE = (0.0, 4.0)
_KELVIN_RANGE = (1500.0, 12000.0)
_RADIUS_RANGE = (0.05, 1.5)
_AMBIENT_INTENSITY_RANGE = (0.0, 2.0)
_AMBIENT_KELVIN_RANGE = (1500.0, 12000.0)
_EXPOSURE_RANGE = (0.2, 2.5)


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    if math.isnan(v):
        return lo
    return lo if v < lo else (hi if v > hi else v)


def _lerp(a: float, b: float, t: float) -> float:
    return float(a) + (float(b) - float(a)) * float(t)


def _unit_dir(x: float, y: float, z: float) -> tuple:
    """归一化方向向量，并保证 dz > 0（指向观察者一侧）。"""
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-9:
        return 0.0, 0.0, 1.0
    x, y, z = x / n, y / n, z / n
    if z < 0.02:
        z = 0.02
        m = math.sqrt(x * x + y * y)
        if m < 1e-9:
            return 0.0, 0.0, 1.0
        s = math.sqrt(1.0 - z * z) / m
        x, y = x * s, y * s
    return x, y, z


def _params_to_dict(params) -> dict:
    """RenderParams | dict -> 纯 dict（可 JSON 序列化）。"""
    if isinstance(params, RenderParams):
        return params.to_dict()
    if isinstance(params, dict):
        return copy.deepcopy(params)
    return RenderParams().to_dict()


def _params_from_dict(d: dict) -> RenderParams:
    """dict -> RenderParams。

    RenderParams 若自带 from_dict() 则优先复用；否则按字段名过滤构造
    （与 Light.from_dict 同策略），保证旧存档里多出的字段不会导致失败。
    """
    d = dict(d or {})
    lights = [Light.from_dict(l) for l in d.get("lights", []) if isinstance(l, dict)]
    loader = getattr(RenderParams, "from_dict", None)
    if callable(loader):
        try:
            p = loader(dict(d))
        except Exception:
            p = None
        if isinstance(p, RenderParams):
            return p
    known = set(RenderParams.__dataclass_fields__)
    p = RenderParams(**{k: v for k, v in d.items() if k in known and k != "lights"})
    p.lights = lights
    return p


@dataclass
class Cue:
    """一个命名灯光场景。params 为 RenderParams.to_dict() 的结果。"""

    id: str = ""
    name: str = DEFAULT_CUE_NAME
    params: dict = field(default_factory=dict)
    fade: float = 1.0
    created: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", DEFAULT_CUE_NAME)),
            params=dict(d.get("params") or {}),
            fade=_clamp(d.get("fade", 1.0), 0.0, FADE_MAX),
            created=float(d.get("created", 0.0) or 0.0),
        )


class CueStore:
    """CUE 的持久化仓库（cues.json）。路径可注入，便于测试。"""

    def __init__(self, path: str = ""):
        self.path = path or subdir("cues.json")

    # ---------- 磁盘读写 ----------

    def _read_all(self) -> list:
        """读取全部 CUE；文件缺失或损坏时返回空列表，绝不抛异常。"""
        try:
            if not os.path.exists(self.path):
                return []
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return []
        if isinstance(raw, dict):                     # 兼容 {id: {...}} 结构
            raw = list(raw.values())
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, dict) and str(item.get("id", "")):
                out.append(Cue.from_dict(item).to_dict())
        return out

    def _write_all(self, items: list) -> None:
        """原子写入：先写临时文件再 os.replace，崩溃不会损坏原文件。"""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(items, ensure_ascii=False, indent=2, default=_json_default)
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".cues-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = ""
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---------- 公开 API ----------

    def list(self) -> list:
        """全部 CUE（保存顺序），每项为 dict。"""
        return self._read_all()

    def get(self, cue_id: str) -> dict | None:
        """按 id 取一个 CUE，不存在返回 None。"""
        for item in self._read_all():
            if item.get("id") == cue_id:
                return item
        return None

    def save(self, name: str, params, fade: float = 1.0,
             cue_id: str = "") -> Cue:
        """新增或更新一个 CUE。

        cue_id 命中已有条目则原地更新（保留 created），否则用新的 uuid4 hex 新建。
        名称去掉首尾空白，留空则记为「未命名 CUE」；fade 夹到 0~30 秒。
        """
        clean_name = str(name or "").strip() or DEFAULT_CUE_NAME
        clean_fade = _clamp(fade, 0.0, FADE_MAX)
        payload = _params_to_dict(params)

        items = self._read_all()
        for i, item in enumerate(items):
            if cue_id and item.get("id") == cue_id:
                cue = Cue(id=item["id"], name=clean_name, params=payload,
                          fade=clean_fade, created=float(item.get("created") or time.time()))
                items[i] = cue.to_dict()
                self._write_all(items)
                return cue

        cue = Cue(id=uuid.uuid4().hex, name=clean_name, params=payload,
                  fade=clean_fade, created=time.time())
        items.append(cue.to_dict())
        self._write_all(items)
        return cue

    def remove(self, cue_id: str) -> bool:
        """删除一个 CUE，返回是否真的删掉了。"""
        items = self._read_all()
        kept = [it for it in items if it.get("id") != cue_id]
        if len(kept) == len(items):
            return False
        self._write_all(kept)
        return True

    def interpolate(self, cue_id: str, current: RenderParams, t: float) -> RenderParams:
        """从 current 向目标 CUE 淡变，t 为进度（0~1，越界自动夹紧）。

        纯函数：不修改 current，返回全新的 RenderParams。
        - t<=0：完全等于 current（不做任何换算）。
        - 0<t<1：强度/色温/半径/环境/曝光按 t 线性插值；方向向量插值后重新
          归一化为单位向量且 dz>0；点光源位置 px/py/pz 同样插值。阴影模式、
          光照模式等离散参数不插值，保持 current 的值。
        - t>=1：标量/方向取 CUE 的值，离散参数整体切换为 CUE 的值。
        - 光源数量不一致时按序号配对：CUE 多出的光源直接采用，current 多出的
          光源原样保留（不参与淡变）。
        - cue_id 不存在时原样返回 current 的深拷贝。
        """
        t = _clamp(t, 0.0, 1.0)
        result = copy.deepcopy(current)
        if t <= 0.0:
            return result

        item = self.get(cue_id)
        if item is None:
            return result

        target = _params_from_dict(item.get("params") or {})
        settled = t >= 1.0
        cur_lights = list(current.lights)
        dst_lights = list(target.lights)

        blended = []
        for i in range(max(len(cur_lights), len(dst_lights))):
            if i >= len(dst_lights):          # CUE 里没有的光源：保持当前光源
                blended.append(copy.deepcopy(cur_lights[i]))
                continue
            if i >= len(cur_lights):          # 当前没有的光源：直接采用 CUE 的
                blended.append(copy.deepcopy(dst_lights[i]))
                continue
            blended.append(_blend_light(cur_lights[i], dst_lights[i], t, settled))
        result.lights = blended

        result.ambient_intensity = _clamp(
            _lerp(current.ambient_intensity, target.ambient_intensity, t),
            *_AMBIENT_INTENSITY_RANGE)
        result.ambient_kelvin = _clamp(
            _lerp(current.ambient_kelvin, target.ambient_kelvin, t),
            *_AMBIENT_KELVIN_RANGE)
        result.exposure = _clamp(
            _lerp(current.exposure, target.exposure, t), *_EXPOSURE_RANGE)

        if settled:                           # 离散参数只在终点整体切换
            for f in ("shadow_mode", "lighting_mode", "shadow_strength",
                      "specular_strength", "tone_preserve", "preview_size",
                      "lighting_size"):
                setattr(result, f, copy.deepcopy(getattr(target, f)))
            result.reference_offset = copy.deepcopy(target.reference_offset)
        return result


def _json_default(o):
    """兜底序列化：numpy 标量转 python 标量，其余转字符串。"""
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(o)


def _blend_light(cur: Light, cue: Light, t: float, settled: bool) -> Light:
    """单个光源的淡变结果（返回新对象，不修改 cur）。"""
    out = copy.deepcopy(cur)
    out.intensity = _clamp(_lerp(cur.intensity, cue.intensity, t), *_INTENSITY_RANGE)
    out.kelvin = _clamp(_lerp(cur.kelvin, cue.kelvin, t), *_KELVIN_RANGE)
    out.radius = _clamp(_lerp(cur.radius, cue.radius, t), *_RADIUS_RANGE)
    x = _lerp(cur.dx, cue.dx, t)
    y = _lerp(cur.dy, cue.dy, t)
    z = _lerp(cur.dz, cue.dz, t)
    out.dx, out.dy, out.dz = _unit_dir(x, y, z)
    out.px = _lerp(cur.px, cue.px, t)
    out.py = _lerp(cur.py, cue.py, t)
    out.pz = _lerp(cur.pz, cue.pz, t)
    if settled:
        out.kind = cue.kind
        out.visible = cue.visible
        out.name = cue.name
        if hasattr(out, "group"):     # group 字段由 types.py 提供，缺失时忽略
            out.group = getattr(cue, "group", "")
    return out


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import shutil
    import sys

    _fails = []

    def check(name: str, cond: bool) -> None:
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            _fails.append(name)

    tmp_dir = tempfile.mkdtemp(prefix="cues-selftest-")
    path = os.path.join(tmp_dir, "sub", "cues.json")
    try:
        # 1) 文件缺失 -> 空列表
        s = CueStore(path)
        check("缺失文件 list() == []", s.list() == [])
        check("缺失文件 get() is None", s.get("nope") is None)
        check("缺失文件 remove() is False", s.remove("nope") is False)

        # 2) save 新建
        base = RenderParams(lights=[Light(name="主光", intensity=1.0, kelvin=5500.0,
                                          radius=0.4, dx=-0.5, dy=-0.6, dz=0.7)],
                            ambient_intensity=0.5, exposure=1.0)
        c1 = s.save("  棚拍 A  ", base, fade=2.5)
        check("save 返回 uuid4 hex id", len(c1.id) == 32 and c1.id == c1.id.lower()
              and all(ch in "0123456789abcdef" for ch in c1.id))
        check("save 去除名称首尾空白", c1.name == "棚拍 A")
        check("save 保留 fade", abs(c1.fade - 2.5) < 1e-9)
        check("save 记录 created", c1.created > 0)
        check("save 写入磁盘", os.path.exists(path))
        check("save 无残留临时文件",
              all(not n.endswith(".tmp") for n in os.listdir(os.path.dirname(path))))

        c2 = s.save("   ", base)              # 空名
        check("空名回退为「未命名 CUE」", c2.name == DEFAULT_CUE_NAME)
        c3 = s.save("上限", base, fade=999.0)
        c4 = s.save("下限", base, fade=-5.0)
        check("fade 上限夹到 30", abs(c3.fade - 30.0) < 1e-9)
        check("fade 下限夹到 0", abs(c4.fade - 0.0) < 1e-9)

        # 3) 新实例读同一路径（持久化）
        s2 = CueStore(path)
        names = [c["name"] for c in s2.list()]
        check("新实例看到已保存的 CUE", names == ["棚拍 A", DEFAULT_CUE_NAME, "上限", "下限"])
        check("get() 命中", (s2.get(c1.id) or {}).get("name") == "棚拍 A")
        check("get() 未知 id 返回 None", s2.get("deadbeef") is None)
        check("存盘 params 保留灯光", len((s2.get(c1.id) or {}).get("params", {}).get("lights", [])) == 1)

        # 4) 同 id 更新
        before = len(s2.list())
        c1b = s2.save("棚拍 A 改", base, fade=1.0, cue_id=c1.id)
        after = s2.list()
        check("同 id 更新后条数不变", len(after) == before)
        check("同 id 更新保留 id", c1b.id == c1.id)
        check("同 id 更新改名生效", (s2.get(c1.id) or {}).get("name") == "棚拍 A 改")
        check("同 id 更新保留 created", abs(c1b.created - c1.created) < 1e-6)

        # 5) remove
        check("remove 首次 True", s2.remove(c1.id) is True)
        check("remove 再次 False", s2.remove(c1.id) is False)
        check("remove 后列表少一条", len(s2.list()) == before - 1)

        # 6) 损坏文件
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON ,,,")
        check("损坏 JSON -> [] 不抛异常", s2.list() == [])
        check("损坏 JSON 下 get() is None", s2.get(c3.id) is None)
        check("损坏 JSON 下仍可 save", bool(s2.save("恢复", base).id))
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a": 1, "b": [1,2]}')      # 合法 JSON 但结构不对
        check("结构不对 -> [] 不抛异常", s2.list() == [])
        try:
            os.remove(path)
        except OSError:
            pass
        check("删除文件后仍可 save", bool(s2.save("再来", base).id))

        # 7) interpolate
        store = CueStore(os.path.join(tmp_dir, "cues2.json"))
        cur = RenderParams(
            lights=[Light(name="主光", dx=-0.2, dy=0.1, dz=0.9, intensity=0.5,
                          kelvin=4000.0, radius=0.3),
                    Light(name="辅光", dx=0.6, dy=0.2, dz=0.4, intensity=0.2,
                          kelvin=6000.0, radius=0.5)],
            ambient_intensity=0.2, ambient_kelvin=5000.0, exposure=0.9,
            shadow_mode="soft", specular_strength=0.2)
        cue = store.save("霓虹", RenderParams(
            lights=[Light(name="霓虹粉", kind="point", px=0.1, py=0.3, pz=0.6,
                          dx=0.9, dy=-0.1, dz=0.3, intensity=2.0,
                          kelvin=2200.0, radius=1.0),
                    Light(name="霓虹青", dx=-0.7, dy=0.5, dz=0.2, intensity=1.6,
                          kelvin=11000.0, radius=0.8)],
            ambient_intensity=0.8, ambient_kelvin=9000.0, exposure=1.4,
            shadow_mode="hard", specular_strength=0.6), fade=3.0)
        snapshot = json.dumps(cur.to_dict(), sort_keys=True, ensure_ascii=False)

        r0 = store.interpolate(cue.id, cur, 0.0)
        check("t=0 等于 current", json.dumps(r0.to_dict(), sort_keys=True,
                                             ensure_ascii=False) == snapshot)
        r_neg = store.interpolate(cue.id, cur, -5.0)
        check("t<0 夹到 0", json.dumps(r_neg.to_dict(), sort_keys=True,
                                       ensure_ascii=False) == snapshot)
        r_big = store.interpolate(cue.id, cur, 9.0)
        check("t>1 夹到 1", r_big.ambient_intensity == 0.8
              and abs(r_big.exposure - 1.4) < 1e-9
              and r_big.shadow_mode == "hard"
              and r_big.lights[0].name == "霓虹粉"
              and r_big.lights[0].kind == "point")
        check("t=1 灯光参数到达 CUE",
              abs(r_big.lights[0].intensity - 2.0) < 1e-9
              and abs(r_big.lights[0].kelvin - 2200.0) < 1e-9
              and abs(r_big.lights[0].radius - 1.0) < 1e-9
              and abs(r_big.lights[1].kelvin - 11000.0) < 1e-9)
        d0 = r_big.lights[0].direction_vector()
        check("t=1 方向等于 CUE 的单位方向",
              all(abs(a - b) < 1e-6 for a, b in
                  zip(d0, Light.from_dict(cue.params["lights"][0]).direction_vector())))
        check("t=1 方向为单位向量且 dz>0",
              abs(sum(v * v for v in d0) - 1.0) < 1e-9 and d0[2] > 0)

        half = store.interpolate(cue.id, cur, 0.5)
        check("t=0.5 各量位于两端之间",
              0.5 < half.lights[0].intensity < 2.0
              and 2200.0 < half.lights[0].kelvin < 4000.0
              and 0.3 < half.lights[0].radius < 1.0
              and 0.2 < half.ambient_intensity < 0.8
              and 0.9 < half.exposure < 1.4
              and abs(half.lights[0].intensity - 1.25) < 1e-9)
        check("t=0.5 未切换到 CUE 的离散参数", half.shadow_mode == "soft")
        check("t=0.5 点光源位置插值",
              abs(half.lights[0].px - (0.5 + 0.1) / 2) < 1e-9)   # Light 默认 px=0.5

        check("interpolate 不修改 current",
              json.dumps(cur.to_dict(), sort_keys=True, ensure_ascii=False) == snapshot)
        check("interpolate 返回新对象", half is not cur and half.lights[0] is not cur.lights[0])

        # 光源数量不一致
        one = RenderParams(lights=[Light(name="仅一只", intensity=0.3, kelvin=5000.0)])
        try:
            r_less = store.interpolate(cue.id, one, 0.5)
            r_more = store.interpolate(cue.id, RenderParams(lights=[
                Light(), Light(), Light(), Light()]), 0.5)
            ok = (len(r_less.lights) == 2 and len(r_more.lights) == 4)
        except Exception as exc:                      # pragma: no cover - 自检用
            ok = False
            print("    异常：%r" % (exc,))
        check("光源数量不一致不抛异常且按序号配对", ok)
        check("多出的 current 光源原样保留",
              r_more.lights[3].name == Light().name
              and abs(r_more.lights[3].kelvin - 5500.0) < 1e-9)
        check("CUE 多出的光源直接采用",
              r_less.lights[1].name == "霓虹青" and abs(r_less.lights[1].intensity - 1.6) < 1e-9)

        # 空 CUE / 未知 id
        empty = RenderParams(lights=[])
        empty_cue = store.save("空场景", empty, fade=0.0)
        r_empty = store.interpolate(empty_cue.id, cur, 0.5)
        check("CUE 无光源时保留 current 光源", len(r_empty.lights) == 2)
        grouped = store.save("带分组", RenderParams(lights=[
            Light(name="分组灯", group="顶部组", intensity=3.0, kelvin=3200.0)]))
        check("存盘保留光源分组",
              (store.get(grouped.id) or {}).get("params", {}).get("lights", [{}])[0].get("group") == "顶部组")
        r_group_half = store.interpolate(grouped.id, cur, 0.5)
        r_group_full = store.interpolate(grouped.id, cur, 1.0)
        check("淡变中仍保留 current 的分组",
              getattr(r_group_half.lights[0], "group", None) == getattr(cur.lights[0], "group", None))
        check("淡变终点采用 CUE 的分组",
              getattr(r_group_full.lights[0], "group", None) == "顶部组")
        unknown = store.interpolate("0000000000000000", cur, 0.5)
        check("未知 cue_id 返回 current 副本",
              json.dumps(unknown.to_dict(), sort_keys=True, ensure_ascii=False) == snapshot)
        check("fade=0 存盘", store.get(empty_cue.id)["fade"] == 0.0)

        # 非法 fade 类型不炸
        check("fade 非法值不抛异常", store.save("怪值", base, fade="abc").fade == 0.0)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("----")
    if _fails:
        print("FAILED %d 项：%s" % (len(_fails), ", ".join(_fails)))
        sys.exit(1)
    print("全部通过")
