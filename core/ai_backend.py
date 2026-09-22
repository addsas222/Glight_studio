# -*- coding: utf-8 -*-
"""AI 预处理层：四级后端链，产出深度图。

优先级（可在设置中调整/禁用）：
  1. builtin   —— 软件内置本地深度引擎（ONNX Runtime + MiDaS-small，离线）
  2. local     —— 用户自建 FastAPI 服务（http://localhost:8765）
  3. cloud     —— 云端 API（阿里云 / Replicate / OpenAI 兼容通用接口）
  4. simulate  —— 灰度梯度模拟降级（仅预览模式）

任一级失败/超时（默认 3 秒）自动降级到下一级，绝不抛出阻塞异常。
所有结果经 AICache 持久化，相同图片不重复调用。
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.request
import urllib.error
from typing import Callable, List, Optional

import numpy as np
from PIL import Image

from .cache import AICache
from .degrade import simulate_depth_from_luminance

BACKEND_ORDER_DEFAULT = ["builtin", "local", "cloud", "simulate"]

from .paths import subdir

CONFIG_PATH_DEFAULT = subdir("config.json")


class AIBackendConfig:
    """设置面板可直接操作的配置对象（可 JSON 序列化）。"""

    def __init__(self, **kw):
        self.order: List[str] = kw.get("order", list(BACKEND_ORDER_DEFAULT))
        # 仅预览模式：跳过全部 AI 后端，直接用灰度梯度模拟深度（完全离线）
        self.preview_only: bool = kw.get("preview_only", False)
        # 内置本地引擎
        self.builtin_model_path: str = kw.get("builtin_model_path", "")
        self.builtin_enabled: bool = kw.get("builtin_enabled", True)
        # 内置法线模型（可选）：MoGe-2 法线分支。留空或文件缺失时，
        # 法线由深度场几何派生（depth_to_height → height_to_normal）。
        self.builtin_normal_model_path: str = kw.get(
            "builtin_normal_model_path", "")
        # 用户自建服务
        self.local_url: str = kw.get("local_url", "http://127.0.0.1:8765")
        self.local_enabled: bool = kw.get("local_enabled", True)
        # 云端
        self.cloud_provider: str = kw.get("cloud_provider", "openai_compatible")
        #   openai_compatible | replicate | aliyun
        self.cloud_base_url: str = kw.get("cloud_base_url", "")
        self.cloud_api_key: str = kw.get("cloud_api_key", "")
        self.cloud_model: str = kw.get("cloud_model", "")
        self.cloud_enabled: bool = kw.get("cloud_enabled", False)
        # 通用
        self.timeout: float = kw.get("timeout", 3.0)
        # 界面主题（默认蓝色；由设置持久化，重启后保持）
        self.theme: str = kw.get("theme", "blue")

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "AIBackendConfig":
        return cls(**json.loads(s))

    def save(self, path: str = "") -> str:
        """把设置写入本地配置文件（含 API Key），重启软件后自动恢复。"""
        p = path or CONFIG_PATH_DEFAULT
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        return p

    @classmethod
    def load(cls, path: str = "") -> "AIBackendConfig":
        """读取本地设置；文件缺失或损坏时返回默认配置（不抛异常）。"""
        p = path or CONFIG_PATH_DEFAULT
        try:
            with open(p, "r", encoding="utf-8") as f:
                return cls(**json.load(f))
        except Exception:
            return cls()


class AIPreprocessor:
    def __init__(self, config: Optional[AIBackendConfig] = None,
                 cache: Optional[AICache] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config = config or AIBackendConfig()
        self.cache = cache or AICache()
        self.log = log or (lambda msg: None)

    # ---------- 对外主入口 ----------

    def get_depth(self, rgb: np.ndarray, image_bytes: bytes = b"",
                  force_backend: Optional[str] = None) -> dict:
        """返回 {'depth': HxW float32 (0~1, 大=近), 'backend': str, 'from_cache': bool}。"""
        if not image_bytes:
            buf = io.BytesIO()
            Image.fromarray(rgb).save(buf, format="PNG")
            image_bytes = buf.getvalue()

        if self.config.preview_only and not force_backend:
            self.log("仅预览模式：跳过全部 AI 后端，使用灰度梯度模拟深度")
            return {"depth": simulate_depth_from_luminance(rgb),
                    "normal": None, "backend": "simulate", "from_cache": False}

        order = [force_backend] if force_backend else self.config.order
        cache_keys = {}
        for backend in order:
            # 被禁用的后端既不能调用，也不能命中它的缓存：
            # 否则用户在设置里关掉某后端后，仍会悄悄复用该后端此前的旧结果。
            if not (force_backend or self._enabled(backend)):
                continue
            if backend == "simulate":
                continue  # 模拟结果不缓存
            cache_keys[backend] = AICache.key_for(
                image_bytes, backend, self._model_hint(backend))
            hit = self.cache.get(cache_keys[backend])
            if hit is not None:
                self.log(f"缓存命中：{backend}")
                return {"depth": hit["depth"], "normal": hit.get("normal"),
                        "backend": hit.get("backend", backend),
                        "from_cache": True}

        for backend in order:
            fn = {
                "builtin": self._try_builtin,
                "local": self._try_local,
                "cloud": self._try_cloud,
                "simulate": self._try_simulate,
            }.get(backend)
            if fn is None:
                continue
            t0 = time.time()
            try:
                res = _as_backend_result(fn(rgb))
            except Exception as e:
                self.log(f"后端 {backend} 失败：{e}")
                continue
            if res is None:
                continue
            depth = res["depth"]
            if depth is None or depth.size == 0:
                continue
            normal = res.get("normal")
            self.log(f"后端 {backend} 成功，耗时 {time.time()-t0:.1f}s")
            if backend in cache_keys:
                self.cache.put(cache_keys[backend], depth,
                               {"backend": backend, "w": int(depth.shape[1]),
                                "h": int(depth.shape[0]),
                                "normal": normal is not None},
                               normal)
            return {"depth": depth, "normal": normal,
                    "backend": backend, "from_cache": False}

        # 理论不可达：simulate 永不失败
        return {"depth": simulate_depth_from_luminance(rgb),
                "backend": "simulate", "from_cache": False}

    def _enabled(self, backend: str) -> bool:
        """该后端在设置中是否启用。simulate 是保底，永远可用。"""
        c = self.config
        if backend == "builtin":
            return bool(c.builtin_enabled)
        if backend == "local":
            return bool(c.local_enabled)
        if backend == "cloud":
            return bool(c.cloud_enabled)
        return True

    def _model_hint(self, backend: str) -> str:
        c = self.config
        if backend == "builtin":
            # 两个模型都进缓存键：换掉法线模型后必须重算，否则会一直
            # 复用旧模型产出的法线（会话缓存只认文件身份，不认配置）。
            return c.builtin_model_path + "|" + c.builtin_normal_model_path
        if backend == "local":
            return c.local_url
        if backend == "cloud":
            return c.cloud_provider + "|" + c.cloud_model
        return ""

    # ---------- 各后端实现 ----------

    def _try_builtin(self, rgb: np.ndarray) -> Optional[dict]:
        """内置离线引擎：深度 + 法线（法线模型缺失时 normal=None，由渲染层派生）。

        法线模型是可选增强：它比「深度几何派生」能还原更多体积细节，
        但缺失/推理失败不应让整个内置后端失效，故只降级为 None。
        """
        if not self.config.builtin_enabled:
            return None
        from .preprocess import analyze
        return analyze(rgb, self.config.builtin_model_path,
                       self.config.builtin_normal_model_path,
                       self.config.timeout, self.log)

    def _try_local(self, rgb: np.ndarray) -> Optional[dict]:
        """调用用户自建服务。

        优先 POST /analyze（深度 + 法线一次返回）；旧版服务无该端点时
        回落到 POST /depth（法线由本地几何派生）。
        """
        if not self.config.local_enabled:
            return None
        base = self.config.local_url.rstrip("/")
        png = encode_png(rgb)
        try:
            data = self._http(_post_png(base + "/analyze", png),
                              self.config.timeout)
            j = json.loads(data)
            return {
                "depth": _resize_depth(
                    _decode_png_b64(j["depth_base64"]), rgb.shape[:2]),
                "normal": _resize_normal(
                    _decode_png_b64(j["normal_base64"]), rgb.shape[:2]),
            }
        except urllib.error.HTTPError as e:
            if e.code not in (404, 405, 501):
                raise
            self.log("自建服务无 /analyze 端点，回退 /depth（法线本地派生）")
        data = self._http(_post_png(base + "/depth", png),
                          self.config.timeout)
        return {"depth": self._decode_depth_response(data, rgb.shape[:2]),
                "normal": None}

    def _try_simulate(self, rgb: np.ndarray) -> Optional[np.ndarray]:
        return simulate_depth_from_luminance(rgb)

    def _try_cloud(self, rgb: np.ndarray) -> Optional[np.ndarray]:
        c = self.config
        if not c.cloud_enabled or not c.cloud_api_key:
            return None
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        png = buf.getvalue()

        if c.cloud_provider == "replicate":
            return self._cloud_replicate(png, rgb.shape[:2])
        if c.cloud_provider == "aliyun":
            return self._cloud_aliyun(png, rgb.shape[:2])
        return self._cloud_openai_compatible(png, rgb.shape[:2])

    def _cloud_replicate(self, png: bytes, hw) -> Optional[np.ndarray]:
        c = self.config
        base = c.cloud_base_url or "https://api.replicate.com/v1"
        # 1) 上传/直接以 data URL 形式创建 prediction（Replicate 官方推荐先上传文件，
        #    简化实现：使用 data: URI，多数模型部署接受）
        import base64
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode()
        body = json.dumps({
            "version": c.cloud_model,
            "input": {"image": data_uri},
        }).encode()
        req = urllib.request.Request(
            base.rstrip("/") + "/predictions", data=body, method="POST",
            headers={"Authorization": f"Token {c.cloud_api_key}",
                     "Content-Type": "application/json"})
        resp = json.loads(self._http(req, self.config.timeout))
        pred_url = resp.get("urls", {}).get("get")
        if not pred_url:
            raise RuntimeError("Replicate 未返回 prediction 地址")
        # 2) 轮询（受总超时约束）
        deadline = time.time() + 30
        while time.time() < deadline:
            req2 = urllib.request.Request(
                pred_url, headers={"Authorization": f"Token {c.cloud_api_key}"})
            r = json.loads(self._http(req2, self.config.timeout))
            if r.get("status") == "succeeded":
                out = r.get("output")
                out_url = out if isinstance(out, str) else (out or [None])[0]
                if not out_url:
                    return None
                img = np.asarray(Image.open(
                    io.BytesIO(self._http(urllib.request.Request(out_url), 10))
                ), np.float32)
                return _resize_depth(img, hw)
            if r.get("status") in ("failed", "canceled"):
                raise RuntimeError("Replicate 预测失败: " + str(r.get("error")))
            time.sleep(1.5)
        raise TimeoutError("Replicate 轮询超时")

    def _cloud_aliyun(self, png: bytes, hw) -> Optional[np.ndarray]:
        """阿里云视觉智能开放平台：深度估计类能力。

        阿里云各能力签名流程较重（HMAC-SM3/RPC 签名），此处实现通用
        RPC POST 调用骨架，能力 endpoint 由用户在设置中填入
        （cloud_base_url 填完整能力 URL，cloud_model 填 Action 名）。
        若签名失败则抛异常并自动降级到下一后端。
        """
        c = self.config
        import base64, hashlib, hmac, uuid, urllib.parse
        action = c.cloud_model or "EstimateImageDepth"
        params = {
            "Action": action, "Version": "2020-09-30",
            "Format": "JSON", "AccessKeyId": c.cloud_api_key.split(":")[0],
            "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ImageURL": "data:image/png;base64," + base64.b64encode(png).decode(),
        }
        qs = "&".join(f"{urllib.parse.quote(k, safe='')}="
                      f"{urllib.parse.quote(str(v), safe='')}"
                      for k, v in sorted(params.items()))
        string_to_sign = "GET&%2F&" + urllib.parse.quote(qs, safe="")
        ak_secret = c.cloud_api_key.split(":", 1)[1] if ":" in c.cloud_api_key else ""
        sig = base64.b64encode(hmac.new(
            (ak_secret + "&").encode(), string_to_sign.encode(),
            hashlib.sha1).digest()).decode()
        url = c.cloud_base_url + "?" + qs + "&Signature=" + urllib.parse.quote(sig, safe="")
        resp = json.loads(self._http(urllib.request.Request(url), self.config.timeout))
        data_url = resp.get("Data", {}).get("URL") or resp.get("Data", {}).get("ImageUrl")
        if not data_url:
            raise RuntimeError("阿里云返回异常: " + json.dumps(resp, ensure_ascii=False)[:200])
        img = np.asarray(Image.open(
            io.BytesIO(self._http(urllib.request.Request(data_url), 10))), np.float32)
        return _resize_depth(img, hw)

    def _cloud_openai_compatible(self, png: bytes, hw) -> Optional[np.ndarray]:
        """OpenAI 兼容通用接口：期望视觉模型按提示返回 base64 深度图。

        cloud_base_url 例：https://dashscope.aliyuncs.com/compatible-mode/v1
        cloud_model 例：qwen-vl-max / gpt-4o 等。
        """
        c = self.config
        import base64
        b64 = base64.b64encode(png).decode()
        body = json.dumps({
            "model": c.cloud_model or "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text":
                     "Generate a monochrome depth map of this image "
                     "(white=near, black=far), same aspect ratio. "
                     "Return only the image as image/png base64 data URI."},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                ],
            }],
        }).encode()
        url = (c.cloud_base_url or
               "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {c.cloud_api_key}",
                     "Content-Type": "application/json"})
        resp = json.loads(self._http(req, self.config.timeout))
        content = resp["choices"][0]["message"]["content"]
        import re
        m = re.search(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)", content)
        if not m:
            raise RuntimeError("OpenAI 兼容接口未返回图像")
        img = np.asarray(Image.open(io.BytesIO(base64.b64decode(m.group(1)))), np.float32)
        return _resize_depth(img, hw)

    # ---------- 工具 ----------

    @staticmethod
    def _http(req: urllib.request.Request, timeout: float) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    @staticmethod
    def _decode_depth_response(data: bytes, hw) -> Optional[np.ndarray]:
        """自建服务返回 PNG（白=近）或 JSON {'depth_base64': ...}。"""
        try:
            j = json.loads(data)
            import base64
            img = Image.open(io.BytesIO(base64.b64decode(j["depth_base64"])))
        except (ValueError, KeyError):
            img = Image.open(io.BytesIO(data))
        return _resize_depth(np.asarray(img, np.float32), hw)


def _as_backend_result(v) -> Optional[dict]:
    """把各后端返回值统一成 {'depth':..., 'normal':...|None}。"""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    return {"depth": v, "normal": None}


def encode_png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return buf.getvalue()


def _post_png(url: str, png: bytes) -> urllib.request.Request:
    return urllib.request.Request(url, data=png, method="POST",
                                  headers={"Content-Type": "image/png"})


def _decode_png_b64(s: str) -> np.ndarray:
    import base64
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(s))), np.float32)


def _resize_normal(d: np.ndarray, hw) -> np.ndarray:
    """自建服务返回的法线 PNG（0.5=零）→ -1~1 且缩放目标尺寸。"""
    if d.ndim == 2:
        d = np.stack([d, d, np.full_like(d, 255.0)], -1)
    d = np.clip(d[..., :3], 0, 255) / 255.0 * 2.0 - 1.0
    if d.shape[0] != hw[0] or d.shape[1] != hw[1]:
        d = np.asarray(Image.fromarray(
            ((d * 0.5 + 0.5) * 255).astype(np.uint8)).resize(
            (hw[1], hw[0]), Image.BILINEAR), np.float32) / 255.0 * 2.0 - 1.0
    n = np.linalg.norm(d, axis=-1, keepdims=True)
    return (d / np.maximum(n, 1e-6)).astype(np.float32)


def _resize_depth(d: np.ndarray, hw) -> np.ndarray:
    """归一化到 0~1（大=近）并缩放到目标尺寸。"""
    if d.ndim == 3:
        d = d[..., 0] if d.shape[2] >= 1 else d.mean(-1)
    lo, hi = np.percentile(d, 1), np.percentile(d, 99)
    d = np.clip((d - lo) / max(hi - lo, 1e-3), 0, 1)
    # MiDaS 类输出为“越小越近”与“越大越近”不定，此处约定：
    # 内置模型/自建服务已保证“白=近”；云端按通用提示同样保证。
    if d.shape[0] != hw[0] or d.shape[1] != hw[1]:
        d = np.asarray(
            Image.fromarray((d * 255).astype(np.uint8)).resize(
                (hw[1], hw[0]), Image.LANCZOS), np.float32) / 255.0
    return d.astype(np.float32)
