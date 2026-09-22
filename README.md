# 凌日光影棚 Horizon Light Studio

为「加查俱乐部」及同类 2D 动漫风格的**图片与视频**提供智能光照增强：
AI 深度/法线估计 + 本地多光源物理渲染，**完全独立运行**，
不依赖任何图像编辑宿主（GIMP/Photoshop 等）。

界面为 **WebUI（Node 构建前端 + Python 提供引擎与本地服务）**，
打包为 Windows `.exe` / macOS `.dmg` / Linux `.AppImage`。
核心引擎（`core/`）零 GUI 依赖，可单独用于批处理。

## 功能总览

### AI 预处理层（四级后端链，失败/超时自动降级，绝不卡死）
1. **内置本地引擎** —— ONNX Runtime，离线，支持两种深度模型并自动识别：
   **Depth Anything V2 Small**（ControlNet 官方深度预处理器同源，推荐）
   或 **MiDaS-small**；另可选装 **MoGe-2 法线模型**输出真实法线
   （未装则由深度几何派生）。模型由用户下载或离线导入，**软件不捆绑任何权重**
2. **用户自建 FastAPI 服务** —— HTTP localhost，一次返回**深度与法线**
3. **云端 API** —— 阿里云 / Replicate / OpenAI 兼容；高分辨率图片或长视频时提示启用
4. **灰度梯度模拟** —— 保底「仅预览模式」，完全离线

### 物理渲染引擎（本地离线）
- 多光源 Lambert + Phong、色温（开尔文）
- **阴影投射互斥**（2.5D 高度场 Shadow Mapping，支持多光源遮挡关系）
- 环境光强度/色温**独立调节**

### 交互与光源控制
- **无限多光源**：3D 轨迹球 + 数值滑块（强度 / 色温 / 半径），方向光与点光
- **双路径智能拾取**（两条都已实现）
  - **a. 点击映射**：点击图片或视频某一帧的像素 → 主光源锚定该点**法线的逆方向**
  - **b. 保守参考图迁移**：导入参考图 → 提取主光方向与全局色温，作为**限幅偏移**
    （±15° / ±500K / ±0.25）叠加，保留原图明暗结构，仅柔和微调（可一键撤销）
- **阴影锐度一键切换**：硬朗（日系赛璐珞）/ 柔化半影（摄影棚）
- **一键重置**：当前预设的全部参数恢复默认

### AI 自动打光（文字 → 光照节点）
用一句中文描述灯光（如「黄昏舞台逆光，暖橘主光从左后方，冷蓝补光勾勒轮廓」），
软件**离线**解析成可编辑的光照节点。离线词表保证断网必出结果；配置云端后可选
调用大模型做更细解析，失败自动回落离线。

### 专业模式（工作流节点图）
把光照拆成节点连线：输入 / AI 深度 / AI 自动打光 / 光源 / 全局光照 / 点击拾取 /
参考图迁移 / 输出。拓扑求值、参数自动限幅、环路与未知节点**明确报错**；
工作流可导出/导入 JSON。

### 插件管理与扩展
**纯数据插件**（只解析 JSON 清单，**永不执行插件代码**）。四类扩展：预设、主题、
灯组、后端声明；可逐个启用/停用（状态持久化）。仓库自带 3 个示例插件。

### 主题引擎
8 套主题：**暗夜蓝调（默认）/ 霓虹青紫 / 暗房琥珀 / 石墨灰阶 / 森林绿 / 胶片暖褐 /
晨雾浅色（浅色）/ 高对比无障碍（WCAG AAA）**。主题不只是颜色，还含字体、字号缩放、
圆角、描边宽度、密度；选择写入本地配置，重启后保持。

### 统一媒体入口
拖入或打开文件即按**文件内容（魔数）**自动识别图片或视频，无需先选模式；
改名过的文件也能正确识别。大文件按流式处理，不占内存。

### 视频处理模式
- **关键帧分析传播（默认）**：仅对关键帧做 AI 分析，其余帧深度在相邻关键帧之间
  线性插值 + 时间轴平滑，光照全片统一。快，适合加查动画。
- **逐帧分析（高质量）**：每帧单独 AI 分析，光照随画面动态变化。慢，适合复杂场景。
- **视频预览播放 + 逐帧检查**
- 输出**同格式视频**并**保留原音轨**（音频直通，只重编码视频）

### 13 个专业界面
| 界面 | 用途 |
|---|---|
| 光影工作台 | 图片导入、光照调节、导出 |
| 智能打光 | 文字 → 光照节点 |
| 专业模式 | 工作流节点图 |
| 视频调光 | 视频两种分析模式、逐帧检查、导出 |
| 逐帧光绘 | 在视频上逐帧手绘光效并合成导出 |
| 光源星图 | 俯视灯位图 / 拓扑 / 列表 |
| 智能拾光 | 从参考照片提取三点布光方案 |
| 三维布光预演 | 虚拟棚内空间预演（纯软件 3D） |
| 暗房终端 | 真实命令控制台（不支持的能力明确回「不支持」） |
| 调光台 | CUE 预设与淡变、通道推子/独奏/静音 |
| 光影指挥屏 | 灯位与相对遥测、事件日志 |
| 插件中心 | 插件安装/启停/详情 |
| 主题引擎 | 8 套主题与设计令牌、对比度校验 |

### 其它
- **5 种预设**：标准棚拍、逆光黄昏、冷色月夜、舞台聚光、霓虹氛围
- **缓存**：AI 分析结果本地持久化（JSON + 深度 PNG + 法线 PNG），同图/同关键帧不重复计算
- **设置持久化**：`~/.horizon_light_studio/config.json`
- 界面中文；输入 PNG/JPEG 64~4096px、视频 MP4/AVI/MOV 等（建议 ≤1080p、≤5 分钟）
- **诚实性**：引擎只做相对 Lambert/Phong 光照，**无法计算**照度(lx)/CRI/Δuv/频闪/
  灯具温度等绝对值——界面不显示这类读数，并对估算值明确标注

## 性能指标（实测）

| 场景 | 实测 | 规格上限 |
|---|---|---|
| 图片 1024px 快速预览 | 约 0.2s | ≤ 15s |
| 图片 1024px 高质量物理阴影 | 约 2.2s | ≤ 60s |
| **视频 1080p 30fps 10s（关键帧传播）** | **96.7s / 300 帧** | **≤ 5 分钟** |
| 视频高质量（逐帧） | 随帧数线性增加 | — |

> 视频逐帧渲染时内存保持平稳（1080p 全程增长约 122MB）：深度按帧流式生成，
> 不物化整片深度。

## 目录结构

```
core/          共享核心引擎（零 GUI 依赖）
  render.py      物理渲染（光照、色温、阴影投射互斥、双性能模式）
  ai_backend.py  四级 AI 后端链与降级
  video.py       视频读写、关键帧选取、深度传播、逐帧处理
  autolight.py   文字 → 光照节点（离线词表 + 可选云端）
  workflow.py    专业模式节点图求值
  plugins.py     纯数据插件注册表
  theme.py       8 套主题与设计令牌
  cues.py        CUE 预设与淡变插值
  harvest.py     从参考图提取灯光方案
  strokes.py     逐帧光绘合成
  paths.py       用户数据目录与旧版迁移
  pipeline.py    单图处理编排
  cache.py       AI 结果持久化缓存
webui/         桌面软件外壳
  api.py         本地 FastAPI 服务（界面与引擎之间的接口）
  desktop.py     桌面启动器（打包入口，stdio 安全 + 启动日志）
  frontend/      前端源码与构建产物（Node 仅构建期需要）
    src/screens/   13 个界面模块
server/        用户自建深度服务（FastAPI，独立部署）
packaging/     三平台打包脚本与配置
tests/         单元测试（python -m pytest tests/）
docs/          用户手册 / API 文档 / 构建指南 / 视频处理指南 / 设计稿导出
hls_cli.py     命令行渲染入口
gimp_plugin/   GIMP 2.10+ 插件（可选，非核心交付）
plugins/examples/  示例插件清单
```

## 快速开始

**使用发行版**：双击 `HorizonLightStudio.exe`（Windows）/ 打开 `.dmg` 拖入应用程序
（macOS）/ 运行 `.AppImage`（Linux）。软件会自动启动本地服务并打开界面。

**从源码**：

```bash
# 方式一：uv（推荐，自动管理虚拟环境与锁文件）
uv sync                          # 安装全部依赖（含开发工具）
uv run python -m webui.desktop   # 启动服务并打开界面

# 方式二：pip
pip install -r requirements.txt
python -m webui.desktop          # 启动服务并打开界面
```

> 只跑软件不需要开发工具：`uv sync --no-dev`。
> 前端若尚未构建，界面会提示构建命令；仅用于开发。
> 构建前端（仅开发期需要 Node）：`cd webui/frontend && npm install && npm run build`

**首次使用内置深度引擎**：界面「设置 → AI 后端」→「一键下载模型」。

| 模型 | 下载体积 | 作用 | 是否必需 |
| --- | --- | --- | --- |
| Depth Anything V2 Small | 约 94MB | 深度图（ControlNet 深度预处理同源） | 二选一 |
| MiDaS-small | 约 64MB | 深度图（体积更小） | 二选一 |
| MoGe-2 法线 | 约 134MB | 法线图（+z 朝观察者） | 可选 |

深度模型两者皆可，**都已部署时优先用 Depth Anything V2**；也可在外部下载
`.onnx` 后用「离线导入模型…」（支持 `.onnx`，界面按类型分「深度 / 法线」）。
不装模型也能用：自动降级为灰度模拟（仅预览）或自建服务/云端。

> 下载源默认走 `hf-mirror.com`（目标网络下 huggingface.co 通常不可达）。
> 需要换镜像时设 `HLS_DEPTH_MODEL_URL` / `HLS_NORMAL_MODEL_URL`
> （MiDaS 用 `HLS_MODEL_URL`）指向任意可达地址。

## 本地自建深度服务（可选）

```bash
server/setup_deployment.sh      # Windows 用 setup_deployment.bat
server/run_server.sh            # Windows 用 run_server.bat
```

服务监听 `http://127.0.0.1:8765`，接口 `/health`、`/analyze`、`/depth`、`/normal`。
详见 `docs/API接口文档.md` 与 `docs/用户手册.md`。

> 若「内置本地引擎」保持启用，链会在内置引擎处命中，自建服务不会被调用；
> 要使用自建服务请先取消勾选内置引擎。

## 打包发行版

```bat
packaging\build_windows.bat        :: → dist\HorizonLightStudio.exe
```
```bash
bash packaging/build_macos.sh      # → dist/凌日光影棚-1.0.0.dmg
bash packaging/build_linux.sh      # → dist/凌日光影棚-1.0.0-x86_64.AppImage
```

**验证状态（诚实记录）**

| 平台 | 状态 |
|---|---|
| Windows `.exe` | **已实际打包并实测**：双击等价启动 4 秒就绪、13 个界面齐全、内置 ffmpeg 可用、`/api/shutdown` 干净退出（无残留进程）、启动日志写入 `~/.horizon_light_studio/startup.log`。构建环境 Python 3.14.7 + PyInstaller 6.22.2。 |
| macOS `.dmg` | 仅提供脚本，**未执行、未验证**（需在 macOS 上运行 `build_macos.sh`） |
| Linux `.AppImage` | 仅提供脚本，**未执行、未验证**（需在 Linux 上运行 `build_linux.sh`） |

详见 `docs/构建指南.md`（含代码签名与公证说明）。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 命令行

```bash
python hls_cli.py --preset 逆光黄昏 --lighting hq in.png out.png
python hls_cli.py --preview-only in.png out.png      # 完全离线
```

## 许可

MIT
