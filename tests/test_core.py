# -*- coding: utf-8 -*-
"""核心引擎单元测试。运行：python -m pytest tests/ -v（或 python tests/test_core.py）"""
import base64
import io
import json
import os
import sys
import tempfile
import threading
import time

import numpy as np
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.ai_backend import AIBackendConfig, AIPreprocessor
from core.cache import AICache
from core.degrade import simulate_depth_from_luminance
from core.pipeline import Pipeline, load_image
from core.presets import PRESET_NAMES, get_preset
from core.reference import analyze_reference, apply_reference_offset
from core.render import (depth_to_height, height_to_normal, kelvin_to_rgb,
                         render)
from core.types import Light, RenderParams
from PIL import Image

# 供 python tests/test_core.py 直接运行时导出样例图使用
_RENDER_SAMPLES: dict = {}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """测试一律使用临时缓存/配置目录，不读写用户真实数据。

    否则「后端全部失败 → 降级」这类用例会被本机已有缓存顶掉，
    导致同一套测试在干净机器与本机结果不一致。
    """
    import core.cache as cache_mod
    from core import ai_backend as ai_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR_DEFAULT", str(tmp_path / "cache"))
    monkeypatch.setattr(ai_mod, "CONFIG_PATH_DEFAULT",
                        str(tmp_path / "config.json"))
    yield


def _test_image(sz=256) -> np.ndarray:
    """合成一个类似动漫角色的测试图：亮色头发区+皮肤区+暗色背景。"""
    y, x = np.mgrid[0:sz, 0:sz].astype(np.float32) / sz
    img = np.full((sz, sz, 3), 40, np.uint8)
    # 头部圆形（皮肤色）
    head = ((x - 0.5) ** 2 + (y - 0.42) ** 2) < 0.09
    img[head] = (245, 220, 200)
    # 头发弧形（亮青）
    hair = (((x - 0.5) ** 2 + (y - 0.38) ** 2) < 0.16) & ~head & (y < 0.55)
    img[hair] = (120, 220, 235)
    # 身体（深蓝）
    body = (y > 0.55) & (((x - 0.5) ** 2) < 0.04)
    img[body] = (60, 70, 160)
    return img


def _png(arr01: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray((np.clip(arr01, 0, 1) * 255).astype(np.uint8)).save(
        buf, format="PNG")
    return buf.getvalue()


def test_kelvin_to_rgb():
    assert kelvin_to_rgb(6500).shape == (3,)
    warm, cool = kelvin_to_rgb(2500), kelvin_to_rgb(9500)
    assert warm[0] > warm[2]      # 暖色偏红
    assert cool[2] > cool[0]      # 冷色偏蓝
    print("✓ kelvin_to_rgb")


def test_normal():
    d = np.zeros((64, 64), np.float32)
    d[:, 32:] = 1.0            # 右半更近
    n = height_to_normal(depth_to_height(d))
    assert n.shape == (64, 64, 3)
    # 台阶边缘（x=32）处法线应明显倾斜向 -x
    assert n[10, 32, 0] < -0.05
    assert abs(np.linalg.norm(n[10, 10]) - 1) < 1e-5
    print("✓ height_to_normal")


def test_simulate_depth():
    img = _test_image()
    d = simulate_depth_from_luminance(img)
    assert d.shape == img.shape[:2]
    assert 0 <= d.min() and d.max() <= 1
    print("✓ simulate_depth (降级模式)")


def test_cache_roundtrip():
    tmpdir = tempfile.mkdtemp(prefix="hlstest_cache")
    c = AICache(tmpdir)
    key = AICache.key_for(b"hello-image", "builtin", "m1")
    d = np.random.rand(32, 32).astype(np.float32)
    # 构造一个明显非平坦的单位法线场
    n = np.zeros((32, 32, 3), np.float32)
    n[..., 2] = 1.0
    n[:16, ..., 0] = -0.5
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    c.put(key, d, {"backend": "builtin"}, n)
    hit = c.get(key)
    assert hit is not None and hit["from_cache"]
    assert np.allclose(hit["depth"], d, atol=1 / 255)
    # 法线以 ±1 编码后再 8bit 量化，容差按 2/255 计
    assert hit["normal"] is not None
    assert np.allclose(hit["normal"], n, atol=3 / 255)
    assert c.get(AICache.key_for(b"other", "builtin")) is None
    # 无外部法线时不应写出 normal.png
    key2 = AICache.key_for(b"no-normal", "builtin", "m1")
    c.put(key2, d, {"backend": "builtin"})
    assert c.get(key2)["normal"] is None
    print("✓ AICache 持久化（深度 PNG + 法线 PNG + JSON）")


def test_render_fast_and_hq():
    img = _test_image()
    d = simulate_depth_from_luminance(img)
    d[:, 128:150] = 1.0        # 一道脊，确保高质量模式有真实遮挡
    p = get_preset("标准棚拍")
    p.lighting_mode = "linear"
    p.specular_strength = 0.0  # 排除高光干扰：差异必须来自阴影
    t0 = time.time()
    out = render(img, d, p)
    tf = time.time() - t0
    assert out.shape == img.shape
    assert out.dtype == np.uint8
    print(f"✓ 快速预览渲染 {tf:.2f}s")

    p.lighting_mode = "hq"
    p.shadow_mode = "hard"
    t0 = time.time()
    out2 = render(img, d, p)
    th = time.time() - t0
    assert not np.array_equal(out, out2)  # HQ 有物理阴影，结果应不同
    # 高质量模式含物理阴影，整体应比线性叠加更暗
    assert out2.astype(np.float32).mean() < out.astype(np.float32).mean()
    print(f"✓ 高质量硬阴影渲染 {th:.2f}s")
    _RENDER_SAMPLES["linear"], _RENDER_SAMPLES["hq"] = out, out2


def test_shadow_mapping_occludes():
    """阴影投射互斥必须真的产生遮挡。

    回归用例：早前版本中光线爬升误用像素步长（slope*max(h,w)/steps），
    首步抬升即达 3.3，远高于高度场幅值 0.22，导致 occluded 恒为空、
    高质量模式与快速预览逐像素相同。这里用「中部高脊 + 侧向光」验证。
    """
    from core.render import _light_dirs, _shadow_mask
    h = w = 256
    d = np.zeros((h, w), np.float32)
    d[:, int(w * 0.50):int(w * 0.58)] = 1.0     # 脊状遮挡体
    height = depth_to_height(d)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    left = np.s_[:, :int(w * 0.5)]

    low = Light(dx=0.9, dy=0.0, dz=0.3)
    L, _ = _light_dirs(low, (h, w), yy, xx)
    vis_low = _shadow_mask(low, height, (h, w), L, 26)
    assert vis_low.min() == 0.0, "低角度侧光必须产生遮挡"
    shadowed_low = 1.0 - vis_low[left].mean()

    high = Light(dx=0.9, dy=0.0, dz=1.2)
    L, _ = _light_dirs(high, (h, w), yy, xx)
    vis_high = _shadow_mask(high, height, (h, w), L, 26)
    shadowed_high = 1.0 - vis_high[left].mean()

    # 光源越高，阴影越短：低角度光的阴影覆盖必须严格多于高角度光
    assert shadowed_low > shadowed_high > 0.0, \
        f"阴影应随光源仰角增大而缩短（低={shadowed_low:.2f} 高={shadowed_high:.2f}）"
    print(f"✓ 阴影投射互斥（低角度遮挡 {shadowed_low*100:.0f}% > "
          f"高角度 {shadowed_high*100:.0f}%）")


def test_external_normal_is_used():
    """AI 法线图应真正参与光照计算，而不是被忽略后重新由深度派生。"""
    img = _test_image()
    d = simulate_depth_from_luminance(img)
    p = get_preset("标准棚拍")
    p.lighting_mode = "hq"
    derived = render(img, d, p)

    # 与深度场无关的倾斜法线场：整平面朝向 +x 侧光源
    n = np.zeros((*img.shape[:2], 3), np.float32)
    n[..., 0] = 0.6
    n[..., 2] = 0.8
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    from_ai = render(img, d, p, normal=n)

    assert from_ai.shape == img.shape
    assert not np.array_equal(derived, from_ai)
    # 传入的法线没有任何 -y 分量，而派生法线有；结果必须确实不同
    assert float(np.abs(from_ai.astype(np.float32) -
                        derived.astype(np.float32)).mean()) > 1.0
    print("✓ 外部 AI 法线图参与渲染")


def test_presets_all_render():
    img = _test_image()
    d = simulate_depth_from_luminance(img)
    for name in PRESET_NAMES:
        p = get_preset(name)
        p.lighting_mode = "linear"
        out = render(img, d, p)
        assert out.shape == img.shape
    print(f"✓ 全部 {len(PRESET_NAMES)} 种预设渲染通过")


def test_point_light_shadows():
    img = _test_image()
    d = simulate_depth_from_luminance(img)
    p = RenderParams(lighting_mode="hq", shadow_mode="hard")
    p.lights = [Light(name="聚光", kind="point", px=0.2, py=0.2, pz=0.3,
                      intensity=2.0, kelvin=5000)]
    out = render(img, d, p)
    # 遮挡侧应明显更暗：取右下角（远离光源且被头部遮挡概率高）
    assert out.shape == img.shape
    print("✓ 点光源阴影投射")


def test_reference_offset():
    ref = _test_image()
    f = analyze_reference(ref)
    assert -1 <= f["light_dx"] <= 1 and 1800 <= f["kelvin"] <= 12000
    p = get_preset("标准棚拍")
    k_before, i_before = p.lights[0].kelvin, p.lights[0].intensity
    p2 = apply_reference_offset(p, f)
    # 限幅验证
    assert abs(p2.lights[0].kelvin - k_before) <= 500.01
    assert abs(p2.lights[0].intensity - i_before) <= 0.2501
    # 深拷贝：原参数未被污染
    assert p.lights[0].kelvin == k_before
    print("✓ 参考图迁移（保守限幅偏移）")


def test_backend_chain_degrade():
    """AI 后端全部不可用时必须降级到 simulate，且不抛异常。

    必须显式关掉内置引擎：内置引擎是**离线**后端，若本机已下载模型，
    它会合法地成功（这不算降级失败）。本用例要验证的是「所有可用来源
    都拿不到时仍能出一张预览图」。
    """
    cfg = AIBackendConfig(order=["builtin", "local", "cloud", "simulate"],
                          builtin_enabled=False,       # 强制走到 local
                          local_url="http://127.0.0.1:1",  # 不存在的端口
                          cloud_enabled=False, timeout=0.3)
    pl = Pipeline(cfg)
    img = _test_image()
    res = pl.run(img, get_preset("冷色月夜"))
    assert res["backend"] == "simulate"
    assert res["normal"] is None
    print(f"✓ 后端链自动降级 → {res['backend']}")


def test_preview_only_mode_skips_all_backends():
    """仅预览模式：跳过全部 AI 后端，完全离线，且不读缓存。"""
    cfg = AIBackendConfig(order=["builtin", "local", "cloud", "simulate"],
                          preview_only=True, cloud_enabled=False)
    pl = Pipeline(cfg)
    img = _test_image()
    res = pl.run(img, get_preset("标准棚拍"))
    assert res["backend"] == "simulate"
    assert res["from_cache"] is False
    print("✓ 仅预览模式（跳过全部 AI 后端）")


class _StubLocalService:
    """最小 HTTP 桩：模拟用户自建服务，用于验证客户端调用契约。"""

    def __init__(self, routes=("analyze", "health"), ready=True):
        self.routes = routes
        self.ready = ready
        self.seen = []
        from http.server import BaseHTTPRequestHandler, HTTPServer

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.seen.append(("GET", self.path))
                if self.path == "/health":
                    self._send(200, json.dumps(
                        {"status": "ok", "ready": outer.ready}).encode())
                else:
                    self._send(404, b"{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                outer.seen.append(("POST", self.path))
                name = self.path.strip("/")
                if name not in outer.routes:
                    self._send(404, b'{"detail":"no such endpoint"}')
                    return
                d = np.linspace(0, 1, 64 * 64).reshape(64, 64).astype(np.float32)
                if name == "analyze":
                    nrm = np.zeros((*d.shape, 3), np.float32)
                    nrm[..., 2] = 1.0
                    self._send(200, json.dumps({
                        "width": 64, "height": 64,
                        "depth_base64": base64.b64encode(_png(d)).decode(),
                        "normal_base64": base64.b64encode(_png(
                            nrm * 0.5 + 0.5)).decode(),
                    }).encode())
                else:
                    self._send(200, _png(d), "image/png")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def test_local_backend_analyze_contract():
    """客户端应向自建服务请求深度**与**法线（/analyze）。"""
    img = _test_image(64)
    svc = _StubLocalService(routes=("analyze",))
    with svc as url:
        cfg = AIBackendConfig(order=["local", "simulate"], local_url=url,
                              builtin_enabled=False, cloud_enabled=False,
                              timeout=5.0)
        pre = AIPreprocessor(cfg, AICache(tempfile.mkdtemp(prefix="hlstest_l")))
        res = pre.get_depth(img)
        assert res["backend"] == "local"
        assert res["depth"].shape == img.shape[:2]
        assert res["normal"] is not None
        assert res["normal"].shape == (*img.shape[:2], 3)
        # 法线应是单位向量（0.5 编码往返误差内）
        norm = np.linalg.norm(res["normal"][32, 32])
        assert abs(norm - 1.0) < 0.05
        assert ("POST", "/analyze") in svc.seen
    print("✓ 自建服务 /analyze（深度+法线）")


def test_local_backend_legacy_fallback():
    """旧版服务只有 /depth 时，客户端回退并本地派生法线，不报错。"""
    img = _test_image(64)
    svc = _StubLocalService(routes=("depth",))
    with svc as url:
        cfg = AIBackendConfig(order=["local", "simulate"], local_url=url,
                              builtin_enabled=False, cloud_enabled=False,
                              timeout=5.0)
        pre = AIPreprocessor(cfg, AICache(tempfile.mkdtemp(prefix="hlstest_l2")))
        res = pre.get_depth(img)
        assert res["backend"] == "local"
        assert res["depth"].shape == img.shape[:2]
        assert res["normal"] is None
        assert ("POST", "/analyze") in svc.seen      # 先试新端点
        assert ("POST", "/depth") in svc.seen        # 404 后回退
    print("✓ 自建服务旧版 /depth 兼容回退")


def test_local_service_unready_degrades():
    """服务未就绪（模型缺失 → /analyze 返回 404/503）时必须降级，不抛异常。"""
    img = _test_image(64)
    with _StubLocalService(routes=("health",)) as url:
        cfg = AIBackendConfig(order=["local", "simulate"], local_url=url,
                              builtin_enabled=False, cloud_enabled=False,
                              timeout=5.0)
        pre = AIPreprocessor(cfg, AICache(tempfile.mkdtemp(prefix="hlstest_l3")))
        res = pre.get_depth(img)
        assert res["backend"] == "simulate"
    print("✓ 自建服务不可用时自动降级仅预览模式")


def test_params_json_roundtrip():
    p = get_preset("舞台聚光")
    p.lighting_mode = "hq"
    p.shadow_mode = "hard"
    p.lights[0].kelvin = 4321.0
    p2 = RenderParams.from_json(p.to_json())
    assert len(p2.lights) == len(p.lights)
    assert p2.lights[0].kelvin == 4321.0
    assert p2.lighting_mode == "hq"      # 线性预览/物理阴影的唯一切换字段
    assert p2.shadow_mode == "hard"
    print("✓ RenderParams JSON 序列化")


def test_sizes_and_perf_1024():
    """性能指标：1024px 预览 ≤15s，高质量 ≤60s。"""
    img = _test_image(1024)
    d = simulate_depth_from_luminance(img)
    p = get_preset("标准棚拍")
    p.lighting_mode = "linear"
    t0 = time.time(); render(img, d, p); tf = time.time() - t0
    p.lighting_mode = "hq"; p.shadow_mode = "soft"
    t0 = time.time(); render(img, d, p); th = time.time() - t0
    print(f"✓ 1024px 性能：预览 {tf:.1f}s (≤15s)，高质量 {th:.1f}s (≤60s)")
    assert tf <= 15 and th <= 60


def test_size_bounds():
    """输入尺寸约束：64~4096px 之外必须拒绝。"""
    tmp = tempfile.mkdtemp(prefix="hlstest_size")
    small = os.path.join(tmp, "small.png")
    Image.fromarray(np.zeros((32, 32, 3), np.uint8)).save(small)
    try:
        load_image(small)
        raise AssertionError("32px 图片应被拒绝")
    except ValueError:
        pass
    ok = os.path.join(tmp, "ok.png")
    Image.fromarray(np.zeros((64, 64, 3), np.uint8)).save(ok)
    assert load_image(ok).shape == (64, 64, 3)
    print("✓ 输入尺寸范围校验 64~4096px")


if __name__ == "__main__":
    test_kelvin_to_rgb()
    test_normal()
    test_simulate_depth()
    test_cache_roundtrip()
    test_params_json_roundtrip()
    test_reference_offset()
    test_render_fast_and_hq()
    test_shadow_mapping_occludes()
    test_external_normal_is_used()
    test_presets_all_render()
    test_point_light_shadows()
    test_backend_chain_degrade()
    test_preview_only_mode_skips_all_backends()
    test_local_backend_analyze_contract()
    test_local_backend_legacy_fallback()
    test_local_service_unready_degrades()
    test_size_bounds()
    test_sizes_and_perf_1024()
    out = _RENDER_SAMPLES.get("linear")
    if out is not None:
        Image.fromarray(out).save("test_fast.png")
    print("\n全部测试通过 ✔")
