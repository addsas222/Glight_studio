/**
 * 后端接口客户端（唯一入口，界面各处不得直接 fetch）。
 *
 * 约定：
 *  - 所有接口都在本机服务下（同源 /api/*），由 webui/api.py 提供。
 *  - 图像/视频以 base64 PNG 字符串返回（**不含** data: 前缀）。
 *  - 出错时统一抛 ApiError，携带后端返回的中文 detail，便于直接展示。
 *  - 不对网络返回值做无校验断言：JSON 接口按声明的响应类型解析，
 *    失败即抛 ApiError（比静默产生 undefined 更安全）。
 */

export interface Light {
  name: string;
  kind: "directional" | "point";
  dx: number; dy: number; dz: number;
  px: number; py: number; pz: number;
  intensity: number; kelvin: number; radius: number;
  visible: boolean;
  group?: string;
}

export interface RenderParams {
  lights: Light[];
  ambient_intensity: number;
  ambient_kelvin: number;
  shadow_mode: "soft" | "hard";
  lighting_mode: "linear" | "hq";
  preview_size: number;
  lighting_size: number;
  shadow_strength: number;
  specular_strength: number;
  tone_preserve: number;
  exposure: number;
  reference_offset?: Record<string, number> | null;
}

export interface Preset { name: string; params: RenderParams; }
export interface ThemeInfo { id: string; name: string; dark: boolean; accent: string; }

export interface VideoInfo {
  path: string; width: number; height: number; fps: number; duration: number;
  frame_count: number; codec: string; has_audio: boolean; container: string;
}

export interface VideoHints {
  over_recommended_size: boolean;
  over_recommended_duration: boolean;
  cloud_suggested: boolean;
}

export type JobStatus = "running" | "done" | "error" | "cancelled";

export interface JobState {
  job_id: string;
  state: JobStatus;
  progress: number;
  message: string;
  result: VideoJobResult | null;
  error: string | null;
}

export interface VideoJobResult {
  output?: string;
  frames?: number;
  mode?: string;
  backend?: string;
  keyframes?: number[] | null;
  download_name?: string;
}

export interface PluginInfo {
  id: string; name: string; version: string; kind: string;
  description?: string; author?: string; enabled?: boolean; path?: string;
}

export interface PluginState {
  plugins: PluginInfo[];
  enabled: string[];
  kinds: string[];
}

export interface AppSettings {
  order?: string[];
  preview_only?: boolean;
  builtin_enabled?: boolean;
  builtin_model_path?: string;
  local_enabled?: boolean;
  local_url?: string;
  cloud_enabled?: boolean;
  cloud_provider?: string;
  cloud_base_url?: string;
  cloud_model?: string;
  cloud_api_key_set?: boolean;
  cloud_api_key?: string;
  timeout?: number;
}

/** 内置模型类型：depth=MiDaS-small、dav2=Depth Anything V2、normal=MoGe-2。 */
export type ModelKind = "depth" | "dav2" | "normal";

export interface ModelDownloadResult {
  ok: boolean;
  already: boolean;
  kind: ModelKind;
  path: string;
  size_mb?: number;
  /** 实际使用的下载源（便于排查镜像是否生效） */
  source?: string;
  message: string;
}

export interface ModelImportResult {
  ok: boolean;
  kind: ModelKind;
  path: string;
  size_mb: number;
  message: string;
}

/** 模型下载进度：`total` 为 0 表示服务器没给 Content-Length，百分比不可知。 */
export interface ModelProgress {
  active: boolean;
  kind: string;
  done: number;
  total: number;
  percent: number;
}

/**
 * 启动预热进度：`idle → scheduled → warming → ready | missing`。
 *
 * `ready`/`missing` 是**精确结论**，`warming` 只表示后台正在加载 ——
 * 首屏拿到的 `model_ready` 是 allow_load=False 的快速判断，可能偏保守，
 * 因此界面在 warming 期间应显示「加载中」而不是「未就绪」。
 */
export interface WarmupState {
  state: "idle" | "scheduled" | "warming" | "ready" | "missing";
  seconds: number;
  depth: boolean;
  normal: boolean;
  detail: string;
}

export interface AppState {
  presets: Preset[];
  preset_names: string[];
  settings: AppSettings;
  cache_dir: string;
  model_ready: boolean;
  /** 法线模型是否就绪（可选增强；未装时法线由深度几何派生） */
  normal_model_ready?: boolean;
  /** 启动预热进度（旧后端可能不返回） */
  warmup?: WarmupState;
  version: string;
  high_res_hint: boolean;
  ffmpeg: boolean;
  video_exts: string[];
  video_limits: {
    recommend_max_side: number;
    recommend_max_duration: number;
    hard_max_side: number;
    hard_max_duration: number;
  };
  params: RenderParams;
  themes: { default: string; /** 已持久化的主题选择（后端配置） */ current?: string; themes: ThemeInfo[] };
}

export interface MediaImage {
  kind: "image";
  image_id: string;
  width: number;
  height: number;
  original_png_b64: string;
  high_res_hint: boolean;
}

export interface MediaVideo {
  kind: "video";
  video_id: string;
  info: VideoInfo;
  hints: VideoHints;
  source_name: string;
}

export type MediaResult = MediaImage | MediaVideo;

export interface AnalyzeResult {
  backend: string;
  from_cache: boolean;
  depth_png_b64: string;
  normal_png_b64: string | null;
}

export interface RenderResult {
  png_b64: string;
  elapsed: number;
  backend: string;
  from_cache: boolean;
  width: number;
  height: number;
  high_res_hint: boolean;
}

export interface PickResult { dx: number; dy: number; dz: number; nx: number; ny: number; nz: number; }

export interface AutoLightNode { type: string; name: string; params: Record<string, unknown>; }

export interface AutoLightResult {
  rig: { name: string; prompt: string; lights: Light[]; ambient_intensity: number; ambient_kelvin: number; shadow_mode: string; source: string; note: string };
  params: RenderParams;
  nodes: AutoLightNode[];
  note: string;
  source: "offline" | "cloud";
}

export interface Stroke {
  type: "point" | "glow" | "beam" | "starburst";
  x: number; y: number; radius: number; intensity: number; kelvin: number;
  softness: number; blend: "add" | "screen" | "soft";
  angle?: number; spread?: number; spikes?: number;
}

export interface Cue { id: string; name: string; params: RenderParams; fade: number; created?: number; }

export interface HarvestedLight {
  role: string;
  light: Light;
  confidence: number;
  basis: string;
}

export interface HarvestResult {
  scene_type: string;
  ambient_kelvin: number;
  lights: HarvestedLight[];
  key_to_fill_ratio: number;
  contrast: number;
  notes: string[];
  params: RenderParams;
}

export interface WorkflowNodeType {
  id: string; name: string; category: string;
  inputs: { id: string; label: string }[];
  outputs: { id: string; label: string }[];
}

export interface WorkflowNode { id: string; type: string; name?: string; params?: Record<string, unknown>; x?: number; y?: number; }
export interface WorkflowEdge { src: string; dst: string; src_port?: string; dst_port?: string; }
export interface Workflow { nodes: WorkflowNode[]; edges: WorkflowEdge[]; name?: string; }

export interface WorkflowTypes {
  node_types: WorkflowNodeType[];
  default: Workflow;
}

export interface WorkflowEval {
  ok: boolean;
  problems: string[];
  params: RenderParams | null;
}

export interface ThemeDetail {
  id: string;
  name: string;
  dark: boolean;
  tokens: Record<string, string>;
  /** 非颜色变量（字体/圆角/描边/密度）；旧后端可能不返回 */
  variables?: Record<string, string>;
  css: string;
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, detail: string) {
    super(detail || code);
    this.status = status;
    this.code = code;
  }
}

const parseError = (j: unknown): { code?: string; detail?: string } => {
  if (typeof j !== "object" || j === null) return {};
  const out: { code?: string; detail?: string } = {};
  if ("error" in j && typeof j.error === "string") out.code = j.error;
  if ("detail" in j && typeof j.detail === "string") out.detail = j.detail;
  return out;
};

async function callJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "network", "无法连接本地服务，请确认软件仍在运行。");
  }
  if (!res.ok) {
    let code = `http_${res.status}`;
    let detail = `请求失败（${res.status}）`;
    const ctype = res.headers.get("content-type") || "";
    if (ctype.includes("json")) {
      try {
        const { code: c, detail: d } = parseError(await res.json());
        if (c) code = c;
        if (d) detail = d;
      } catch { /* 保留默认信息 */ }
    }
    throw new ApiError(res.status, code, detail);
  }
  const payload: unknown = await res.json();
  return payload as T;
}

/** 下载二进制（成品视频），返回 Blob URL 由调用方负责 revoke */
async function callBlob(path: string): Promise<Blob> {
  const res = await fetch(path);
  if (!res.ok) throw new ApiError(res.status, `http_${res.status}`, "下载失败");
  return res.blob();
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** 原始字节上传（统一入口按魔数识别类型，无需声明 Content-Type） */
function raw(bytes: Blob | Uint8Array, filename?: string): RequestInit {
  const headers: Record<string, string> = { "Content-Type": "application/octet-stream" };
  if (filename) headers["X-Filename"] = filename;
  return { method: "POST", headers, body: bytes as BodyInit };
}

// ---------------------------------------------------------------- 接口

export const api = {
  // 状态与设置
  state: () => callJson<AppState>("/api/state"),
  saveSettings: (settings: AppSettings) => callJson<{ ok: boolean; settings: AppSettings }>(
    "/api/settings", json(settings)),
  clearCache: () => callJson<{ removed: number }>("/api/cache/clear", { method: "POST" }),
  shutdown: () => callJson<{ ok: boolean }>("/api/shutdown", { method: "POST" }),

  // 内置模型（kind 走查询串：import 的请求体已被 .onnx 原始字节占用）
  modelDownload: (kind: ModelKind = "depth") =>
    callJson<ModelDownloadResult>(
      `/api/model/download?kind=${encodeURIComponent(kind)}`, { method: "POST" }),
  modelImport: (bytes: Blob | Uint8Array, kind: ModelKind = "depth") =>
    callJson<ModelImportResult>(
      `/api/model/import?kind=${encodeURIComponent(kind)}`, raw(bytes)),
  modelProgress: () => callJson<ModelProgress>("/api/model/progress"),

  // 主题
  themes: () => callJson<{ default: string; themes: ThemeInfo[] }>("/api/themes"),
  theme: (id: string) => callJson<ThemeDetail>(`/api/theme/${encodeURIComponent(id)}`),
  setTheme: (id: string) => callJson<{ ok: boolean; theme: ThemeDetail }>(
    "/api/theme", json({ id })),

  // 媒体（图片/视频自动识别）
  media: (bytes: Blob | Uint8Array, filename?: string) => callJson<MediaResult>("/api/media", raw(bytes, filename)),
  image: (bytes: Blob | Uint8Array, filename?: string) => callJson<MediaImage>("/api/image", raw(bytes, filename)),

  // 图片流程
  analyze: (image_id: string) => callJson<AnalyzeResult>("/api/analyze", json({ image_id })),
  render: (image_id: string, params: RenderParams) =>
    callJson<RenderResult>("/api/render", json({ image_id, params })),
  pick: (image_id: string, x: number, y: number) =>
    callJson<PickResult>("/api/pick", json({ image_id, x, y })),
  reference: (image_id: string, params: RenderParams, reference_png_b64: string) =>
    callJson<{ params: RenderParams; offset: Record<string, number> }>(
      "/api/reference", json({ image_id, params, reference_png_b64 })),
  exportImage: (image_id: string, params: RenderParams, format: "png" | "jpeg") =>
    callJson<{ png_b64: string; filename: string }>("/api/export", json({ image_id, params, format })),
  depthPreview: (image_id: string) =>
    callJson<{ depth_png_b64: string }>("/api/depth-preview", json({ image_id })),

  // AI 自动打光
  autolight: (text: string, opts?: { use_cloud?: boolean; base_params?: RenderParams; apply?: boolean }) =>
    callJson<AutoLightResult>("/api/autolight", json({ text, ...opts })),

  // CUE 预设（带淡变）
  cues: () => callJson<{ cues: Cue[] }>("/api/cues"),
  cueSave: (name: string, params: RenderParams, fade: number, id?: string) =>
    callJson<{ cue: Cue }>("/api/cues/save", json({ name, params, fade, id: id || "" })),
  cueDelete: (id: string) => callJson<{ ok: boolean }>("/api/cues/delete", json({ id })),
  cueInterpolate: (id: string, params: RenderParams, t: number) =>
    callJson<{ params: RenderParams }>("/api/cues/interpolate", json({ id, params, t })),

  // 插件
  plugins: () => callJson<PluginState>("/api/plugins"),
  pluginInstall: (manifest: unknown) =>
    callJson<{ ok: boolean; plugin: PluginInfo; state: PluginState }>("/api/plugins/install", json(manifest)),
  pluginToggle: (id: string, enabled: boolean) =>
    callJson<{ ok: boolean; state: PluginState }>("/api/plugins/toggle", json({ id, enabled })),
  pluginRemove: (id: string) =>
    callJson<{ ok: boolean; state: PluginState }>("/api/plugins/remove", json({ id })),

  // 工作流（专业模式）
  workflowTypes: () => callJson<WorkflowTypes>("/api/workflow/types"),
  workflowEvaluate: (workflow: Workflow) =>
    callJson<WorkflowEval>("/api/workflow/evaluate", json({ workflow })),

  // 智能拾光
  harvest: (bytes: Blob | Uint8Array, filename?: string) =>
    callJson<HarvestResult>("/api/harvest", raw(bytes, filename)),

  // 视频
  videoUpload: (bytes: Blob | Uint8Array, filename: string) =>
    callJson<MediaVideo>("/api/video", raw(bytes, filename)),
  videoFrame: (video_id: string, index: number) =>
    callJson<{ png_b64: string; width: number; height: number; index: number }>(
      "/api/video/frame", json({ video_id, index })),
  videoKeyframes: (video_id: string, count = 8) =>
    callJson<{ keyframes: number[]; count: number }>("/api/video/keyframes", json({ video_id, count })),
  videoPick: (video_id: string, index: number, x: number, y: number) =>
    callJson<PickResult>("/api/video/pick", json({ video_id, index, x, y })),
  videoProcess: (body: {
    video_id: string; params: RenderParams; mode: "keyframe" | "perframe";
    keyframe_count?: number; smooth?: number;
  }) => callJson<{ job_id: string }>("/api/video/process", json(body)),
  videoJob: (job_id: string) => callJson<JobState>(`/api/video/job/${job_id}`),
  videoCancel: (job_id: string) =>
    callJson<{ ok: boolean }>(`/api/video/job/${job_id}/cancel`, { method: "POST" }),
  videoThumbnails: (video_id: string, count = 12, width = 96) =>
    callJson<{ count: number; frames: number[]; width: number; height: number; png_b64: string[] }>(
      `/api/video/thumbnails/${video_id}?count=${count}&width=${width}`),
  videoResultBlob: (video_id: string) => callBlob(`/api/video/result/${video_id}`),
  videoDelete: (video_id: string) => callJson<{ ok: boolean }>("/api/video/delete", json({ video_id })),

  // 逐帧光绘
  strokesPreview: (video_id: string, index: number, strokes: Stroke[]) =>
    callJson<{ png_b64: string }>("/api/strokes/preview", json({ video_id, index, strokes })),
  strokesExport: (body: { video_id: string; params: RenderParams; track: Record<string, Stroke[]> }) =>
    callJson<{ job_id: string }>("/api/strokes/export", json(body)),
  strokesResultBlob: (job_id: string) => callBlob(`/api/strokes/result/${job_id}`),
};

/** 把 base64 PNG 变成可直接放进 <img> 的 data URL */
export const pngUrl = (b64: string): string => `data:image/png;base64,${b64}`;

/** 触发下载（base64 内容或 Blob URL） */
export function downloadB64(b64: string, filename: string, mime = "image/png"): void {
  const a = document.createElement("a");
  a.href = `data:${mime};base64,${b64}`;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function downloadBlobUrl(url: string, filename: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** 轮询任务直到结束；onTick 可用于刷新进度条 */
export async function pollJob(
  jobId: string,
  onTick?: (s: JobState) => void,
  intervalMs = 500,
): Promise<JobState> {
  for (;;) {
    const s = await api.videoJob(jobId);
    onTick?.(s);
    if (s.state !== "running") return s;
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, intervalMs);
    await promise;
  }
}
