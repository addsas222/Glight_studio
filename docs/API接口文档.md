# API 接口文档

本文档覆盖三部分：

1. **本地自建深度服务**（`server/app.py`，FastAPI）—— 用户自行部署的独立进程
2. **云端 AI 后端**（Replicate / 阿里云 / OpenAI 兼容）
3. **核心 Python API**（`core/`，供二次开发与批处理）

---

## 一、本地自建深度服务

### 1.1 基本信息

| 项 | 值 |
|---|---|
| 框架 | FastAPI（+ Uvicorn） |
| 默认地址 | `http://127.0.0.1:8765` |
| 启动 | `uvicorn server.app:app --host 127.0.0.1 --port 8765` |
| 交互式文档 | `GET /docs`、`GET /redoc`、`GET /openapi.json`（FastAPI 自带） |
| 请求体格式 | 原始图片字节（PNG / JPEG），`Content-Type: image/png` 或 `image/jpeg` |
| 鉴权 | 无（仅监听本机回环地址，请勿暴露到公网） |
| 并发 | 单进程同步推理；ONNX 会话按模型路径进程内缓存 |

**环境变量**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HLS_MODEL_PATH` | 自动挑选：已部署的 `depth-anything-v2-small.onnx`，否则 `model-small.onnx` | 深度 ONNX 模型 |
| `HLS_NORMAL_MODEL_PATH` | `~/.horizon_light_studio/models/moge2-vits-normal.onnx`（存在时） | 可选法线 ONNX 模型；文件不存在则由深度几何派生法线 |

**支持的模型**（按 ONNX 文件内部签名自动识别，不看文件名）：

| 家族 | 用途 | 输入约定 | 输出约定 |
|---|---|---|---|
| Depth Anything V2 | 深度 | 等比缩放，短边 518 / 长边 ≤1036，两侧为 14 的倍数；ImageNet 归一化 | 视差，**大 = 近**，无需取反 |
| MiDaS-small | 深度 | 按模型声明的尺寸（常见 `0` / 256×256） | 视差，**大 = 近** |
| MoGe-2（法线分支） | 法线 | **黑边补方** → 518×518；令 `num_tokens = (518/14)² = 1369` | 单位法线；输出在 OpenCV 系，**z 已取反**为「+z 朝观察者」 |

**尺寸约束**：短边 ≥ 64，长边 ≤ 4096。超出返回 `413`。

**颜色约定**

- 深度图：8bit 灰度，**白 = 近，黑 = 远**
- 法线图：8bit RGB，切线空间，`0.5`（即像素值 128）表示零

### 1.2 错误码

| 状态码 | 触发条件 | 说明 |
|---|---|---|
| `400` | 请求体为空，或不是可解析的图片 | `detail` 给出具体原因 |
| `413` | 尺寸超限（短边 < 64 或长边 > 4096） | `detail` 给出实际尺寸与许可范围 |
| `422` | 请求体不符合接口约定 | FastAPI 参数校验失败 |
| `502` | 法线模型输出形状异常（期望 `(1,3,H,W)`） | 仅在使用 `HLS_NORMAL_MODEL_PATH` 时可能发生 |
| `503` | **深度模型未加载** | 服务**不会伪造结果**，客户端据此降级 |
| `500` | 其他未预期异常 | 查看服务控制台日志 |

> **重要设计**：深度模型缺失时接口返回 `503` 而**不是**模拟深度。
> 这样桌面软件能如实判断「自建服务不可用」，从而降级到下一后端，
> 而不是把模拟结果错记成「自建服务」的结果。

### 1.3 `GET /health`

存活与就绪探测。

**响应 200**

```json
{
  "status": "ok",
  "version": "1.2.0",
  "depth_model": "/home/user/.horizon_light_studio/models/depth-anything-v2-small.onnx",
  "normal_model": "/home/user/.horizon_light_studio/models/moge2-vits-normal.onnx",
  "normal_source": "onnx",
  "ready": true,
  "message": "就绪"
}
```

| 字段 | 说明 |
|---|---|
| `ready` | **模型是否就绪**。`false` 时服务可用但所有推理接口返回 `503` |
| `depth_model` | 已加载的深度模型路径；未就绪为 `null` |
| `normal_model` | 已加载的法线模型路径；未使用为 `null` |
| `normal_source` | `onnx`（法线来自独立模型）或 `derived-from-depth`（由深度几何派生） |

模型未就绪时：

```json
{
  "status": "ok",
  "ready": false,
  "message": "深度模型未加载：请运行 server/setup_deployment 下载，或设置 HLS_MODEL_PATH"
}
```

### 1.4 `POST /analyze`

**桌面软件默认调用的接口**：一次返回深度**与**法线。

- 请求体：图片原始字节
- 响应 `200`：`application/json`

```json
{
  "width": 512,
  "height": 512,
  "depth_encoding": "png8: white=near, black=far",
  "normal_encoding": "png8: tangent-space, 0.5=zero",
  "normal_source": "derived-from-depth",
  "depth_base64": "iVBORw0KGgo...",
  "normal_base64": "iVBORw0KGgo..."
}
```

| 字段 | 说明 |
|---|---|
| `width` / `height` | 输入图片尺寸（输出深度/法线与之一致） |
| `depth_base64` | PNG 深度图，base64（白=近） |
| `normal_base64` | PNG 法线图，base64（0.5=零） |
| `normal_source` | 法线来源，同 `/health` |

```bash
curl -X POST --data-binary @input.png \
     -H "Content-Type: image/png" \
     http://127.0.0.1:8765/analyze
```

### 1.5 `POST /depth`

- 请求体：图片原始字节
- 响应 `200`：`image/png`，与输入同尺寸的 8bit 灰度深度图（白=近）

```bash
curl -X POST --data-binary @input.png \
     -H "Content-Type: image/png" \
     http://127.0.0.1:8765/depth -o depth.png
```

### 1.6 `POST /normal`

- 请求体：图片原始字节
- 响应 `200`：`image/png`，RGB 法线图（R/G/B = 法线 x/y/z × 0.5 + 0.5）

```bash
curl -X POST --data-binary @input.png \
     -H "Content-Type: image/png" \
     http://127.0.0.1:8765/normal -o normal.png
```

### 1.7 客户端调用契约

桌面软件（`core/ai_backend.py`）对自建服务的调用顺序：

1. `POST /analyze` —— 期望同时拿到深度与法线；
2. 若返回 **404 / 405 / 501**（旧版服务无该端点），回退到 `POST /depth`，
   法线改由本地几何从深度派生（`height_to_normal`）；
3. 若返回 **503**（模型未就绪）或其他错误，本次调用失败，
   自动降级到后端链的下一级（云端 → 灰度模拟）。

因此旧版只实现 `/depth` 的服务**仍可继续使用**，不会报错。

### 1.8 部署

```bash
# Linux / macOS
cd server && ./setup_deployment.sh && ./run_server.sh

# Windows
cd server
setup_deployment.bat
run_server.bat
```

依赖清单见 `server/requirements.txt`。部署脚本会创建**服务专用虚拟环境
`.venv-server`**（与开发用的 `.venv` 分开）、安装依赖并下载 MiDaS-small（约 64MB）。

---

## 二、云端 AI 后端

由 `core/ai_backend.py` 实现，在软件「设置 → AI 后端」中配置。

| 提供商 | `cloud_provider` | Base URL 示例 | `cloud_model` |
|---|---|---|---|
| OpenAI 兼容通用 | `openai_compatible` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` |
| Replicate | `replicate` | `https://api.replicate.com/v1` | model version hash |
| 阿里云视觉智能 | `aliyun` | 能力 endpoint URL | Action 名，如 `EstimateImageDepth` |

**通用约定**

- 输入以图片字节（PNG）直接 POST，或按提供商要求编码（Replicate 用 data URI，
  阿里云用 `ImageURL` 字段）。
- 输出统一归一化为 `0~1`、白=近的深度图，并缩放回输入尺寸。
- **超时**：默认 3 秒（设置面板可调）。超时或失败自动降级到下一后端，
  界面不阻塞。
- **密钥**：保存在 `~/.horizon_light_studio/config.json`（本机），
  仅在使用云端后端时出网。

---

## 三、核心 Python API

`core/` 为零 GUI 依赖的共享引擎，可独立用于批处理与二次开发。

### 3.1 最小示例

```python
from core.ai_backend import AIBackendConfig
from core.pipeline import Pipeline, load_image
from core.presets import get_preset

cfg = AIBackendConfig(local_url="http://127.0.0.1:8765")
pl = Pipeline(cfg, log=print)
rgb = load_image("input.png")          # uint8 HxWx3，已做白底合成与尺寸校验
params = get_preset("逆光黄昏")
res = pl.run(rgb, params, progress=lambda p, msg: print(f"{p:.0%} {msg}"))
res["image"]                           # uint8 HxWx3 渲染结果
res["depth"], res["normal"], res["backend"], res["from_cache"], res["elapsed"]
```

### 3.2 `Pipeline`

| 方法 | 说明 |
|---|---|
| `run(rgb, params, progress=None, force_backend=None)` | 深度获取 + 渲染，返回结果字典 |
| `run_file(in_path, out_path, params, progress=None, force_backend=None)` | 读文件、渲染、写同尺寸新文件 |

`progress` 回调签名为 `(float 0~1, str 中文说明)`。

### 3.3 `AIBackendConfig`

```python
AIBackendConfig(
    order=["builtin", "local", "cloud", "simulate"],  # 后端链顺序
    preview_only=False,          # 仅预览模式（跳过全部 AI 后端，完全离线）
    builtin_enabled=True, builtin_model_path="",
    local_enabled=True, local_url="http://127.0.0.1:8765",
    cloud_enabled=False, cloud_provider="openai_compatible",
    cloud_base_url="", cloud_api_key="", cloud_model="",
    timeout=3.0,
)
```

| 方法 | 说明 |
|---|---|
| `save(path="")` | 写入 `~/.horizon_light_studio/config.json` |
| `load(path="")` | 读取配置；文件缺失或损坏时返回默认配置，不抛异常 |

### 3.4 `AIPreprocessor.get_depth`

```python
res = AIPreprocessor(cfg).get_depth(rgb, image_bytes=b"", force_backend=None)
# {'depth': HxW float32 0~1 (大=近), 'normal': HxWx3 float32 -1~1 或 None,
#  'backend': str, 'from_cache': bool}
```

### 3.5 渲染参数 `RenderParams`

```python
from core.types import RenderParams, Light

params = RenderParams(
    lights=[Light(name="主光", kind="directional",
                  dx=-0.5, dy=-0.6, dz=0.7,      # 指向光源的单位向量
                  intensity=1.0, kelvin=5500, radius=0.35)],
    ambient_intensity=0.45, ambient_kelvin=6500.0,
    shadow_mode="soft",        # soft=柔化半影 / hard=硬朗赛璐珞
    lighting_mode="linear",    # linear=快速预览 / hq=高质量物理阴影
    preview_size=512,          # 快速预览的光照计算分辨率上限
    lighting_size=1024,        # 高质量模式的光照计算分辨率上限
    shadow_strength=0.85,      # 阴影强度 0~1
    specular_strength=0.35, tone_preserve=0.6, exposure=1.0,
)
```

`Light.kind` 为 `directional` 时用 `dx/dy/dz`；
为 `point` 时用 `px/py/pz`（归一化图像坐标，`pz` 以图宽为单位）。

### 3.6 直接调用渲染器

```python
from core.render import render
out = render(rgb, depth, params, progress=None, normal=None)
```

`normal` 可传入 AI 法线图（`HxWx3`，单位向量 `-1~1`）；为 `None` 时
由深度场几何派生。`params.lighting_mode == "hq"` 时执行逐光源阴影投射。

### 3.7 参考图迁移

```python
from core.reference import analyze_reference, apply_reference_offset
feats = analyze_reference(ref_rgb)           # {'light_dx','light_dy','kelvin','brightness'}
params2 = apply_reference_offset(params, feats)   # 限幅 ±15° / ±500K / ±0.25
```

### 3.8 缓存

```python
from core.cache import AICache
cache = AICache()                                   # ~/.horizon_light_studio/cache
key = AICache.key_for(image_bytes, "local", "http://127.0.0.1:8765")
cache.put(key, depth, {"backend": "local"}, normal)  # 写 depth.png / normal.png / meta.json
hit = cache.get(key)                                 # {'depth','normal','from_cache',...}
cache.clear()                                        # 返回清除条目数
```

---

## 四、后端链与降级

默认顺序：`builtin → local → cloud → simulate`。

- 任一级失败或超时（默认 3 秒）自动切换下一级；
- `simulate`（灰度梯度模拟）永不失败，是保底；
- 结果按「**图片内容哈希 + 后端标识 + 模型提示**」缓存，相同图片不重复调用；
- **被禁用的后端不会被调用，也不会命中它的缓存**（在设置里关掉某后端后，
  不会再悄悄复用该后端此前的旧结果）；
- 若启用了 `preview_only`，直接跳过全部 AI 后端与缓存，使用灰度模拟。

---

## 五、本机软件接口（界面 ↔ 引擎）

桌面软件以本地服务形式提供界面（WebUI），前缀 `/api`，默认
`http://127.0.0.1:8756`。仅允许本机来源访问（非本机 `Origin` 返回
`403 forbidden_origin`）。


### 5.1 图片接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 预设列表、设置、缓存目录、模型就绪状态、ffmpeg 可用性、视频限制 |
| POST | `/api/image` | 上传图片（原始字节）→ `image_id`、尺寸、原始 PNG、高分辨率提示 |
| POST | `/api/analyze` | 对 `image_id` 做 AI 分析 → 深度 PNG（+ 法线 PNG）与来源后端 |
| POST | `/api/render` | 按参数渲染 → 结果 PNG、耗时、后端、是否命中缓存 |
| POST | `/api/pick` | 点击像素 → 该点法线与主光方向 |
| POST | `/api/reference` | 参考图保守迁移 → 新参数与偏移量 |
| POST | `/api/export` | 导出 PNG/JPEG（base64 返回，由界面下载） |
| POST | `/api/depth-preview` | 彩色化深度图预览 |
| POST | `/api/settings` | 读写 AI 后端设置（密钥不回传，只回 `cloud_api_key_set`） |
| POST | `/api/cache/clear` | 清理缓存 |
| POST | `/api/media` | **统一入口**：按文件内容自动识别图片/视频 |
| POST | `/api/shutdown` | 请求退出软件（202 后进程自行停止；供界面「退出」按钮调用） |

`/api/state` 另含：`params`（界面当前参数）、`themes`（主题清单）、
`ffmpeg`（ffmpeg 是否可用）、`video_exts`、`video_limits`。

### 5.2 新增功能接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/themes` | 主题清单（默认 `blue`） |
| GET/POST | `/api/theme/{id}` / `/api/theme` | 读取主题 / **选择并持久化主题**（`{id}`，写入 config.json） |
| POST | `/api/autolight` | **文字 → 光照节点**：`{text, use_cloud?, base_params?, apply?}` |
| GET | `/api/plugins` | 插件清单与启用状态 |
| POST | `/api/plugins/install` | 安装插件（提交清单 JSON；只解析数据，不执行代码） |
| POST | `/api/plugins/toggle` | 启用/停用插件：`{id, enabled}` |
| POST | `/api/plugins/remove` | 移除插件：`{id}` |
| GET | `/api/workflow/types` | 可用节点类型与默认工作流 |
| POST | `/api/workflow/evaluate` | 校验并求值节点图：`{workflow}` → `{ok, problems, params}` |
| GET | `/api/cues` | CUE 预设列表 |
| POST | `/api/cues/save` | 保存 CUE：`{name, params, fade, id?}`（`fade` 为淡变秒数，0~30；同 id 则更新） |
| POST | `/api/cues/delete` | 删除 CUE：`{id}` |
| POST | `/api/cues/interpolate` | 淡变中途参数插值：`{id, params, t}`，`t` 为 0~1 |
| POST | `/api/harvest` | **智能拾光**：提交参考图字节 → 场景类型、光源方案、光比、对比度、notes |
| POST | `/api/strokes/preview` | **逐帧光绘预览**：`{video_id, index, strokes}` → 合成后的帧 PNG |
| POST | `/api/strokes/export` | 逐帧光绘导出（202 后台任务）：`{video_id, params, track}`，`track` 为 `{"帧号": [stroke,…]}` |
| GET | `/api/strokes/result/{job_id}` | 下载光绘导出的成品视频 |

自动打光示例：

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"text":"黄昏舞台逆光，暖橘主光从左后方打来，冷蓝补光勾勒轮廓","use_cloud":false}' \
  http://127.0.0.1:8756/api/autolight
```

返回 `rig`（光源节点）、`params`（可直接渲染的参数）、`nodes`（可灌入工作流）、
`note`（中文解析说明）、`source`（`offline` 或 `cloud`）。

工作流求值失败时返回 `{"ok": false, "problems": ["工作流存在环路，无法求值"]}`，
不会抛 500。

> `/api/harvest` 的返回**不含**照度(lx)/CRI/Δuv/闪烁/功率等引擎无法计算的量，
> 并在 `notes` 中附中文声明；可推导的只有光比、对比度、色温一致性与相对强度。

`strokes` 元素的取值（与引擎严格对应，非法值会回落到默认而不是报错）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `type` | `point` / `glow` / `beam` / `starburst` | 点光源 / 柔光晕 / 光束 / 星芒 |
| `blend` | `add` / `screen` / `soft` | 叠加 / 滤色 / 柔光 |
| `x`/`y` | 0~1 | 归一化图像坐标 |
| `radius` | 0.01~0.6 | 归一化（相对长边） |
| `intensity` | 0~4 | 强度 |
| `kelvin` | 1500~12000 | 色温 |
| `softness` | 0~1 | 柔边 |
| `angle` / `spread` | 度 | 仅 `beam` 使用（朝向 / 张角） |
| `spikes` | 2~12 | 仅 `starburst` 使用 |

### 5.3 视频接口

单位说明：`video_id` 由上传返回；`index` 为帧序号（从 0 开始）。

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| POST | `/api/video` | 原始视频字节（`X-Filename` 可选） | 返回 `video_id`、`info`（宽高/帧率/时长/帧数/编码/是否有音轨）、`hints` |
| POST | `/api/video/frame` | `{video_id, index}` | 返回该帧 PNG（base64） |
| POST | `/api/video/keyframes` | `{video_id, count}` | 返回关键帧序号列表 |
| POST | `/api/video/pick` | `{video_id, index, x, y}` | 在某一帧上拾取 → `{dx,dy,dz,nx,ny,nz}` |
| POST | `/api/video/process` | `{video_id, params, mode, keyframe_count, smooth}` | **202** 返回 `{job_id}`，后台处理 |
| GET | `/api/video/job/{job_id}` | — | `{state, progress, message, result, error}` |
| POST | `/api/video/job/{job_id}/cancel` | — | 取消任务（清理半成品） |
| GET | `/api/video/result/{video_id}` | — | 下载成品视频（含音轨） |
| POST | `/api/video/delete` | `{video_id}` | 删除临时文件与登记项 |

`mode` 取值：`keyframe`（关键帧分析传播，默认）或 `perframe`（逐帧分析）。

请求示例：

```bash
# 上传
curl -X POST --data-binary @in.mp4 -H "Content-Type: video/mp4" \
     http://127.0.0.1:8756/api/video

# 开始处理（关键帧传播）
curl -X POST -H "Content-Type: application/json" \
     -d '{"video_id":"<id>","params":{...},"mode":"keyframe","keyframe_count":8}' \
     http://127.0.0.1:8756/api/video/process

# 轮询进度
curl http://127.0.0.1:8756/api/video/job/<job_id>

# 下载成品
curl -o out.mp4 http://127.0.0.1:8756/api/video/result/<video_id>
```

错误响应统一为 `{"error": str, "detail": str}`：
`400 bad_params`、`404 unknown_video` / `unknown_image`、
`409 video_busy`（该视频已有任务在跑）、`503 ffmpeg_missing`、`500` 其他。

---


### 5.4 内置模型接口（`/api/model/*`）

供界面「**一键下载模型**」「**离线导入模型…**」以及脚本化/离线部署使用。

| 方法 | 路径 | 请求 | 行为 |
|---|---|---|---|
| POST | `/api/model/download?kind=depth` | 空 | 下载 **MiDaS-small**（约 64MB）。**幂等**：已就绪时直接返回 `already: true`，不联网 |
| POST | `/api/model/download?kind=dav2` | 空 | 下载 **Depth Anything V2 Small**（约 94MB）。同样幂等 |
| POST | `/api/model/download?kind=normal` | 空 | 下载 **MoGe-2 法线**模型（约 134MB）。同样幂等 |
| POST | `/api/model/import?kind=…` | `.onnx` 原始字节 | 落盘到模型目录，**实际加载校验**后立即生效（无需重启） |
| GET | `/api/model/progress` | 空 | 当前下载进度（前端进度条每 400ms 轮询；无下载时 `active:false`） |

`kind` 省略时为 `depth`。三种取值都会**校验文件确实属于该家族**，防止
「导入成功但推理时才失败」：

- `kind=depth`：必须是深度模型（MiDaS 或 DAV2），传入法线模型会被拒；
- `kind=dav2`：必须是 DAV2（固定下载/导入到 `depth-anything-v2-small.onnx`）；
- `kind=normal`：必须带 `normal` 输出（MoGe-2）。

`GET /api/model/progress` 返回 `{active, kind, done, total, percent}`，`done` / `total` 单位是**字节**。
**服务器没给 `Content-Length` 时 `total` 为 0、`percent` 恒为 0** —— 此时真实进度不可知，
调用方只能显示「已接收 N MB」，**不得拿 `done` 冒充总量去算百分比**；
下载失败时 `active` 会复位为 `false`（否则界面会永远停在「下载中…」）。

**错误码（重点）**

| 状态 | `error` | 触发条件 |
|---|---|---|
| `200` | — | 成功；`download` 的 `already:true` 表示已有模型、未重复下载 |
| `400` | `empty_body` | `import` 请求体为空 |
| `400` | `bad_kind` | `kind` 不是 `depth` / `dav2` / `normal` |
| `400` | `bad_model` | 文件不像是 ONNX；或**通过了大小检查但无法加载**（这是关键：仅按 >1MB 判断会放过伪文件，导致界面误报就绪、推理时静默降级）；或**家族不符**（如把法线模型当深度模型导入） |
| `500` | `write_failed` | 写入模型目录失败（权限/磁盘） |
| `502` | `download_failed` | 下载失败或超时（默认 30 秒）。`detail` 会提示用镜像环境变量，或改用离线导入 |

```bash
# 一键下载（幂等）；kind 省略等价于 depth
curl -X POST "http://127.0.0.1:8756/api/model/download"
curl -X POST "http://127.0.0.1:8756/api/model/download?kind=dav2"
curl -X POST "http://127.0.0.1:8756/api/model/download?kind=normal"

# 轮询下载进度（下载是同步长请求，界面靠这个接口显示进度条）
curl -s "http://127.0.0.1:8756/api/model/progress"
# {"active":true,"kind":"depth","done":5242880,"total":66764249,"percent":7.9}

# 离线导入本地 .onnx
curl -X POST --data-binary @depth-anything-v2-small.onnx \
     -H "Content-Type: application/octet-stream" \
     "http://127.0.0.1:8756/api/model/import?kind=dav2"
```

> 下载源可用环境变量覆盖：`HLS_MODEL_URL`（MiDaS）、`HLS_DEPTH_MODEL_URL`
> （Depth Anything V2）、`HLS_NORMAL_MODEL_URL`（MoGe-2 法线）。
> 默认的 DAV2 / MoGe 源已指向 `hf-mirror.com`（huggingface.co 在国内常不可达）。
> 失败（异常）不会留下半成品；导入失败也不会污染已就绪的模型。
> 临时文件名是**唯一**的（为并发安全），因此若进程被硬杀（关窗 / 任务管理器）
> 可能留下一个约 64MB 的 `.part`/`.importing` 孤儿文件。这类残留会在**之后的某次**
> 启动时被自动回收 —— 判据是该文件已超过 10 分钟未变动，因此刚留下的孤儿
> 不会在下一次启动就被清掉，需等到 10 分钟之后的某次启动。
> 之所以加时间判断：下载也把临时文件放在同一目录，无差别删除会打断
> 另一个进程（例如正在跑 `setup_deployment.sh`）的进行中下载。
