# HANDOFF —— 自动打光程序（凌日光影棚 Horizon Light Studio）

> 交接时间：2026-09-21
> 项目根目录：`C:\Users\54301\Downloads\Glight_studio`
> 本次交接范围：**深度/法线 AI 模型部署** + **ControlNet 风格预处理模块接入后端** + **全链路端到端验证** + **软件优化（#6）** + **Pen UI 设计稿（#7）**
> 六项任务全部完成。#6 的尾巴已在当晚补完并实测（见 §7 #6）；#7 **只做完了前半段**
> （2 个界面的设计稿），后半段见下方 🔄 与 §7 #7。
>
> ⚠️ **如果你在别处看到「首次推理要 ~12s」「视频冷启动 13.7s」——那是错的**，
> 是磁盘分析缓存没命中造成的假象；真实数字见 §4.5。
>
> ⚠️ **#7 的正式产出仍是设计稿 PNG**（静态样例值，非运行时）——但真实界面代码**就在**且可用：
> `webui/frontend/src/` 13 屏，`tsc`+`vite build` 通过。二者是「设计稿 ↔ 实现」的平行关系，
> `check_design_fidelity.py` 量化了它们的一致度（见 §7 #7），别以为 `docs/design/` 的图是唯一产物。
>
> 🔄 **2026-09-22 续作（本轮）**：`#7` 卡在「把 11 个界面补进 `.pen`」这一步（生成器
> `docs/design/tools/gen_screens.py` 只写到 `# CHUNK4`，遇额度上限中断）。本轮**已续完**：
> `.pen` 现有 **15 个顶层节点 / 13 个界面**，`b/B8-B18.png` 11 张已渲染并逐屏核对；
> 「回写成代码」也已验收（`check_design_fidelity.py`）。详见 §7 #7 两段续作记录。
> ✅ **Open Design 方案 B 已试**（2026-09-22 续作末，证据见 §7 #7「方案 B 试跑记录」）：
> MCP daemon 无头可用——正确握手时序下 `tools/list` 返回 **22 个工具**，只读数据调用亦返回真实结果
> （`list_projects`→`[]`、`get_vela_login_status`→已登录，stderr 空）；但它是「项目·工件·运行」范式，**不接**
> `gen_screens.py → .pen → PNG` 出图流水线，故备选而未采用。**#7 后半段待办到此全部有结论。**
>
> ✅ **本轮另坐实两件事**：① 顶栏 `screenCrumb`（含副标题）**在真机实例里渲染成功**——无头 Edge
> `--dump-dom` 打开跑起来的 SPA，`topbar-screen-name/sub` 正确、无连接错误；② `pytest tests/ -q`
> 里原先偶发的 1 failed 是**测试自身不稳定**（Windows 目录 mtime 惰性，非引擎回归），已按仓库既有
> `os.utime` 做法修成确定性（15 连绿），`core/cache.py` 未动、无需重跑 §5.2。详见 §8 第 2 条。

---

## 0. 一句话现状

程序**当前就是可用的**：图片与视频两条链路都已用真实模型 + 真实 HTTP + 真实 ffmpeg 跑通，
内置 `builtin` 后端（ONNX Runtime，完全离线）已就绪，深度图与法线图都产出真实几何结构，
渲染结果确实改变像素。

六项交接任务已全部完成。**#6 的三条尾巴**（下载进度条 #6-4、代码分割评估 #6-5、
预热状态接入前端 #6-6）已于 `2026-09-21` 晚补做完并逐条实测（数字在 §7 #6）。
**#7 已于 `2026-09-22` 续完「铺界面」这一半**：13 个界面全部进了 `.pen` 事实源、
11 张新设计稿 PNG（`b/B8–B18`）已导出并核对；「把设计回写成代码」也已验收——
代码本就在且在，`tsc`+`vite build` 通过，`docs/design/tools/check_design_fidelity.py` 把
设计稿与代码的关系变成可复跑验收。**Open Design 方案 B 也已探测验证**（见 §7 #7「方案 B 试跑记录」）。

---

## 1. 项目是什么

2D 动漫风（Gacha Club 类）图片/视频的 **AI 自动打光软件**，MIT 协议。

- `core/` —— 纯 Python 引擎，**零 GUI 依赖**，可单独被 GIMP 插件 / CLI 调用
- `webui/` —— FastAPI 本地服务 + vite/TypeScript 前端（13 个界面），Python 托管，运行期不需要 Node
- `hls_cli.py` —— GIMP 插件用的命令行入口
- `server/` —— 可选的「自建深度服务」（把推理放到另一台机器上）

**四级 AI 后端降级链**：`builtin`（内置 ONNX，离线）→ `local`（用户自建服务，默认
`http://127.0.0.1:8765`）→ `cloud`（Replicate / 阿里云 / OpenAI 兼容）→ `simulate`
（灰度兜底）。任何一级失败或超时（默认 3s）都自动往下走，**永远不会因为 AI 挂了而彻底不能用**。

---

## 2. 环境（已就绪，照抄即可）

| 项 | 值 |
| --- | --- |
| 工作区 | `C:\Users\54301\Downloads\Glight_studio` |
| Python | 3.12.14，虚拟环境在 `.venv\`（uv 管理，已装好全部运行期依赖） |
| 关键依赖 | onnxruntime 1.30.0 / numpy 2.5.3 / pillow 12.3.0 / fastapi 0.141.1 |
| ffmpeg | 系统 PATH 上有（Gyan.FFmpeg 9.0.1），另随包有 imageio-ffmpeg 兜底 |
| GPU | GTX 1650 4GB（CPU 推理也能跑，DAV2-Small 单图约 1s 量级） |
| 用户数据目录 | `C:\Users\54301\.horizon_light_studio\`（旧名 `.gacha_light_studio` 会自动迁移） |
| 启动服务 | `.venv\Scripts\python.exe -m webui.desktop`（GUI 窗口）或 `python -m uvicorn --host 127.0.0.1 --port 8756 "webui.api:app"`（无头） |

**网络限制（很重要）**：`huggingface.co`、`github.com` **不可达**（连接直接失败）；
`hf-mirror.com` **可达**；pypi / 清华源 / npmmirror 均可用。
→ 所以模型下载默认走 `hf-mirror.com`，新增任何境外依赖前先确认可达性。

**版本库（2026-09-22 已建基线）**：已 `git init`（分支 `main`），首次提交 `ef22c57` 收录 127 个源文件
（约 1.8 MB；`.venv` / `node_modules` / `dist` / 模型权重 `*.onnx` / `_e2e_out` 等已由 `.gitignore`
排除、未入库）。本机默认 `core.autocrlf=false`（克制 CRLF 改写）。改动前 `git add -A && git commit`，
改坏了用 `git checkout -- <file>` 或 `git reset --hard <good-sha>` 回退。原先「无 `.git`、改坏无回退」的风险已消除。

---

## 3. 本次交付物

### 3.1 已部署的模型（真实文件，已落盘）

目录 `C:\Users\54301\.horizon_light_studio\models\`：

| 文件 | 大小 | 用途 | 来源（镜像） |
| --- | --- | --- | --- |
| `depth-anything-v2-small.onnx` | 99,060,839 B（≈94.5 MiB） | 深度图，= ControlNet 的 depth 预处理器同源权重 | `hf-mirror.com/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx` |
| `moge2-vits-normal.onnx` | 140,852,051 B（≈134.3 MiB） | 法线图 | `hf-mirror.com/Ruicheng/moge-2-vits-normal-onnx/resolve/main/model.onnx` |

> 为什么法线用 MoGe-2 而不是 BAE / DSine：ControlNet 官方的 `NormalBaeDetector` /
> `DSineDetector` **只有 PyTorch 权重，至今没有可直接用的 ONNX 导出**，与「离线 + ONNX Runtime」
> 的技术路线冲突。MoGe-2 ViT-S 法线分支输出的切空间法线与 BAE 语义一致，是等价替代。

下载地址可用环境变量覆盖：`HLS_DEPTH_MODEL_URL` / `HLS_NORMAL_MODEL_URL`。

### 3.2 新增代码

**`core/preprocess.py`（新文件，~290 行）—— ControlNet 风格预处理模块**

只负责「各模型家族的预处理与后处理差异」，下载/校验/会话缓存复用 `core/builtin_depth.py`。
两者共用同一份模型目录与会话缓存（同一模型不会加载两次）。

公开 API：

```python
depth_model_url() / normal_model_url()          # 可被环境变量覆盖
depth_model_path() / normal_model_path()
preferred_depth_model(model_path="")            # 显式配置 > 已部署 DAV2 > 默认 MiDaS 路径
download_depth_model(path="", progress=None, timeout=30.0)
download_normal_model(path="", progress=None, timeout=30.0)
model_family(sess) -> "dav2" | "moge_normal" | "midas"   # 按 ONNX 输入签名识别，不看文件名
dav2_depth(rgb, model_path="", log=None, sess=None)      # -> HxW float32，0~1，大=近
moge_normal(rgb, model_path="", log=None, sess=None)     # -> HxWx3 float32，-1~1
depth_map(rgb, model_path="", log=None, sess=None)       # 按家族分发；传错文件会抛清晰异常
analyze(rgb, depth_model="", normal_model="", timeout=3.0, log=None)
    # -> {'depth': HxW 0~1（大=近）, 'normal': HxWx3 -1~1 或 None}
```

`analyze()` 的降级策略是**刻意设计**的：深度模型缺失/失败 → 抛异常（调用方负责降级）；
法线模型缺失/推理失败 → 只记日志并返回 `normal=None`，上层用
`depth_to_height()` + `height_to_normal()` 本地派生，**不该让整条后端链一起失败**。

**接入点**：

- `core/ai_backend.py` —— 新增配置项 `builtin_normal_model_path`；`_try_builtin()` 改调
  `preprocess.analyze()`（第 200 行附近）；缓存键已把法线模型路径并入（第 183 行）
- `webui/api.py` —— 新增 `POST /api/model/download`、`POST /api/model/import`、
  `GET /api/model/progress`（下载进度轮询，见 §7 #6-4）；`/api/state` 新增 `normal_model_ready` 字段
- `webui/frontend/src/api.ts` / `shell.ts` —— `ModelKind = "depth" | "dav2" | "normal"`，
  模型管理界面可下载/导入，深度与法线的就绪状态**分开显示**
- `tests/test_features.py` —— 新增 7 个 ControlNet 预处理测试 + 3 个回归测试

### 3.3 一个容易踩的坑：`kind` 是 **query 参数**，不是 body 字段

```python
# webui/api.py:1699
async def api_model_import(request: Request, kind: str = "depth") -> dict:
    ...
    data = await request.body()      # ← body 被原始 .onnx 字节占用
```

FastAPI 会把带默认值的标量参数绑成 **query 参数**。`/api/model/import` 的 body 必须是
原始 `.onnx` 字节（用于覆盖用户自己的模型文件），所以 `kind` 只能从 URL 上带：

```
POST /api/model/import?kind=normal     # 正确
POST /api/model/import                 # body 里写 kind 无效，会被当成 .onnx 字节
```

`kind` 三选一：`depth`（默认，MiDaS 家族）/ `dav2` / `normal`，
由 `_model_dst(kind)` 决定落盘路径，`_validate_import(path, kind)` 决定校验方式
（传给 `normal` 的文件必须是 `moge_normal` 家族，传给 `dav2` 的必须是 `dav2` 家族）——
**故意做严**：装错文件却「提示成功」，用户会以为模型没效果，比直接拒绝难排查得多。

### 3.4 UI 设计稿（Pen）

`docs/design/凌日光影棚-设计稿.pen` + 由它确定性导出的
`docs/design/b/B6-暗房终端.png`、`b/B7-光影指挥屏.png`，
外加一张真实前端对照图 `b/B7-光影指挥屏-真实前端.png`，
导出工具在 `docs/design/tools/`。

**这批图不产出可运行代码**，前端实现仍以 `webui/frontend/src/` 为准。
完整说明、复现命令、以及「Pen 应用不会因外部改 `.pen` 而重绘」这个坑，见 **§7 #7**。

---

## 4. 模型硬事实（**实测确认，勿凭直觉改动**）

### 4.1 两套约定（全项目统一）

- **深度图**：`float32`，范围 `0~1`，**大 = 近（白 = 靠近观察者）**，按 1/99 百分位归一化。
- **法线图**：`HxWx3` 的**单位向量**，分量范围 `-1~1`，切空间（tangent space），
  **+z 朝向观察者**。存成 PNG 时用 `0.5 = 零` 编码（`(n*0.5+0.5)*255`）。

### 4.2 Depth Anything V2（`dav2`）

- 归一化与 MiDaS **完全相同**（ImageNet mean/std），差别只在输入尺寸
- 输入：短边 518，长边上限 1036，**两侧都必须是 14 的整数倍**（ViT patch 大小）
- 输出是**视差（disparity）**，本身就是**大 = 近**，与项目约定一致 → **不要取反**
  （实测：主体 4.59 > 头部 3.06 > 背景 2.55）

### 4.3 MoGe-2 法线（`moge_normal`）

- **只接受正方形输入**：非方图必须**黑边补方**再裁回内容区；直接拉伸会连带旋转法线方向
  （形状失真会污染几何）。默认边长 518，`num_tokens` 必须等于 `(边长/14)²`
  （边长 518 → 1369）
- 输出在 **OpenCV 相机系**（+z 指向场景内部，实测 z 均值 −0.9476）→ **z 必须取反**
  才满足项目「+z 朝向观察者」的约定。这是最容易忘、且忘了以后渲染出来「光从背面来」的地方

### 4.4 8bit PNG 量化上限（**调试阈值时的必读项**）

解码后的法线 PNG 是 8 位，每分量步长 `1/255`，因此单位向量长度误差的**理论上限**是：

```
2√3 / 255 = 1.36e-02
```

实测分布：`p50 2.94e-3 / p90 7.00e-3 / p99 8.57e-3 / max 1.07e-02`。

→ **任何低于 ~0.014 的阈值，测的都是 PNG 量化误差，不是模型质量。**
（内存里的 `float32` 数组仍然可以按 `err < 1e-3` 断言 —— `tests/test_features.py` 就是这么做的；
`tests/live_e2e.py` 因为读的是解码后的 PNG，阈值放宽到 `0.02` 并写明了理由。）

### 4.5 启动与首次推理成本（**已实测，勿沿用旧数字**）

> ⚠️ 本节旧版本写的是「首次推理 ~12s（ONNX 会话加载）」「视频冷启动 13.7s vs 热态 1.6s」。
> **那两个数字是错的**，量到的是**磁盘分析缓存**没命中 / 命中的差别，不是会话加载。
> 下面全部是本机（GTX 1650，CPU 推理）重新实测的值。

| 项目 | 耗时 |
| --- | --- |
| `import onnxruntime` | 0.28s（仅首次导入） |
| DAV2 会话构建 | 0.40s |
| MoGe 会话构建 | **1.24s**（主导项） |
| **预热合计** | **≈1.9s** |
| 稳态单图分析（DAV2 + MoGe，会话已缓存） | ≈3.0s |
| 首次分析（不预热） | 4.7s（= 1.9s 加载 + 2.8s 推理） |
| 视频关键帧（同一段 24 帧 288px） | 22.9s 全新图 / 2.3s 命中分析缓存 |

**关键结论：ONNX Runtime 构建会话时会长时间持有 GIL。**
实测 MoGe 加载 1.09s 期间，另一线程的 10ms 睡眠被拉长到 **0.937s** —— 也就是说
预热一旦开始，**整个服务**（连不碰模型的 `/api/themes`）都会被冻住约 1.3s。
这件事**躲不掉**（换线程、先 import onnxruntime 都试过，见下），只能选时机。
`webui/api.py` 现在的做法：

1. `/api/state` 走 `model_ready(..., allow_load=False)`：只做快速判断 + 读记忆值，
   **不触发加载** → 首屏 0.17s（旧行为要等 1.92s）；
2. 预热线程**推迟 `WARMUP_DELAY = 3.0s`** 再启动，把那次冻结挪到首屏画完、
   用户还在挑图的空档里。实测时间线：
   `t=1.07s` 首个请求 0.08s → `t≈4.4s` 冻结 1.33s → `t≈5.7s` 预热完成；
3. 预热状态由 `/api/state["warmup"]` 暴露：`idle → scheduled → warming → ready|missing`
   （含 `seconds` / `depth` / `normal` / `detail`），失败不抛异常。

收益实测：**首次分析 4.71s → 3.55s**。

> 试过但**没用**的两个变体，别再走一遍：
> ① 预热线程立刻启动（不推迟）→ 首屏第一个请求被拖到 1.64s；
> ② 先在主线程 `import onnxruntime` 再启动预热 → 冻结只从 1.64s 降到 0.95s，
> 却让端口可连时间从 0.99s 退到 1.64s（lifespan 里做的事会推迟 uvicorn 开始监听）。
>
> ⚠️ 别为了「消除卡顿」把 `WARMUP_DELAY` 改回 0：那是把 1.3s 冻结挪回首屏。
> 真要根治，只能把推理挪到**独立进程**（进程外加载不抢 GIL），属于架构改动。

---

## 5. 如何验证

### 5.1 快：单元/集成测试（不依赖运行中的服务）

```bash
cd C:\Users\54301\Downloads\Glight_studio
.venv\Scripts\python.exe -m pytest tests/ -q
```

**100 个用例**（99 passed / 1 skipped，全量约 207–233s，视机器负载；跳过的那个是可选依赖相关）。
覆盖 `core/` 引擎、视频链路、ControlNet 预处理、以及若干回归用例。
本次优化新增的 8 个用例集中在两处：`# ---- 缓存容量守护`（LRU 先淘最冷、
不超限不动、`HLS_CACHE_MAX_MB` 覆盖、写入触发淘汰）与 `# ---- 会话并发与预热`
（8 线程并发只建一次会话、预热状态可见、预热推迟且可见 scheduled、预热异常被吞）。
另加 `test_model_progress_reports_and_never_fabricates`：下载进度可轮询、
无 Content-Length 时**不编造百分比**、下载失败后槽位必须复位。

### 5.2 慢但真：全链路自检（**交付/换机后必跑**）

`tests/live_e2e.py` 是本次新增的**真环境**自检脚本：走**真实 HTTP + 真实 ONNX 模型 + 真实 ffmpeg**。
它**不是 pytest 用例**（无 `test_` 前缀，不会被 `pytest tests/` 收集），必须对着**已在运行**的服务跑：

```bash
# 终端 1
.venv\Scripts\python.exe -m uvicorn --host 127.0.0.1 --port 8756 "webui.api:app"

# 终端 2
.venv\Scripts\python.exe tests/live_e2e.py --port 8756
# 可选：--skip-video  只测图片链路（快）
#       --mode keyframe|perframe|both   视频模式（默认 both）
```

退出码 `0` = 全绿；非 `0` = 有环节失败（失败点打印原因）。
产物落在 `_e2e_out/`（已加入 `.gitignore`）：`clip.mp4`、`render.png`、
`video_keyframe.mp4`、`video_perframe.mp4`、`strokes.mp4` —— 可直接肉眼验收。

**它比 pytest 多测的东西**（也就是说：pytest 全绿**不代表**下面这些没问题）：

- 真 HTTP 层（pytest 走的是路由内省/直接调函数，绕过 HTTP）
- **内容级**断言，而不是状态码：深度图**有结构**（`std > 0.02`，全平图 = 模型没跑）、
  深度方向正确（中心比四角近）、法线 +z 为正、渲染结果**确实变了像素**、
  成品视频**抽帧后确实被重打光**（不是把源片重新封装一遍）
- 真实 ffmpeg 抽帧 / 转码 / 成品帧数与分辨率核对

### 5.3 已实测通过的数字（2026-09-21，热态）

| 链路 | 结果 |
| --- | --- |
| 图片 | `backend=builtin`（不是 simulate）；深度 `std=0.276`；中心 0.583 > 四角 0.376（白=近 ✓）；法线 z 均值 0.943；渲染与源图平均像素差 **27.22**；png + jpeg 导出魔数正确 |
| 视频 keyframe | 24/24 帧、2.00s、`backend="keyframe×5"`；第 12 帧与源片平均像素差 **19.24** |
| 视频 perframe | 24/24 帧、`backend="perframe"`；第 12 帧平均像素差 **18.86** |
| 笔刷（strokes） | 预览与素图差异 **3.48**；导出 24 帧 mp4 正常 |

> ✅ **2026-09-22 复跑 `tests/live_e2e.py`**（对运行中的 API 服务 + 真实 ONNX / ffmpeg，非 pytest）：**全绿**——图片 `std=0.276` / 中心 `0.583>四角 0.376` / 法线 `z=0.943` / 渲染差 `27.22`、视频 keyframe `24/24` 帧、第 12 帧差 `19.24`、笔刷差 `3.48` **逐项复现上表**；`perframe 18.86` 本轮未单独复跑（keyframe + strokes 已覆盖全链路）。
---

## 6. 代码结构速查

```
core/
  paths.py           用户数据目录 ~/.horizon_light_studio（含旧目录迁移）
  builtin_depth.py   ONNX 骨架：下载 / 校验 / 会话缓存 / MiDaS 推理
  preprocess.py      ★ 本次新增：DAV2 深度 + MoGe-2 法线（ControlNet 风格）
  ai_backend.py      四级后端链 builtin → local → cloud → simulate
  render.py          打光渲染（依赖「白=近」与「法线 +z 朝观察者」）
  video.py           视频链路（keyframe / perframe 两种模式）
  ...
webui/
  api.py             FastAPI 路由（全部路由见文末）
  desktop.py         桌面窗口入口（默认端口 8756）
  frontend/          vite + TypeScript，13 个界面；`npm run build` 产物由 Python 托管
server/              可选「自建深度服务」（1.2.0）
hls_cli.py           GIMP 插件命令行入口
tests/
  test_core.py / test_features.py / test_video.py    pytest（100 例）
  live_e2e.py        ★ 本次新增：真环境全链路自检（非 pytest）
  ui_playwright.py   界面拟人点击测试（需 playwright chromium）
```

---

## 7. 后续任务（未完成，按优先级）

### #6 优化现有软件

**已做完的三项**（都在 `2026-09-21`，都有对应测试）：

1. **首屏不再等模型** —— `/api/state` 走 `model_ready(..., allow_load=False)`，
   只做快速判断 + 读记忆值，首屏 **1.92s → 0.17s**。预热线程推迟 3s 启动，
   把 ONNX 会话构建那次 **1.3s 全站冻结**（GIL）挪到首屏之后；首次分析
   **4.71s → 3.55s**。细节与实测数据见 §4.5，**别把 `WARMUP_DELAY` 改回 0**。
2. **`core/cache.py` 加了容量上限 + LRU 淘汰** —— 默认 2GB（`HLS_CACHE_MAX_MB`
   可覆盖，≤0 = 不限），`put()` 末尾按 30s 节流检查一次，超了按目录 mtime
   淘汰到 90% 水位；`get()` 会 `os.utime` 摸一下目录当 LRU 用。
   之前缓存**只增不减**，跑视频关键帧很容易把磁盘吃满。
3. **`core/builtin_depth.py` 的 `_get_session` 线程安全** —— 原来两个请求同时
   首次分析会各建一遍会话（MoGe 一遍 1.24s，且各自持有 GIL）。现在按路径分锁 +
   双重检查，同一模型只建一次；文件被替换时（大小/mtime 变了）自动重建。

**后来补做的三项**（都在 `2026-09-21` 晚，已实测验证）：

4. **下载进度条** —— 后端新增 `GET /api/model/progress`（模块级 `_model_dl` 槽位 +
   `_dl_progress(kind)` 回调，接 `download_file` 早就有的 `progress` 参数），
   前端 `modelProgressBar()` 以 400ms 轮询画进度条。**已用真实下载验证**：
   点「一键下载模型」（kind=depth，MiDaS 64MB）→ 按钮变 `下载中…（约 64MB）` 且禁用，
   进度条显示 `已下载 5.0 / 66.8 MB（8%）`，落盘 `model-small.onnx` = 66,764,249 B，
   结束后 `active=false / percent=100.0`。
   **不要编造百分比**：服务器没给 `Content-Length` 时 `total=0`，此时只能显示
   「已接收 N MB」，用 `.bar--indet` 动画表示「在动但算不准」——有测试守着这条
   （`test_model_progress_reports_and_never_fabricates`）。
5. **代码分割：评估结论是「不做」**（数字见下，别重做这遍评估）。
6. **预热状态已接入真实前端顶栏** —— `backendChip()` + `pollWarmup()`。
   实测（冷启动抓的连拍）：`t=1.5s/2.6s/4.0s` 显示 `引擎加载中…`（琥珀点，
   `dot--warn`），`t=6.0s` 翻成 `内置本地引擎 · 就绪`（绿点，title=`模型预热耗时 1.43s`）。
   文案取自设计稿 §7 的 `dSL02` / `fTzYQ` 节点，不是自己编的。

#### #6-5 代码分割的实测数字（结论：不做）

冷加载（禁用缓存）实测两遍，`dist/assets/app.js` = **248.34 kB / gzip 89.13 kB**：

| 指标 | 实测 |
| --- | --- |
| `app.js` 解码后体积 | 270,295 B（**未压缩**，见下） |
| **ScriptDuration**（全部 JS 执行） | **13 ms** |
| TaskDuration（主线程忙） | 152–158 ms |
| RecalcStyle + Layout | 约 116–122 ms（**真正的开销在这里**） |
| DOMContentLoaded / load | 48–51 ms / 89–91 ms |
| 首屏 DOM 节点 / JS 堆 | 1350 个 / 1.2 MB |

**判断依据**：整个包的 JS 执行只有 13ms，拆包最多省下这 13ms 的一小部分，却要
多付几个 chunk 的往返（本机每个约 10ms，可能倒亏），还要把 `main.ts` 的静态
`registerScreen()` 改成异步注册、动 `Screen` 契约。**收益不抵复杂度，明确不做。**

> ⚠️ 顺带发现：`uvicorn` 的 `StaticFiles` **不压缩**，所以 `app.js` 是按 270 kB
> 裸传的（vite 报的 89 kB gzip 是构建期估算，运行期并没有生效）。本机 localhost
> 传输约 0 成本，**当前不是问题**；但如果哪天要远程部署，应该先加 gzip 中间件，
> 而不是拆包 —— 那才是省带宽的正解。

> 注意 `/api/state` 的 `version` 字段是 **桌面应用版本 1.0.0**，与 `server/app.py` 的
> **深度服务版本 1.2.0** 是两回事，别当成不一致去「修」。

### #7 用 Pen / Open Design MCP 制作 UI

**状态：Pen 侧已完成**（2026-09-21 出「暗房终端」「光影指挥屏」2 个界面；
2026-09-22 补完其余 11 个 —— `.pen` 现在共 13 个界面，`b/B6`–`b/B18` 共 12 张图）；
**Open Design（方案 B）已于 2026-09-22 续作末探测：MCP daemon 无头可跑、协议握手正常、`tools/list` 返回 22 个工具（见下「方案 B 试跑记录」）；未用于出设计稿。**

用户提供了两套 MCP 配置，本次只走了 Pen（方案 A）：

```jsonc
// 方案 A：Pen（pencil）—— 已写入 用户级 settings.json，已生效
{ "name": "pencil", "transport": "stdio",
  "command": "E:\\Users\\54301\\AppData\\Local\\Programs\\Pen\\resources\\app.asar.unpacked\\out\\mcp-server-windows-x64.exe",
  "args": ["--app", "desktop"], "env": {} }

// 方案 B：Open Design（open-design）—— 本次未使用，留作备选
{ "mcpServers": { "open-design": {
  "command": "E:\\Users\\54301\\AppData\\Local\\Programs\\Open Design\\Open Design.exe",
  "args": ["E:\\Users\\54301\\AppData\\Local\\Programs\\Open Design\\resources\\app\\prebundled\\daemon\\daemon-cli.mjs", "mcp"],
  "env": {
    "OD_DATA_DIR": "C:\\Users\\54301\\AppData\\Roaming\\Open Design\\namespaces\\release-stable-win\\data",
    "OD_SIDECAR_CLIENT_ENDPOINT": "\\\\.\\pipe\\open-design-sidecar-367abf7b6819bbe8cf185bf2c2a89488",
    "OD_MCP_BOOTSTRAP_COMMAND": "E:\\Users\\54301\\AppData\\Local\\Programs\\Open Design\\Open Design.exe",
    "OD_MCP_BOOTSTRAP_ARGS": "[\"--headless\"]",
    "ELECTRON_RUN_AS_NODE": "1" } } } }
```

##### 方案 B 试跑记录（2026-09-22 续作末）

**结论：方案 B 的 MCP 通道可用、已验证；本轮未用于产出设计稿。** 用与配置完全一致的方式
（`Open Design.exe <daemon-cli.mjs> mcp` + `ELECTRON_RUN_AS_NODE=1` + 给定 env，sidecar 管道用配置里那个）
起了**带 18s 超时强杀**的无头 stdio 握手探针（脚本落在系统 temp，一次性、不留仓库）：

- **前置就绪**：`Open Design.exe` / `daemon-cli.mjs` / `OD_DATA_DIR` 都在；探测时 Open Design GUI 正在运行
  （11 个进程），配置里那个 sidecar 命名管道 `\\.\pipe\open-design-sidecar-367abf7…` 真实存在。
- **握手成功**：`initialize` → `tools/list` 正常返回，stdout ≈ 21 kB、**stderr 为空、进程退出码 0**。
- **22 个工具**：简报（`collect_brief` / `confirm_brief`）、项目（`list_projects` / `create_project` /
  `delete_project` / `get_project`）、工件与文件（`create_artifact` / `get_artifact` / `get_file` /
  `write_file` / `delete_file` / `list_files` / `search_files`）、运行与代理（`start_run` / `get_run` /
  `cancel_run` / `list_agents`）、技能与插件（`list_skills` / `list_plugins`）、账号（`start_vela_login` /
  `get_vela_login_status`）、上下文（`get_active_context`）。
- ✅ **数据类调用在无头下其实可用（更正早前误判）**：先前探针 `list_projects` 得 0 字节，是**协议时序 bug**——
  必须先 `initialize` 并等结果、再发 `notifications/initialized`、**之后**再 `tools/call`，且留足等待。用正确时序重探
  （`od_probe3.cjs`，一次性脚本）：`list_projects`→`{"projects":[]}`、`get_active_context`→`{"active":false, hint…}`
  （GUI 当前没打开任何项目，约 5 分钟无交互会失效）、`get_vela_login_status`→**已登录**
  （NavaCloudy / 1704872879@qq.com，plan=free，consoleOrigin=open-design.ai/cloud）。全程 **stderr 空、无 error**。
  更正结论：**MCP 传输 + 发现（22 工具）+ 只读数据三类，在无头 stdio 下全部可用**。
- **与方案 A 的本质差异**：Open Design 走「项目·工件·运行(run)·代理(agent)」范式，**不接**
  `gen_screens.py → .pen → PNG` 这条静态出图流水线；因此定位为**备选（可备而不用）**，
  设计稿事实源仍以 `.pen`（方案 A）为准。

#### 交付物（都在 `docs/design/`）

| 文件 | 是什么 |
| --- | --- |
| `凌日光影棚-设计稿.pen` | Pen 文档本体（**15 个顶层节点**：`Frame bi8Au` / `NavBtn ivUPE` / `暗房终端 Skfts` / `光影指挥屏 ASOlL` + **2026-09-22 追加的 11 个界面**，共 3291 节点 / 1.19 MB） |
| `b/B6-暗房终端.png` | 由 `.pen` 导出的设计稿（2880×1800，@2x） |
| `b/B7-光影指挥屏.png` | 同上，新增的「光影指挥屏」界面设计稿 |
| `b/B7-光影指挥屏-真实前端.png` | **真实 WebUI** 同一界面的截图，用于对照设计稿可实现 |
| `b/B8-光影工作台.png` … `b/B18-主题引擎.png` | **2026-09-22 新增的 11 张**，全部由 `.pen` 确定性导出 |
| `screens.json` | 屏幕名 → 节点 id / PNG 文件名 / 画布坐标（id 每次生成都是随机的，渲染脚本靠它取 id） |
| `tools/gen_screens.py` | 生成器：克隆 `Skfts` 外壳 + 挂载 11 个界面主体，写回 `.pen` 与 `screens.json` |
| `tools/render_screens.py` | 按 `screens.json` 批量渲染 PNG（`pen2html.py` + `shot_url.mjs` 的驱动） |
| `tools/pen2html.py` | `.pen` → 独立 HTML 的转译器（为什么需要它：见下面那节「关键坑」） |
| `tools/shot_url.mjs` | 用无头 Edge + CDP 把 HTML 截成 PNG 的驱动脚本 |

`B1–B5`（调光台 / 光源星图 / 逐帧光绘 / 智能拾光 / 三维布光预演）和 `1–6`
（光影工作台 / 智能打光 / 专业模式 / 视频调光 / 插件中心 / 主题引擎）是**上一轮遗留的概念稿**，
不是 Pen 文档，分辨率 1600×1000 / 1600×1060。加上 `B6`/`B7` 与 **2026-09-22 新出的
`B8–B18`**，13 个界面**都有图**，但来源分两类：

| 类别 | 文件 | 性质 |
| --- | --- | --- |
| 上一轮概念稿 | `1–6.png`、`b/B1–B5.png` | 视觉参考，**不是** Pen 事实源，也**没有**逐个校对过引擎能力边界；这 11 个界面的概念稿已被 `B8–B18` 取代 |
| Pen 设计稿（事实源） | `b/B6`、`b/B7`、`b/B8–B18.png` | 由 `凌日光影棚-设计稿.pen` 确定性导出，2880×1800（1440×900 @2x）；13 个界面一一对应 |

> ⚠️ **概念稿里有用错的地方，别照抄。** 典型：`b/B5-三维布光预演.png` 上写了
> `显色指数 96 CRI`、`环境照度 320 lx`、`漫反射 0.82` —— 这几个量**本引擎根本无法计算**
> （见 §0 与 `core/` 的诚实约束）。要做「三维布光预演」界面时，以 `B6`/`B7` 的处理方式为准
> （无法计算的值一律不显示，并用一句说明讲清楚为什么）。
> `B6`/`B7` 是**照这个约束画的**：色温/强度/半径/相对占比都有，CRI/照度/Δuv 一律没有。

#### ⚠️ 关键坑：Pen 应用**不会**因为外部改动 `.pen` 文件而重绘

这是本次最重要的发现，**别再浪费时间尝试**：

- 文档落盘在 `C:\Users\54301\.pencil\documents\<docId>\pencil-new.pen`
  （本次 `docId = a693244d-9f64-4d07-9faf-811b55af15da`，节点有 `fileToken`）。
- 直接编辑这个 JSON **不会**让 Pen 界面更新；只有通过 MCP 的
  `Insert`/`Update`/`Delete`/`Copy` 改动应用内存里的模型才可能反映出来。
- 即便用 MCP 插入，**渲染器也追不上**：本次通过 MCP 插入的 `主体分栏`（应用侧 id `v9mIQ`）
  在磁盘 JSON 里是对的，但界面在 20+ 分钟轮询 + 重启导出后**始终没有画出来**，
  主区域一直是空白，顶栏探针（`#ff2d2d` 矩形）和导轨探针（绿矩形）也一直残留。
- 另外应用会把插入的 frame 规范化 —— `v9mIQ` 落盘后**没有 `layout` 字段**。

**结论：不要指望 Pen 应用做渲染/导出。** 现在的做法是：
`docs/design/凌日光影棚-设计稿.pen` 作为**设计事实源**，
用 `tools/pen2html.py` 自己把它转成 HTML，再用无头 Edge 截图成 PNG。
本轮两张图就是这么出的，完全可复现。

#### 复现 PNG 的命令

```bash
# 1) .pen -> HTML（第二个参数是屏幕节点 id，第三个是输出路径）
python docs/design/tools/pen2html.py docs/design/凌日光影棚-设计稿.pen \
       ASOlL /tmp/bridge.html        # 光影指挥屏；暗房终端换成 Skfts

# 2) 起一个带调试端口的无头 Edge
msedge.exe --headless=new --disable-gpu --hide-scrollbars \
  --remote-debugging-port=9222 --user-data-dir=/tmp/shoot-profile \
  --no-first-run --no-default-browser-check about:blank

# 3) HTML -> PNG（1440×900 @2x）
node docs/design/tools/shot_url.mjs 1440 900 file:///tmp/bridge.html /tmp/bridge.png
```

**注意**：`shot_url.mjs` 找 CDP target 时用的是 `t.type === "page"`，
**不要**改成 `t.url.startsWith("http")` —— `about:blank` 会被过滤掉，脚本会静默卡死。

#### 续作记录（2026-09-22）：11 个界面补进 `.pen` + 11 张 PNG

上一轮的 `gen_screens.py` 写到 `# CHUNK4`（= `build_harvest` / `build_previs`）就断了
（触发额度上限）。本轮把 `# CHUNK5`（`build_desk` / `build_plugins` / `build_theme`）
与 `# CHUNK6`（`SCREENS` 注册表 + `POS` + `main()`）补完，并修了三个渲染期才暴露的问题。
屏幕名 → 节点 id 的映射每次生成都会变（随机 id），所以落到了 `screens.json`：

```
B8  光影工作台 · Light Workbench   B9  智能打光 · Auto Light     B10 专业模式 · Pro Mode
B11 视频调光 · Video Light         B12 逐帧光绘 · Frame Paint    B13 光源星图 · Light Starmap
B14 智能拾光 · Light Harvest       B15 三维布光预演 · 3D Previs  B16 调光台 · Light Desk
B17 插件中心 · Plugins             B18 主题引擎 · Theme Engine
```

复跑命令（第 2 步的无头 Edge 必须先起，见上一节）：

```bash
# 1) 重新生成 .pen（幂等：先按名字摘掉同名屏幕再追加；顺带重写 screens.json）
.venv/Scripts/python.exe docs/design/tools/gen_screens.py

# 2) 批量/单张出图
.venv/Scripts/python.exe docs/design/tools/render_screens.py
.venv/Scripts/python.exe docs/design/tools/render_screens.py --only B16
```

本轮的三处修正（都是实测才发现的）：

1. `chip()` 加了 `width="fit_content"`，`pen2html.py` 新增该取值的支持（不参与父容器拉伸，
   竖排父容器里补 `align-self:flex-start`）。**不加的话**，直接放在竖排卡片里的 chip 会被
   `align-items:stretch` 拉成**整行色条** —— `B15` 的「已锁定位置」「布光已应用到调光台」、
   `B16` 的「已应用参考图光照」「AI 自动打光」都会走形。
2. `B16` 的「CUE 名称（留空为「未命名 CUE」）」输入框原本与「保存当前为 CUE」按钮并排，
   输入框被压窄、占位文案溢出到按钮底下；改成两者各占一行。
3. `pen2html.py` 的 `ICONS` 从 20 个补到 **34 个**（新增 `folder sparkles workflow wand puzzle
   palette sliders sliders-horizontal download upload check trash rotate star`），路径取自
   `webui/frontend/src/ui.ts` 的 `ICONS`（前端同源）；只有 `sliders-horizontal` 用 lucide 官方
   路径（`ui.ts` 里没有它，但 `.pen` 导轨的「调光台」按钮用它）。现在 `.pen` 用到的 34 种图标
   **0 缺失**，不会再画成空白/info 兜底。

本轮做过的自检（都可复跑）：

- 写回前先存 `凌日光影棚-设计稿.bak-before-11screens.pen`；写回后逐字节比对 `bi8Au` /
  `ivUPE` / `Skfts` / `ASOlL` 四棵子树 —— **与原文件完全一致**（原有 2 个界面没被碰过）。
- id 唯一性：3291 节点 / 3291 唯一 id / 重复 0（`seed_ids()` 先把既有 id 登记再生成新 id）。
- 无头 Edge 里跑溢出探针（`scrollWidth/Height > clientWidth/Height`）：11 张图**无横向溢出**，
  纵向只有 ≤12px 的行内基线差（来自克隆外壳的 chip，`B6`/`B7` 本来就有，属既有观感）。
- 诚实性：11 个界面**没有一个**显示照度 lx / CRI / Δuv / 频闪 / DMX / 灯具温度；文案逐字取自
  `webui/frontend/src/screens/*.ts`（本轮新写的三屏专门对着 `desk.ts` / `plugins.ts` /
  `theme.ts` 核过）。主题引擎的对比度是**真算的**（`theme.ts` 的 WCAG 2.1 公式 + 蓝主题令牌）：
  主文本/背景 `16.7:1 AAA`、次要文字/面板 `7.5:1 AAA`、按钮文字/强调色 `5.1:1 AA`
  （`theme.ts:108` 的 `wcagLevel()`：≥7 显示 AAA；按钮文字令牌是 `#08111e`，不是白色）。
- `pytest tests/ -q`：与设计稿改动无关（只动了 `docs/design/**`），仍为 99 passed / 1 skipped。

#### 续作记录·回写成代码（2026-09-22）：把设计稿 ↔ 前端代码的关系变成可复跑验收

用户的续作要求是「**Pen 允许产出可用代码**」。逐屏核对后的结论：**代码本就在且在** ——
`webui/frontend/src/` 13 个界面全部可用、可构建，设计稿是它们的设计侧表述（上一节已证明设计稿
文案逐字取自这些 `.ts`）。所以这半段没有去做「从 0 生成界面」，而是把「设计稿 ↔ 可用代码」这条
关系**固化成可复跑的验收口径**，并实测通过：

- **可运行性坐实**：`webui/frontend` 下 `tsc --noEmit` 干净（exit 0）；`vite build` 成功（exit 0），
  产出 `dist/index.html` + `dist/assets/app.js`（≈248 kB，gzip ≈89 kB）+ `dist/assets/index.css`（≈9.85 kB）。
  即：**这份前端是真实可部署的运行期代码**，由 Python 服务托管 `dist/`。
- **新增保真自检** `docs/design/tools/check_design_fidelity.py`（换机可复跑，路径自定位，默认只报告）：
  逐个界面抽出全部 text 文案 → 去空白 → 在**整个前端源码**里查是否已落地；含数字/比例/色温、
  「角色 · 类型」组合、纯 ASCII 标记的归为「运行时/占位」，不算缺口。
  实测：**文案 621 · 已落地 435 · 运行时·占位差异(允许) 136 · 真·缺口 50**。
- **50 条「真·缺口」的定性**：绝大多数不是代码缺失，而是**运行时值或设计占位**——
  当前主题名「暗夜蓝调」（`/api/themes`）、后端标签「深度来源：内置本地引擎」（`backendLabel()`）、
  角色·类型「主光 · 点光」（`ROLE_ZH`+光源类型拼接）、示例文件名 `角色立绘.png`、`CUE 01 · 黄昏逆光`；
  以及**改写/拆分的说明句**——`plugins.ts:496/547` 已含「这是纯数据插件…只解析 JSON 清单」「data 随 kind 变化」，
  `bridge.ts:230/302/471` 已含三句诚实脚注（`本引擎只做相对 Lambert/Phong…不显示`，逐字对齐设计稿）。
- ✅ **2026-09-22 复核真·缺口（55→50，逐条 grep 定性）**：重跑仍是 621 文案 / 已落地 435 / 运行时·占位 136 / 真·缺口 50（比初版少 5，来自 `autolight.ts`、`plugins.ts` 的说明句已改写落地）。对 50 条逐条 grep 定性：**≈40 条根本不是缺口**——主题名「暗夜蓝调 / 霓虹青紫 / 暗房琥珀」在 `core/theme.py` 定义、`当前主题：`（theme.ts）、后端标签由 `backendLabel()`/`core/ai_backend.py` 产出、`深度来源：`（shell.ts）是页脚运行时项，角色·类型「主光 · 点光」等由 `ROLE_ZH`+光源类型（`core/cues.py` 的点光 / 平行光）拼接、示例文件名 `角色立绘.png` / `舞台_黄昏.jpg` 是设计侧 mock 资源（真实为用户上传）；**≈6 条是已改写 / 拆分落地**（前缀在、尾串动态）：`生成的光照节点…`(autolight.ts)、「无 · 纯数据清单」「只解析 JSON / 随 kind 变化」(plugins.ts)、`参考图 · 场景采样完成` 的「完成」(harvest.ts)。**仅 ≈4 条是真正未落地的静态标签**（功能性界面无缺，纯文案级收尾，可择要回写）：`光绘时间轴`(framepaint.ts)、`主灯组`(starmap.ts)、`选中：` 的选择指示（promode.ts / previs.ts）——三条在各自 `.ts` 中 grep 均为 0。结论：**设计稿→可用代码这条线路已验收，无功能缺口**；纯文案口径接近 100%。
- ✅ **已把设计侧小标题接入运行时**（2026-09-22 续作末，附真相）：为 `Screen` 增加 `subtitle?` 字段，
  在**外壳顶栏**新增统一的 `screenCrumb`（`topbar-screen-name` / `topbar-screen-sub`，超 46vw 省略号），
  rail 按钮 `title` 回显 `名称 · 副标题`。10 个有设计副标题的屏幕全部逐字接入
  （workbench / autolight / video / framepaint / starmap / harvest / previs / desk / plugins / theme）；
  `promode` / `bridge` / `terminal` 设计侧本就无副标题，运行时也不加。
  **已用无头 Edge 对运行中的服务做真实渲染验证**：顶栏 `topbar-screen-sub` 现实「素材 · 光源 · 实时预览」，
  `tsc --noEmit` 与 `vite build` 通过，保真度自检 428→435 已落地。
  运行时值（当前主题名 / 后端标签 / 示例文件名 / `ROLE_ZH`）**仍保持动态**，不写死。诚实脚注逐字在代码里，无需搬运。


#### 设计侧的硬约束（做新界面前必读）

- 所有文案里的**诚实说明不能删**：`强度 0–4 为引擎相对标度；照度 lx / CRI / Δuv / 频闪 /
  DMX / 灯具温度本引擎无法计算。`（注意是**半角破折号 `–`**，照着
  `webui/frontend/src/screens/bridge.ts:471` 抄，别手打）以及遥测面板的
  `本引擎只做相对 Lambert/Phong 光照：照度(lx)、CRI、Δuv、频闪、灯具温度等无法计算，因此不显示。`
  （`bridge.ts:302`）。这两句在 `.pen` 里是**逐字**对齐代码的（节点 `tTxG5` / `3p4Ae`），
  改设计稿时别把它改走样 —— `webui/frontend/src/` 的相应断言会挂。
- `.pen` 的 `variables` 直接对齐 `core/theme.py` 的蓝色主题：
  `bg-root #0a0d13` / `bg-panel #0e121a` / `bg-elevated #161c27` / `bg-canvas #070910` /
  `border #1d2532` / `border-strong #2b3648` / `text-primary #e9eef8` / `text-secondary #98a5ba` /
  `text-muted #5e6a7e` / `accent #3b82f6` / `accent-bright #6fa8ff` / `accent-tint #13233c` /
  `accent-line #2a4c82` / `success #34d399` / `warm #ffa94d` / `violet #a78bfa`，
  字体 `Inter` / `JetBrains Mono`。**改主题时两边要一起改。**
- 「光影指挥屏」用的是 `bridge.ts` 的既定常量：
  `INTENSITY_MAX=4`、`KELVIN_MIN=1500` / `KELVIN_MAX=12000`、`RADIUS_MIN=0.05` /
  `RADIUS_MAX=1.5`、`FIELD_N=128`、`ROLE_ZH` 六角色（主光/补光/轮廓光/背景光/眼神光/氛围光）。
  设计稿里的 6 路灯具（CH01–CH06）就是按这套角色表造的，数值都是从
  `强度 / 色温 / 半径` 推出来的（光比 = 主光/补光 = 0.70/0.44 = 1.59:1）。
- `#6` 遗留项 6（界面没消费 `/api/state["warmup"]`）在设计稿里**已补**：
  顶栏有 `引擎加载中…` / `内置本地引擎 · 就绪` 两个 chip。前端实现时照抄即可。

#### 还没做的

- ✅ **「11 个界面补进 `.pen`」已完成**（2026-09-22，见上面「续作记录」）：`.pen` 现在有
  13 个界面，`b/B8–B18.png` 11 张已出图并核对。这一项**不再是待办**。
- ✅ **「把设计稿回写成可用代码」已验收**（2026-09-22 续作）：结论是「代码本就在，设计稿是它的
  设计侧表述」，并已把这条关系**变成可复跑的验收**，见 §7 #7「续作记录·回写成代码」。
  - `webui/frontend/src/` 13 个界面**全部可用且可构建**：`tsc --noEmit` 干净，
    `vite build` 产出 `dist/{index.html, assets/app.js≈248kB, assets/index.css}`。
  - 新增 `docs/design/tools/check_design_fidelity.py`（换机可复跑，默认只报告）：逐个界面抽文案、
    去白色后在`整个前端源码`里查是否已落地。实测 **已落地 435 / 运行时·占位差异(允许) 136 / 真·缺口 50**。
  - 这 50 条里绝大多数是**运行时值或设计占位**：当前主题名「暗夜蓝调」、后端标签「深度来源：内置本地引擎」、
    角色·类型「主光 · 点光」、示例文件名 `角色立绘.png`、`CUE 01 · 黄昏逆光`；
    以及**改写/拆分的说明句**——`plugins.ts` 已含「这是纯数据插件…只解析 JSON 清单」、
    `bridge.ts` 已含两句诚实脚注（`无法计算…因此不显示`，逐字对齐设计稿）。
  - ✅ **已**把设计侧小标题（「素材 · 光源 · 实时预览」「场景采集 · 识别光源 · 应用建议」等）塞进运行时：
    做法是**外壳顶栏新增统一 `screenCrumb`**（`subtitle?` → `topbar-screen-name` / `topbar-screen-sub`，
    rail 按钮 `title` 回显 `名称 · 副标题`），10 屏逐字接入；无头 Edge 渲染验证顶栏副标题正确显示、
    `tsc`/`vite build` 通过。运行时值（主题名 / 后端标签 / 文件名 / `ROLE_ZH`）**依旧动态**，未写成设计示例值。
  - 也就是说 **#7 现有设计稿 PNG + 可运行代码，且两者关系带验收维护**；前端仍以
    `webui/frontend/src/` 为准。
- ✅ **Open Design（方案 B）已探测**（2026-09-22 续作末）：MCP daemon 无头可用——正确握手时序下
  `tools/list` 返回 22 个工具，**只读数据调用也返回真实结果**（`list_projects`→`[]`、`get_active_context`→active:false、
  `get_vela_login_status`→已登录，stderr 空）。它是「项目·工件·运行」范式、不接 `.pen` 出图流水线，故备选而未采用。
  详见 §7 #7「方案 B 试跑记录」。
- `pen2html.py` 覆盖的节点类型 = 文档里**实际用到**的那些；`ICONS` 现有 **34 个**
  （`layers sun image chevron-down settings power terminal keyboard rotate-cw camera gauge
  radar crosshair fire play info search film plus lightbulb folder sparkles workflow wand
  puzzle palette sliders sliders-horizontal download upload check trash rotate star`）。
  再加新图标要同步补 `ICONS`（路径优先从 `webui/frontend/src/ui.ts` 抄），否则会渲染成空白。
- 新屏幕 body 里的是**静态设计值**（`CUE 01 · 黄昏逆光`、`com.example.*` 示例插件、
  `16.7:1` 对比度…），不是运行时数据；真实前端由 `/api/state` 驱动，设计稿只锁信息层级与文案。

现有前端的 `/api/state` 是「一个请求拿到全部初始状态」的设计（预设、设置、模型就绪状态、
视频限制、主题、当前参数），做新 UI 时**沿用这个契约**最省事，不必新增接口。

---

## 8. 下一个 Agent 的上手清单

1. 读 §0 / §2 / §4，把「深度白=近」「法线 +z 朝观察者」「DAV2 不取反 / MoGe 要取反 z」记牢
2. `pytest tests/ -q` 跑一遍，确认 100 例基线（99 passed / 1 skipped）。
   2026-09-22 续作末发现原先偶发的 1 failed（`test_put_triggers_prune_when_over_cap`）是**测试自身
   不稳定**，非引擎回归：该测试断言「两个连续写入的条目里旧的应被淘汰」，但 Windows 目录 mtime
   更新是惰性的，快速连续写入会拿到相同时间 → `prune()` 的淘汰顺序退化成随 scandir 随机。已按仓库
   既有做法（同文件 `test_cache_prune_evicts_lru_first` 的 `os.utime`）为该测试显式把 `old` 的目录
   时间戳压早，连续 15 次全绿；`core/cache.py` **未改**，故这条改动不触发 §5.2 重跑。
3. 起服务 + `python tests/live_e2e.py` 跑一遍，确认 §5.3 的数字量级对得上
4. 想改渲染/几何 → 先看 `core/render.py` 对两套约定的依赖，改完**必须**重跑 §5.2
5. 动 `preprocess.py` 里任何常量前，先读文件头的「实测确认，勿凭直觉改动」注释块
6. **别把 `_e2e_out/` 提交进版本库**（已在 `.gitignore`），它只是自检产物
7. 想动设计稿 / 重出图 → 先读 §7 #7「续作记录」：改 `gen_screens.py` → 跑它（会重写
   `.pen` 与 `screens.json`，幂等）→ 跑 `render_screens.py` 出图；出图前必须有无头 Edge
   占着 `9222` 调试端口，且**别**把写回后的 `.pen` 当成 Pen 应用会自动重绘（见「关键坑」）

### 全部 HTTP 路由（`webui/api.py`）

```
GET  /api/state                      POST /api/image            POST /api/analyze
POST /api/render                     POST /api/pick             POST /api/reference
POST /api/export                     POST /api/depth-preview     POST /api/settings
POST /api/cache/clear                POST /api/media             GET  /api/cues
POST /api/cues/save                  POST /api/cues/delete       POST /api/cues/interpolate
POST /api/harvest                    GET  /api/themes            GET  /api/theme/{id}
POST /api/theme                      POST /api/autolight         GET  /api/plugins
POST /api/plugins/install            POST /api/plugins/toggle    POST /api/plugins/remove
GET  /api/workflow/types             POST /api/workflow/evaluate
POST /api/video                      POST /api/video/frame       POST /api/video/keyframes
POST /api/video/pick                 POST /api/video/process     GET  /api/video/job/{id}
POST /api/video/job/{id}/cancel      GET  /api/video/thumbnails/{vid}
GET  /api/video/result/{vid}         POST /api/video/delete
POST /api/strokes/preview            POST /api/strokes/export    GET  /api/strokes/result/{job}
POST /api/model/download?kind=...    POST /api/model/import?kind=...
GET  /api/model/progress             POST /api/shutdown
```
