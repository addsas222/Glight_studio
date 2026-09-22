/**
 * B1 调光台（Lighting Desk）—— 基于真实 CUE + 通道模型的控制台视图。
 *
 * 左栏：CUE 预设场景（api.cues / cueSave / cueDelete / cueInterpolate）、
 *       淡变时长、当前 CUE 与淡变进度、对比原图。
 * 中栏：当前画面（图片走 api.render，视频显示当前帧）+ 灯位图（俯视平面，canvas）。
 * 右栏：通道推子（真实写回 params.lights[i].intensity / visible）、主控、独奏、
 *       AI 自动打光（api.autolight）、参考图迁移（api.reference）、输出信息。
 *
 * 诚实规则：引擎只计算相对 Lambert/Phong 光照，本屏幕只显示可由参数推导的量
 * （相对强度 0–4、色温 K、半径、位置、光源数量、环境强度）。照度 lx、CRI、
 * Δuv、频闪、DMX、灯具温度等物理量本引擎无法计算，因此一律不显示。
 *
 * 淡变实现：绝不在动画帧里重新渲染（1.2s 淡变按 ~0.2s/帧只有 1~6 帧，会跳变并
 * 堆积渲染队列）。做法是：渲染「目标」得到一张新位图，与当前显示位图做 CSS
 * opacity 交叉溶解，时长 = fade 秒 —— 零额外渲染、与帧率无关。api.cueInterpolate
 * 只用于在需要中间帧参数时计算插值参数（这里取 t=1 直接得到目标参数）。
 */
import { api, pngUrl, type Cue, type Light, type RenderParams } from "../api";
import { button, clear, h, icon, reportError, slider, store, toast } from "../ui";
import { currentParams, setParams, type Screen } from "../shell";

/** 引擎强度上限（core/types.py Light.intensity 取值 0~4） */
const INTENSITY_MAX = 4;
/** 淡变滑杆上限（core/cues.py FADE_MAX = 30s） */
const FADE_MAX = 30;

/** 类型代号：光源未设置 group 时按序号回退到这些角色（界面显示用，不参与渲染） */
const ROLE_ZH = ["主光", "补光", "轮廓光", "背景光", "眼神光", "氛围光"];
const ROLE_EN = ["KEY", "FILL", "RIM", "BG", "EYE", "AMB"];

const AI_PLACEHOLDER = "描述想要的光照氛围：黄昏侧逆光，暖色轮廓光打在角色右后方，保留原画明暗结构。";

/** 参考图迁移返回的偏移键名 → 展示用中文标签（与智能打光屏的用词一致） */
const OFFSET_LABELS: Record<string, string> = {
  angle_deg: "主光方向", angle: "主光方向", dir: "主光方向", direction: "主光方向",
  kelvin: "色温", temp: "色温", temperature: "色温",
  intensity: "强度", strength: "强度", scale: "强度",
};

/** 偏移量的展示值：按键名补上单位，未知键原样保留数值 */
function offsetValue(key: string, value: number): string {
  const k = key.replace(/^d_/, "");
  const sign = value > 0 ? "+" : "";
  if (k.includes("angle") || k === "dir" || k === "direction") return `${sign}${value.toFixed(1)}°`;
  if (k.includes("kelvin") || k === "temp" || k === "temperature") return `${sign}${Math.round(value)} K`;
  return `${sign}${Math.round(value * 100) / 100}`;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** 色温 → 十六进制色。同一近似式（Tanner Helland）见 core/render.py，仅用于界面显示 */
function kelvinHex(k: number): string {
  const t = clamp(k, 1500, 12000) / 100;
  let r: number;
  let g: number;
  let b: number;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
  } else {
    r = 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(t - 60, -0.0755148492);
  }
  if (t >= 66) b = 255;
  else if (t <= 19) b = 0;
  else b = 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  const byte = (v: number) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, "0");
  return `#${byte(r)}${byte(g)}${byte(b)}`;
}

function hexA(hex: string, alpha: number): string {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${clamp(alpha, 0, 1).toFixed(3)})`;
}

/** 读取本地文件为裸 base64（/api/reference 只接受裸 base64，不接受 data URL） */
async function fileToB64(file: File): Promise<string> {
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  const reader = new FileReader();
  reader.onload = () => {
    const text = String(reader.result ?? "");
    const comma = text.indexOf(",");
    resolve(comma >= 0 ? text.slice(comma + 1) : text);
  };
  reader.onerror = () => reject(new Error("读取文件失败"));
  reader.readAsDataURL(file);
  return promise;
}

/**
 * 可编程回填的滑杆行。ui.slider 不暴露 input 元素，而「淡变时长」「主控」
 * 需要按外部状态（选中 CUE 的存储 fade / 重置通道）回填显示值。
 */
function rangeRow(opts: {
  label: string;
  min: number; max: number; step: number; value: number;
  format: (v: number) => string;
  onInput: (v: number) => void;
}): { el: HTMLElement; set: (v: number) => void } {
  const val = h("b", {}, opts.format(opts.value));
  const input = h("input", {
    type: "range", min: opts.min, max: opts.max, step: opts.step, value: opts.value,
  });
  input.addEventListener("input", () => {
    const v = Number(input.value);
    val.textContent = opts.format(v);
    opts.onInput(v);
  });
  const el = h("div", { class: "slider-row" },
    h("div", { class: "slider-row__head" }, h("span", {}, opts.label), val),
    input,
  );
  return {
    el,
    set: (v: number) => { input.value = String(v); val.textContent = opts.format(v); },
  };
}

/**
 * 双层图片栈：渲染结果放在 front，淡变时把目标位图放到 back 并用 CSS opacity
 * 过渡做交叉溶解。整个过程只渲染一次目标图，不做逐帧重渲染。
 */
class FadeStack {
  readonly el: HTMLDivElement;
  #front: HTMLImageElement;
  #back: HTMLImageElement;
  #inner: HTMLDivElement;
  #zoom = 1;
  #panX = 0;
  #panY = 0;
  #dragging = false;
  #lastX = 0;
  #lastY = 0;
  #token = 0;
  #onZoom?: (zoom: number) => void;

  constructor() {
    const layer = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;max-width:none;max-height:none";
    this.#front = h("img", { class: "canvas-img", alt: "渲染结果", style: layer, draggable: "false" });
    this.#back = h("img", { class: "canvas-img", alt: "淡变目标", style: `${layer};opacity:0`, draggable: "false" });
    const inner = h("div", {
      style: "position:absolute;inset:0;transform-origin:center center;will-change:transform",
    }, this.#front, this.#back);
    this.el = h("div", {
      style: "position:relative;flex:1;min-height:0;overflow:hidden;background:var(--hls-bg-canvas);cursor:grab",
    }, inner);
    this.#inner = inner;

    this.el.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.#zoom = clamp(this.#zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 0.05, 8);
      this.#applyTransform();
    }, { passive: false });
    this.el.addEventListener("pointerdown", (e) => {
      this.#dragging = true;
      this.#lastX = e.clientX;
      this.#lastY = e.clientY;
    });
    this.el.addEventListener("pointermove", (e) => {
      if (!this.#dragging) return;
      this.#panX += e.clientX - this.#lastX;
      this.#panY += e.clientY - this.#lastY;
      this.#lastX = e.clientX;
      this.#lastY = e.clientY;
      this.#applyTransform();
    });
    const up = (): void => { this.#dragging = false; };
    this.el.addEventListener("pointerup", up);
    this.el.addEventListener("pointerleave", up);
  }

  #applyTransform(): void {
    this.#inner.style.transform = `translate(${this.#panX}px, ${this.#panY}px) scale(${this.#zoom})`;
    this.#onZoom?.(this.#zoom);
  }

  onZoom(fn: (zoom: number) => void): void { this.#onZoom = fn; }

  get zoom(): number { return this.#zoom; }

  setZoom(z: number): void {
    this.#zoom = clamp(z, 0.05, 8);
    this.#applyTransform();
  }

  fit(): void {
    this.#zoom = 1;
    this.#panX = 0;
    this.#panY = 0;
    this.#applyTransform();
  }

  /** 直接替换显示位图（通道推子/参数变化走这里，不做溶解） */
  setImage(url: string | null): void {
    this.#token++;
    if (!url) {
      this.#front.removeAttribute("src");
      this.#back.removeAttribute("src");
      return;
    }
    this.#front.src = url;
    this.#back.style.transition = "none";
    this.#back.style.opacity = "0";
  }

  /** 交叉溶解到目标位图：等目标图加载完成，再让 back 在 seconds 秒内淡入 front 之上 */
  async fadeTo(url: string, seconds: number): Promise<void> {
    const token = ++this.#token;
    if (seconds <= 0.02) { this.setImage(url); return; }
    this.#back.src = url;
    // 等待目标位图可绘制。刻意不用 img.decode()（部分内核下该 Promise 不落地，
    // 会让溶解永久卡住）；只在尚未 complete 时听 load/error，并留 1.5s 兜底。
    const back = this.#back;
    if (!back.complete) {
      const { promise, resolve } = Promise.withResolvers<void>();
      const done = (): void => {
        window.clearTimeout(timer);
        back.removeEventListener("load", done);
        back.removeEventListener("error", done);
        resolve();
      };
      const timer = window.setTimeout(done, 1500);
      back.addEventListener("load", done);
      back.addEventListener("error", done);
      await promise;
    }
    if (token !== this.#token) return;
    this.#back.style.transition = "none";
    this.#back.style.opacity = "0";
    void this.#back.offsetWidth;                       // 强制回流，确保过渡从 0 开始
    this.#back.style.transition = `opacity ${seconds}s linear`;
    this.#back.style.opacity = "1";
    const { promise, resolve } = Promise.withResolvers<void>();
    window.setTimeout(resolve, seconds * 1000 + 30);
    await promise;
    if (token !== this.#token) return;
    this.#front.src = url;
    this.#back.style.transition = "none";
    this.#back.style.opacity = "0";
  }
}

export function createDeskScreen(): Screen {
  // ------------------------------------------------------------ 跨挂载保留的界面状态
  let cues: Cue[] = [];
  let selectedCueId: string | null = null;
  let activeCueId: string | null = null;
  let fade = 1.2;
  /** 主控：比例主控（见 setMaster），界面本地状态，不写入引擎参数 */
  let master = 1;
  /** 主控归零前的强度快照，用于从 0 恢复 */
  let masterBase: number[] | null = null;
  /** 独奏集合（界面本地；渲染时把未独奏通道临时 visible=false，不写回共享参数） */
  const soloed = new Set<string>();
  let selectedChannel = 0;
  let showOriginal = false;
  let lastRenderUrl: string | null = null;
  let lastSize: { w: number; h: number } | null = null;
  let aiNote = "";
  let refOffset: Record<string, number> | null = null;
  let refUndo: RenderParams | null = null;
  let plotZoom = 1;

  // ------------------------------------------------------------ 运行时
  let timer: number | null = null;
  let progressTimer: number | null = null;
  let renderBusy = false;
  let dirty = false;
  let fading = false;
  let selfEdit = false;
  let fadeToken = 0;
  let unsub: (() => void) | null = null;
  let resizeObs: ResizeObserver | null = null;
  let paramsRef: RenderParams | null = null;
  let mediaKey = "";
  let chain: Promise<void> = Promise.resolve();

  const stack = new FadeStack();

  const lightsOf = (): Light[] => currentParams().lights ?? [];

  /** 渲染用的参数：独奏时把未独奏通道临时置为 visible=false（只影响本次渲染） */
  function engineParams(base: RenderParams): RenderParams {
    if (soloed.size === 0) return base;
    return {
      ...base,
      lights: base.lights.map((l) => (soloed.has(l.name) ? l : { ...l, visible: false })),
    };
  }

  function updateLights(next: Light[]): void {
    selfEdit = true;
    setParams({ ...currentParams(), lights: next });
    selfEdit = false;
  }

  function patchLight(name: string, patch: Partial<Light>): void {
    updateLights(lightsOf().map((l) => (l.name === name ? { ...l, ...patch } : l)));
  }

  /** 主控映射：比例微调 —— 拖动按 new/old 比例缩放全部通道强度（0–4 夹紧）；
   *  归零时记录快照，从 0 抬起时按快照比例还原。引擎没有独立主控总线字段，
   *  因此主控只是对通道强度的比例缩放，不额外写任何引擎字段。 */
  function setMaster(v: number): void {
    const list = lightsOf();
    if (v <= 1e-6) {
      masterBase = list.map((l) => l.intensity);
      updateLights(list.map((l) => ({ ...l, intensity: 0 })));
    } else if (master <= 1e-6) {
      const snap = masterBase ?? list.map((l) => l.intensity);
      updateLights(list.map((l, i) => ({
        ...l,
        intensity: clamp((snap[i] ?? l.intensity) * v, 0, INTENSITY_MAX),
      })));
    } else {
      const k = v / master;
      updateLights(list.map((l) => ({ ...l, intensity: clamp(l.intensity * k, 0, INTENSITY_MAX) })));
    }
    master = v;
  }

  // ------------------------------------------------------------ 左栏 DOM
  const cueCountChip = h("span", { class: "chip tiny" }, "0");
  const cueList = h("div", { class: "list" });
  const cueNameInput = h("input", { class: "input", placeholder: "CUE 名称（留空为「未命名 CUE」）" });
  const cueNow = h("div", { class: "small sec" }, "当前 CUE：未应用");
  const cuePhase = h("div", { class: "tiny muted" }, "等待载入");
  const cueBar = h("i", { style: "width:0%" });
  const cueOrigin = h("div", { class: "tiny muted" }, "对比原图：关闭（显示当前渲染）");

  const fadeRow = rangeRow({
    label: "淡变时长",
    min: 0, max: FADE_MAX, step: 0.1, value: fade,
    format: (v) => `${v.toFixed(1)} s`,
    onInput: (v) => { fade = v; },
  });

  const saveBtn = button("保存当前为 CUE", () => void saveCue(), { icon: "plus", variant: "primary" });

  const compareBtn = button("对比原图", () => toggleCompare(), { icon: "image", variant: "ghost" });
  const fitBtn = button("适应窗口", () => stack.fit(), { icon: "crosshair", variant: "ghost" });

  const left = h("div", { class: "panel", style: "width:300px;flex:0 0 300px" },
    h("div", { class: "panel__head" },
      icon("sliders", 13), h("span", {}, "调光台"), h("div", { class: "spacer" }), cueCountChip),
    h("div", { class: "panel__body" },
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "CUE 预设场景"),
        h("span", { class: "tiny muted" }, "点击行选择 · GO 载入")),
      cueList,
      h("div", { class: "divider" }),
      cueNameInput,
      saveBtn,
      fadeRow.el,
      h("div", { class: "tiny muted" }, "保存与载入都用此淡变时长；选中 CUE 会自动填入它存储的值。"),
      h("div", { class: "divider" }),
      h("span", { class: "small", style: "font-weight:600" }, "当前 CUE"),
      cueNow,
      cuePhase,
      h("div", { class: "bar" }, cueBar),
      h("div", { class: "divider" }),
      h("div", { class: "row", style: "gap:8px" }, compareBtn, fitBtn),
      cueOrigin,
      h("div", { class: "tiny muted" }, "画面：滚轮缩放 · 拖拽平移 · 双击适应窗口"),
    ),
  );

  // ------------------------------------------------------------ 中栏 DOM
  const zoomLabel = h("span", { class: "tiny muted mono" }, "100%");
  const imageInfo = h("span", { class: "tiny muted" }, "未载入媒体");

  const zoomOut = button("", () => stack.setZoom(stack.zoom / 1.25), { icon: "minus", variant: "icon", title: "缩小" });
  const zoomIn = button("", () => stack.setZoom(stack.zoom * 1.25), { icon: "plus", variant: "icon", title: "放大" });
  stack.onZoom((z) => { zoomLabel.textContent = `${Math.round(z * 100)}%`; });

  const stage = h("div", { class: "stage", style: "flex-direction:column;align-items:stretch" }, stack.el);

  const plotCanvas = h("canvas", { style: "width:100%;height:100%;display:block" });
  const cursorReadout = h("span", { class: "tiny muted mono" }, "X —  Y —");
  const plotGrid = h("span", { class: "tiny muted" }, "归一化平面 · 每格 0.1 · 强度 0–4 相对标度");

  const plotHead = h("div", {
    class: "row",
    style: "padding:6px 12px;gap:8px;border-bottom:1px solid var(--hls-border);flex-wrap:wrap",
  },
    icon("crosshair", 12, "var(--hls-accent)"),
    h("span", { class: "small", style: "font-weight:600" }, "灯位图 LIGHT PLOT"),
    h("span", { class: "chip tiny" }, "俯视"),
    h("div", { class: "spacer" }),
    cursorReadout,
    h("div", { class: "divider", style: "width:1px;height:14px;margin:0" }),
    button("", () => { plotZoom = clamp(plotZoom / 1.25, 0.5, 3); drawPlot(); }, { icon: "minus", variant: "icon", title: "灯位图缩小" }),
    button("", () => { plotZoom = clamp(plotZoom * 1.25, 0.5, 3); drawPlot(); }, { icon: "plus", variant: "icon", title: "灯位图放大" }),
  );

  const plotBox = h("div", { style: "height:200px;flex:0 0 200px;background:var(--hls-bg-canvas)" }, plotCanvas);

  const center = h("div", { class: "main" },
    h("div", {
      class: "row",
      style: "padding:8px 12px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--hls-border)",
    },
      h("span", { class: "chip" }, icon("sliders", 12, "var(--hls-accent)"),
        "调光台 · 通道推子实时写回当前参数"),
      h("div", { class: "spacer" }),
      zoomOut, zoomLabel, zoomIn,
      imageInfo),
    stage,
    plotHead,
    h("div", { class: "row", style: "padding:4px 12px;gap:8px;border-bottom:1px solid var(--hls-border)" }, plotGrid),
    plotBox,
  );

  // ------------------------------------------------------------ 右栏 DOM
  const channelChip = h("span", { class: "chip tiny" }, "0");
  const channelList = h("div", { class: "list" });
  const soloAllBtn = button("全部独奏", () => toggleAllSolo(), { icon: "star", variant: "ghost" });

  const masterRow = rangeRow({
    label: "主控 MASTER（比例微调）",
    min: 0, max: 1, step: 0.01, value: master,
    format: (v) => `${Math.round(v * 100)}%`,
    onInput: (v) => setMaster(v),
  });

  const masterReadout = h("div", { class: "tiny muted" }, "主控按比例缩放全部通道强度（0 = 全黑，抬回按归零前快照还原）。");

  const aiArea = h("textarea", { class: "textarea", maxlength: 200, placeholder: AI_PLACEHOLDER });
  const aiNoteEl = h("div", { class: "small muted" }, "尚未生成光照节点。");
  const aiBtn = button("生成光照节点", () => void generateLight(), { icon: "sparkles", variant: "primary" });
  aiBtn.style.flex = "1";

  const refInput = h("input", { type: "file", accept: "image/*", class: "hidden" });
  const refSlot = h("div");
  refInput.addEventListener("change", () => {
    const f = refInput.files?.[0];
    refInput.value = "";
    if (f) void transferReference(f);
  });

  const outputLine = h("div", { class: "tiny muted" }, "输出：—");

  const right = h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
    h("div", { class: "panel__head" },
      icon("lightbulb", 13), h("span", {}, "通道 CHANNELS"), h("div", { class: "spacer" }), channelChip),
    h("div", { class: "panel__body" },
      channelList,
      h("div", { class: "divider" }),
      masterRow.el,
      masterReadout,
      h("div", { class: "row", style: "gap:8px" },
        button("添加通道", () => addChannel(), { icon: "plus" }),
        button("重置通道", () => resetChannels(), { icon: "rotate", title: "清除独奏/静音并把主控归 1（强度值保持）" })),
      soloAllBtn,
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "AI 自动打光"),
        h("span", { class: "chip tiny" }, "描述 → 光照节点")),
      aiArea,
      h("div", { class: "row", style: "gap:8px" }, aiBtn),
      aiNoteEl,
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "参考图迁移"),
        h("span", { class: "chip tiny" }, "保守微调")),
      refSlot,
      refInput,
      h("div", { class: "divider" }),
      outputLine,
    ),
  );

  // ------------------------------------------------------------ 渲染调度

  function scheduleRender(): void {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => { timer = null; void doRender(); }, 220);
  }

  async function doRender(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) {
      if (store.ctx.videoId) await loadVideoFrame();
      return;
    }
    if (fading || renderBusy) { dirty = true; return; }
    renderBusy = true;
    store.set({ status: "渲染中…" });
    try {
      const res = await api.render(imageId, engineParams(currentParams()));
      lastRenderUrl = pngUrl(res.png_b64);
      lastSize = { w: res.width, h: res.height };
      if (!showOriginal) stack.setImage(lastRenderUrl);
      store.set({
        lastRender: lastRenderUrl,
        backend: res.backend,
        fromCache: res.from_cache,
        status: `渲染完成 · ${res.elapsed.toFixed(2)} s · 深度来源 ${res.backend}${res.from_cache ? " · 缓存命中" : ""}`,
      });
      renderOutput();
      renderImageInfo();
    } catch (e) {
      reportError(e);
      store.set({ status: "渲染失败" });
    } finally {
      renderBusy = false;
      if (dirty) { dirty = false; scheduleRender(); }
    }
  }

  async function loadVideoFrame(): Promise<void> {
    const videoId = store.ctx.videoId;
    if (!videoId) return;
    try {
      const res = await api.videoFrame(videoId, store.ctx.frameIndex);
      if (!showOriginal) stack.setImage(pngUrl(res.png_b64));
      lastSize = { w: res.width, h: res.height };
      renderOutput();
      renderImageInfo();
    } catch (e) {
      reportError(e);
    }
  }

  function renderImageInfo(): void {
    const kind = store.ctx.mediaKind;
    const name = store.ctx.mediaName || "未载入媒体";
    const dim = lastSize ? `${lastSize.w} × ${lastSize.h}` : store.ctx.videoInfo
      ? `${store.ctx.videoInfo.width} × ${store.ctx.videoInfo.height}`
      : "—";
    clear(imageInfo).append(
      icon(kind === "video" ? "film" : "image", 12, "var(--hls-accent)"),
      h("span", {}, `${name} · ${dim} · ${kind === "video" ? `视频第 ${store.ctx.frameIndex + 1} 帧` : kind === "image" ? "图片模式" : "未载入"}`),
    );
  }

  function renderOutput(): void {
    const p = currentParams();
    const n = p.lights.length;
    const ambient = `${Math.round(p.ambient_intensity * 100)}%`;
    if (store.ctx.mediaKind === "video") {
      clear(outputLine).append(
        h("span", {}, `输出：视频模式 · 调光台仅显示当前帧（${n} 通道 · 环境 ${ambient}）`),
        h("div", { class: "tiny muted" }, "逐帧视频处理请在「视频调光」中进行；本屏幕的通道/CUE 会同步到全部屏幕。"),
      );
      return;
    }
    const size = lastSize ? `${lastSize.w} × ${lastSize.h}` : "—";
    clear(outputLine).append(
      h("span", {}, `输出：PNG · ${size} · ${n} 通道 · 环境 ${ambient}`),
      h("div", { class: "tiny muted" }, "强度/色温为引擎相对标度（0–4 · K）；照度 lx、CRI、Δuv、DMX、灯具温度本引擎无法计算，故不显示。"),
    );
  }

  // ------------------------------------------------------------ CUE

  async function reloadCues(): Promise<void> {
    try {
      const r = await api.cues();
      cues = r.cues;
    } catch (e) {
      cues = [];
      reportError(e);
    }
    if (activeCueId && !cues.some((c) => c.id === activeCueId)) activeCueId = null;
    if (selectedCueId && !cues.some((c) => c.id === selectedCueId)) selectedCueId = null;
    renderCueList();
    renderCueNow();
  }

  function renderCueList(): void {
    cueCountChip.textContent = `${cues.length} 预设`;
    clear(cueList);
    if (!cues.length) {
      cueList.append(h("div", { class: "card small muted" }, "还没有 CUE。调整好光照后用下方按钮保存当前参数。"));
      return;
    }
    cues.forEach((c, i) => {
      const active = c.id === activeCueId;
      const selected = c.id === selectedCueId;
      const go = button("载入", () => void applyCue(c), { icon: "play", variant: active ? "primary" : undefined });
      const del = button("", () => void deleteCue(c), { icon: "trash", variant: "icon", title: `删除 ${c.name}` });
      // 载入/删除按钮的动作优先于整行选择：阻止冒泡，避免行选择把淡变时长
      // 改成该 CUE 的存储值而覆盖用户刚设的淡变。
      go.addEventListener("click", (e) => e.stopPropagation());
      del.addEventListener("click", (e) => e.stopPropagation());
      const row = h("div", {
        class: `item${active ? " on" : ""}`,
        style: `display:flex;flex-direction:column;gap:5px${selected && !active ? ";border-color:var(--hls-accent-line)" : ""}`,
      },
        h("div", { class: "row", style: "gap:6px" },
          h("span", { class: "mono tiny muted" }, String(i + 1).padStart(2, "0")),
          h("span", { class: "item__t grow ellipsis" }, c.name),
          active ? h("span", { class: "chip tiny" }, "已应用") : null,
          h("span", { class: "tiny muted" }, `淡变 ${c.fade.toFixed(1)}s`)),
        h("div", { class: "row", style: "gap:6px" },
          go, del,
          h("div", { class: "spacer" }),
          h("span", { class: "tiny muted" }, `${c.params.lights.length} 通道`)),
      );
      row.addEventListener("click", () => {
        selectedCueId = c.id;
        fade = c.fade;
        fadeRow.set(c.fade);
        renderCueList();
        renderCueNow();
      });
      cueList.append(row);
    });
  }

  function renderCueNow(): void {
    const active = cues.find((c) => c.id === activeCueId) ?? null;
    const idx = active ? cues.indexOf(active) + 1 : 0;
    clear(cueNow).append(
      h("span", {}, "当前 CUE："),
      h("b", {}, active ? `${String(idx).padStart(2, "0")} · ${active.name}` : "未应用"),
    );
    const sel = cues.find((c) => c.id === selectedCueId) ?? null;
    cuePhase.textContent = sel && sel.id !== activeCueId
      ? `已选择「${sel.name}」，按 GO 载入（淡变 ${fade.toFixed(1)}s）`
      : fade <= 0.02 ? "淡变 0s · 硬切" : `淡变 ${fade.toFixed(1)}s`;
  }

  async function saveCue(): Promise<void> {
    try {
      const r = await api.cueSave(cueNameInput.value.trim(), currentParams(), fade);
      cueNameInput.value = "";
      selectedCueId = r.cue.id;
      activeCueId = r.cue.id;
      await reloadCues();
      toast(`已保存 CUE「${r.cue.name}」`, "ok");
      store.set({ status: `已保存 CUE「${r.cue.name}」· 淡变 ${r.cue.fade.toFixed(1)}s · ${r.cue.params.lights.length} 通道` });
    } catch (e) {
      reportError(e);
    }
  }

  async function deleteCue(c: Cue): Promise<void> {
    try {
      await api.cueDelete(c.id);
      if (activeCueId === c.id) activeCueId = null;
      if (selectedCueId === c.id) selectedCueId = null;
      await reloadCues();
      toast(`已删除 CUE「${c.name}」`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  /** 淡变进度条：定时器只更新读数，溶解由 CSS 过渡完成（与帧率无关） */
  function runFadeProgress(seconds: number): void {
    if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null; }
    if (seconds <= 0.02) { cueBar.style.width = "100%"; return; }
    const t0 = Date.now();
    cueBar.style.width = "0%";
    progressTimer = window.setInterval(() => {
      const pct = Math.min(1, (Date.now() - t0) / (seconds * 1000));
      cueBar.style.width = `${Math.round(pct * 100)}%`;
      if (pct >= 1 && progressTimer !== null) {
        window.clearInterval(progressTimer);
        progressTimer = null;
      }
    }, 50);
  }

  function enqueue(fn: () => Promise<void>): void {
    chain = chain.then(fn).catch((e) => { reportError(e); });
  }

  async function applyCueNow(c: Cue): Promise<void> {
    const fadeSec = fade;
    const token = ++fadeToken;
    const idx = cues.indexOf(c) + 1;
    cuePhase.textContent = `载入「${c.name}」… 渲染目标`;
    const target = (await api.cueInterpolate(c.id, currentParams(), 1)).params;
    selfEdit = true;
    setParams(target);
    selfEdit = false;
    activeCueId = c.id;
    selectedCueId = c.id;
    selectedChannel = 0;
    // CUE 存的是绝对强度，应用后把主控比例归 1，避免在 CUE 绝对值上再叠一次缩放
    master = 1;
    masterBase = null;
    masterRow.set(1);
    renderChannels();
    drawPlot();
    renderOutput();
    renderCueList();
    renderCueNow();

    if (!store.ctx.imageId) {
      if (store.ctx.videoId) await loadVideoFrame();
      store.set({
        status: `已应用 CUE「${c.name}」（${target.lights.length} 通道 · 淡变 ${fadeSec.toFixed(1)}s）`,
      });
      runFadeProgress(fadeSec);
      return;
    }
    const res = await api.render(store.ctx.imageId, engineParams(target));
    if (token !== fadeToken) return;                    // 被更新的载入操作取代
    lastRenderUrl = pngUrl(res.png_b64);
    lastSize = { w: res.width, h: res.height };
    fading = true;
    runFadeProgress(fadeSec);
    try {
      if (fadeSec <= 0.02) stack.setImage(lastRenderUrl);
      else await stack.fadeTo(lastRenderUrl, fadeSec);
    } finally {
      fading = false;
    }
    if (token !== fadeToken) return;
    store.set({
      lastRender: lastRenderUrl,
      backend: res.backend,
      fromCache: res.from_cache,
      status: `已应用 CUE ${String(idx).padStart(2, "0")}「${c.name}」· 淡变 ${fadeSec.toFixed(1)}s · ${res.elapsed.toFixed(2)} s 渲染`,
    });
    renderImageInfo();
    renderOutput();
    if (dirty) { dirty = false; scheduleRender(); }
  }

  function applyCue(c: Cue): void {
    enqueue(() => applyCueNow(c));
  }

  function toggleCompare(): void {
    showOriginal = !showOriginal;
    compareBtn.classList.toggle("btn--on", showOriginal);
    cueOrigin.textContent = showOriginal ? "对比原图：开启（显示导入的原图/当前帧）" : "对比原图：关闭（显示当前渲染）";
    if (showOriginal) {
      stack.setImage(store.ctx.originalPng);
    } else if (lastRenderUrl) {
      stack.setImage(lastRenderUrl);
    } else {
      scheduleRender();
    }
  }

  // ------------------------------------------------------------ 通道

  function toggleSolo(name: string): void {
    if (soloed.has(name)) soloed.delete(name);
    else soloed.add(name);
    renderChannels();
    drawPlot(soloed.size === 1 ? name : null);
    scheduleRender();
  }

  function toggleAllSolo(): void {
    const list = lightsOf();
    if (soloed.size === list.length && list.length > 0) soloed.clear();
    else { soloed.clear(); for (const l of list) soloed.add(l.name); }
    renderChannels();
    drawPlot();
    scheduleRender();
  }

  function addChannel(): void {
    const list = lightsOf();
    let n = list.length + 1;
    while (list.some((l) => l.name === `通道 ${n}`)) n++;
    const light: Light = {
      name: `通道 ${n}`,
      kind: "point",
      dx: -0.5, dy: -0.6, dz: 0.7,
      px: 0.5, py: 0.5, pz: 1.2,
      intensity: 1,
      kelvin: 5600,
      radius: 0.35,
      visible: true,
      group: "",
    };
    updateLights([...list, light]);
    selectedChannel = list.length;
    renderChannels();
    drawPlot();
    scheduleRender();
    toast(`已添加通道 CH${String(list.length + 1).padStart(2, "0")}`, "ok");
  }

  function resetChannels(): void {
    soloed.clear();
    master = 1;
    masterBase = null;
    masterRow.set(1);
    updateLights(lightsOf().map((l) => ({ ...l, visible: true })));
    renderChannels();
    drawPlot();
    scheduleRender();
    toast("已清除独奏/静音并把主控归 1（强度值保持）", "ok");
  }

  function channelStrip(l: Light, i: number): HTMLElement {
    // 类型代号：优先用光源自带分组，未分组时按序号回退到固定角色表（界面显示用）
    const roleZh = (l.group ?? "").trim() || ROLE_ZH[i % ROLE_ZH.length];
    const roleEn = ROLE_EN[i % ROLE_EN.length];
    const soloBtn = button("S", () => toggleSolo(l.name), {
      variant: "ghost", on: soloed.has(l.name), title: "独奏：只让独奏通道参与渲染（界面本地，不写入参数）",
    });
    const muteBtn = button("M", () => {
      patchLight(l.name, { visible: !l.visible });
      renderChannels();
      drawPlot();
      scheduleRender();
    }, { variant: "ghost", on: !l.visible, title: "静音：写入引擎字段 visible=false" });
    for (const b of [soloBtn, muteBtn]) {
      b.style.padding = "2px 7px";
      b.style.minWidth = "26px";
      b.style.height = "22px";
    }
    const strip = h("div", {
      class: "card",
      style: `display:flex;flex-direction:column;gap:6px;${i === selectedChannel ? "border-color:var(--hls-accent-line);background:var(--hls-accent-tint)" : ""}`,
    },
      h("div", { class: "row", style: "gap:6px" },
        h("span", { class: "mono tiny muted" }, `CH${String(i + 1).padStart(2, "0")}`),
        h("i", { style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:${kelvinHex(l.kelvin)}` }),
        h("span", { class: "item__t grow ellipsis" }, l.name),
        h("span", { class: `chip tiny${l.visible ? "" : " muted"}` }, l.visible ? `${roleZh} ${roleEn}` : "静音"),
        soloBtn, muteBtn),
      slider({
        label: "强度",
        min: 0, max: INTENSITY_MAX, step: 0.05, value: l.intensity,
        format: (v) => `${Math.round((v / INTENSITY_MAX) * 100)}%`,
        onInput: (v) => {
          patchLight(l.name, { intensity: v });
          scheduleRender();
        },
      }),
      h("div", { class: "row row--between tiny muted" },
        h("span", {}, "相对标度 0–4"),
        h("span", {}, `${l.kind === "point" ? "点光" : "方向光"} · ${Math.round(l.kelvin)}K · 半径 ${l.radius.toFixed(2)}`)),
    );
    strip.addEventListener("pointerdown", () => {
      selectedChannel = i;
      drawPlot();
    });
    return strip;
  }

  function renderChannels(): void {
    const list = lightsOf();
    channelChip.textContent = `${list.length} 通道`;
    clear(channelList);
    if (!list.length) {
      channelList.append(h("div", { class: "card small muted" }, "当前参数里没有通道，可「添加通道」或到「智能打光」生成。"));
      return;
    }
    if (selectedChannel >= list.length) selectedChannel = list.length - 1;
    list.forEach((l, i) => channelList.append(channelStrip(l, i)));
    const allSoloed = list.length > 0 && soloed.size === list.length;
    soloAllBtn.classList.toggle("btn--on", allSoloed);
  }

  // ------------------------------------------------------------ AI 自动打光 / 参考图

  async function generateLight(): Promise<void> {
    const text = aiArea.value.trim();
    if (!text) { toast("请先输入光照描述", "error"); return; }
    aiBtn.disabled = true;
    store.set({ status: "正在解析光照描述…" });
    try {
      const res = await api.autolight(text, { base_params: currentParams() });
      selfEdit = true;
      setParams(res.params);
      selfEdit = false;
      aiNote = res.note || res.rig.note || "（后端未返回说明）";
      aiNoteEl.textContent = `${aiNote}（${res.source === "cloud" ? "云端解析" : "离线解析"} · ${res.params.lights.length} 个光照节点 · 已应用）`;
      soloed.clear();
      master = 1;
      masterBase = null;
      masterRow.set(1);
      renderChannels();
      drawPlot();
      scheduleRender();
      toast(`已生成 ${res.rig.lights.length} 个光照节点`, "ok");
      store.set({ status: `已生成 ${res.rig.lights.length} 个光照节点 · ${res.source === "cloud" ? "云端解析" : "离线解析"}` });
    } catch (e) {
      reportError(e);
      store.set({ status: "光照描述解析失败，已保留原有参数" });
    } finally {
      aiBtn.disabled = false;
    }
  }

  function renderRefSlot(): void {
    clear(refSlot);
    const card = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
      button("导入参考图提取主光方向", () => refInput.click(), { icon: "image" }),
      h("div", { class: "tiny muted" }, "仅做方向与色温迁移，保留原图明暗结构（需要先导入图片）。"),
    );
    if (refOffset) {
      const box = h("div", { class: "card", style: "background:var(--hls-bg-canvas);display:flex;flex-direction:column;gap:3px" });
      const rows = Object.entries(refOffset);
      if (!rows.length) box.append(h("div", { class: "small muted" }, "本次迁移未产生可量化的偏移"));
      for (const [k, v] of rows) {
        box.append(h("div", { class: "kv" },
          h("span", {}, OFFSET_LABELS[k.replace(/^d_/, "")] ?? k.replace(/^d_/, "")),
          h("b", {}, offsetValue(k, v))));
      }
      card.append(box, button("撤销迁移", () => {
        if (refUndo) { selfEdit = true; setParams(refUndo); selfEdit = false; }
        refUndo = null;
        refOffset = null;
        renderRefSlot();
        renderChannels();
        drawPlot();
        scheduleRender();
      }, { icon: "rotate", variant: "ghost" }));
    }
    refSlot.append(card);
  }

  async function transferReference(file: File): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) { toast("请先导入图片", "error"); return; }
    store.set({ status: "正在解析参考图光照…" });
    try {
      const b64 = await fileToB64(file);
      const res = await api.reference(imageId, currentParams(), b64);
      refUndo = structuredClone(currentParams());
      refOffset = res.offset;
      selfEdit = true;
      setParams(res.params);
      selfEdit = false;
      renderRefSlot();
      renderChannels();
      drawPlot();
      scheduleRender();
      store.set({ status: "已应用参考图光照" });
    } catch (e) {
      reportError(e);
      store.set({ status: "参考图迁移失败" });
    }
  }

  // ------------------------------------------------------------ 灯位图

  const toUv = (l: Light): { u: number; v: number } => {
    if (l.kind === "point") return { u: clamp(l.px, 0, 1), v: clamp(l.py, 0, 1) };
    const n = Math.hypot(l.dx, l.dy);
    const ux = n > 1e-6 ? l.dx / n : 0;
    const uy = n > 1e-6 ? l.dy / n : -1;
    return { u: clamp(0.5 + ux * 0.4, 0.02, 0.98), v: clamp(0.5 + uy * 0.4, 0.02, 0.98) };
  };

  let plot = { scale: 1, ox: 0, oy: 0, w: 0, h: 0 };

  function layoutPlot(): void {
    const w = plotCanvas.clientWidth || 480;
    const hgt = plotCanvas.clientHeight || 200;
    const dpr = window.devicePixelRatio || 1;
    plotCanvas.width = Math.round(w * dpr);
    plotCanvas.height = Math.round(hgt * dpr);
    const g = plotCanvas.getContext("2d");
    if (g) g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const scale = Math.min(w - 28, hgt - 28) * plotZoom;
    plot = { scale, ox: (w - scale) / 2, oy: (hgt - scale) / 2, w, h: hgt };
  }

  const uvToPx = (u: number, v: number): { x: number; y: number } =>
    ({ x: plot.ox + u * plot.scale, y: plot.oy + v * plot.scale });

  function drawPlot(highlightSolo: string | null = null): void {
    layoutPlot();
    const g = plotCanvas.getContext("2d");
    if (!g) return;
    const { w, h: hgt, scale } = plot;
    g.clearRect(0, 0, w, hgt);
    g.fillStyle = "#070910";
    g.fillRect(0, 0, w, hgt);

    // 地面网格（每 0.1 一格，0.5 为中线）
    for (let i = 0; i <= 10; i++) {
      const u = i / 10;
      g.strokeStyle = i === 5 ? "rgba(255,255,255,0.16)" : "rgba(255,255,255,0.06)";
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(uvToPx(u, 0).x, uvToPx(u, 0).y);
      g.lineTo(uvToPx(u, 1).x, uvToPx(u, 1).y);
      g.stroke();
      g.beginPath();
      g.moveTo(uvToPx(0, u).x, uvToPx(0, u).y);
      g.lineTo(uvToPx(1, u).x, uvToPx(1, u).y);
      g.stroke();
    }

    const subject = uvToPx(0.5, 0.5);
    g.strokeStyle = "rgba(255,255,255,0.35)";
    g.setLineDash([3, 4]);
    g.beginPath();
    g.arc(subject.x, subject.y, Math.max(10, 0.05 * scale), 0, Math.PI * 2);
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = "#e9eef8";
    g.beginPath();
    g.arc(subject.x, subject.y, 3, 0, Math.PI * 2);
    g.fill();
    g.font = "10px Inter, system-ui, sans-serif";
    g.textAlign = "center";
    g.fillStyle = "#98a5ba";
    g.fillText("被摄体", subject.x, subject.y + 20);

    const list = lightsOf();
    const nodes = list.map((l) => ({ l, ...uvToPx(toUv(l).u, toUv(l).v) }));

    for (const n of nodes) {
      const lit = n.l.visible && (soloed.size === 0 || soloed.has(n.l.name));
      g.strokeStyle = lit ? hexA(kelvinHex(n.l.kelvin), 0.5) : "rgba(255,255,255,0.12)";
      g.lineWidth = 1.2;
      g.setLineDash([4, 4]);
      g.beginPath();
      g.moveTo(subject.x, subject.y);
      g.lineTo(n.x, n.y);
      g.stroke();
      g.setLineDash([]);
    }

    for (const n of nodes) {
      const l = n.l;
      const lit = l.visible && (soloed.size === 0 || soloed.has(l.name));
      const r = 4 + (clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 7;
      const hex = kelvinHex(l.kelvin);
      const glow = g.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 3.2);
      glow.addColorStop(0, hexA(hex, lit ? 0.5 : 0.12));
      glow.addColorStop(1, hexA(hex, 0));
      g.fillStyle = glow;
      g.beginPath();
      g.arc(n.x, n.y, r * 3.2, 0, Math.PI * 2);
      g.fill();
      g.fillStyle = hexA(hex, lit ? 1 : 0.3);
      g.beginPath();
      g.arc(n.x, n.y, r, 0, Math.PI * 2);
      g.fill();
      const isSel = nodes[selectedChannel]?.l.name === l.name;
      const isSolo = highlightSolo === l.name || (soloed.size > 0 && soloed.has(l.name));
      g.strokeStyle = isSel ? "#6fa8ff" : isSolo ? "#ffd54a" : "rgba(0,0,0,0.35)";
      g.lineWidth = isSel ? 2 : 1;
      g.beginPath();
      g.arc(n.x, n.y, r + 2, 0, Math.PI * 2);
      g.stroke();
      const label = `${l.name} ${Math.round((clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 100)}%`;
      g.font = "10px Inter, system-ui, sans-serif";
      g.fillStyle = "rgba(10,13,19,0.82)";
      const tw = g.measureText(label).width + 12;
      g.fillRect(n.x - tw / 2, n.y + r + 5, tw, 15);
      g.fillStyle = lit ? "#e9eef8" : "#5e6a7e";
      g.fillText(label, n.x, n.y + r + 16);
    }

    if (!list.length) {
      g.fillStyle = "#5e6a7e";
      g.font = "12px Inter, system-ui, sans-serif";
      g.fillText("当前参数里没有光源", subject.x, subject.y - 34);
    }
  }

  function pickChannel(sx: number, sy: number): number {
    let best = -1;
    let bestD = 16;
    lightsOf().forEach((l, i) => {
      const p = uvToPx(toUv(l).u, toUv(l).v);
      const d = Math.hypot(p.x - sx, p.y - sy);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  plotCanvas.addEventListener("pointermove", (e) => {
    const r = plotCanvas.getBoundingClientRect();
    const u = (e.clientX - r.left - plot.ox) / plot.scale;
    const v = (e.clientY - r.top - plot.oy) / plot.scale;
    if (u < 0 || u > 1 || v < 0 || v > 1) {
      cursorReadout.textContent = "X —  Y —";
      return;
    }
    cursorReadout.textContent = `X ${u.toFixed(3)}  Y ${v.toFixed(3)}`;
  });
  plotCanvas.addEventListener("pointerleave", () => {
    cursorReadout.textContent = "X —  Y —";
  });
  plotCanvas.addEventListener("pointerdown", (e) => {
    const r = plotCanvas.getBoundingClientRect();
    const hit = pickChannel(e.clientX - r.left, e.clientY - r.top);
    if (hit >= 0) {
      selectedChannel = hit;
      renderChannels();
      drawPlot();
    }
  });
  plotCanvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    plotZoom = clamp(plotZoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), 0.5, 3);
    drawPlot();
  }, { passive: false });
  plotCanvas.addEventListener("dblclick", () => { plotZoom = 1; drawPlot(); });

  // ------------------------------------------------------------ 装配

  function syncFromStore(): void {
    const p = currentParams();
    if (p !== paramsRef) {
      paramsRef = p;
      if (!selfEdit) renderChannels();
      renderOutput();
      drawPlot();
    }
    // 媒体身份（图片/视频/帧号）变化时刷新画面
    const sig = `${store.ctx.mediaKind}|${store.ctx.imageId}|${store.ctx.videoId}|${store.ctx.frameIndex}`;
    if (sig !== mediaKey) {
      mediaKey = sig;
      renderImageInfo();
      if (store.ctx.imageId) {
        if (!showOriginal) scheduleRender();
      } else if (store.ctx.videoId) {
        void loadVideoFrame();
      } else {
        stack.setImage(null);
      }
    }
  }

  return {
    id: "desk",
    name: "调光台",
    subtitle: "CUE 预设场景 · 通道推子实时写回当前参数",
    icon: "sliders",
    mount(root: HTMLElement): void {
      clear(root).append(left, center, right);
      if (!store.ctx.params) {
        clear(root).append(h("div", { style: "padding:24px" },
          h("div", { class: "banner" }, "等待后端状态…请确认本地服务已启动。")));
        return;
      }
      paramsRef = currentParams();
      mediaKey = `${store.ctx.mediaKind}|${store.ctx.imageId}|${store.ctx.videoId}|${store.ctx.frameIndex}`;
      renderChannels();
      renderOutput();
      renderImageInfo();
      renderRefSlot();
      renderCueNow();
      drawPlot();
      zoomLabel.textContent = `${Math.round(stack.zoom * 100)}%`;
      stack.setImage(showOriginal ? store.ctx.originalPng : lastRenderUrl ?? store.ctx.originalPng);
      if (store.ctx.imageId) scheduleRender();
      else if (store.ctx.videoId) void loadVideoFrame();
      void reloadCues();

      paramsRef = currentParams();
      unsub = store.subscribe(() => syncFromStore());
      resizeObs = new ResizeObserver(() => drawPlot());
      resizeObs.observe(plotCanvas);
    },
    unmount(): void {
      if (timer !== null) { window.clearTimeout(timer); timer = null; }
      if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null; }
      fadeToken++;                                       // 取消进行中的溶解收尾
      unsub?.();
      unsub = null;
      resizeObs?.disconnect();
      resizeObs = null;
      renderBusy = false;
      dirty = false;
      fading = false;
    },
  };
}
