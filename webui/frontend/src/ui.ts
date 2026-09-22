/**
 * 通用 UI 原语与轻量状态容器。
 *
 * 刻意不引入框架：界面规模有限，直接操作 DOM 更省依赖、构建更快，
 * 也避免把“构建期 Node”变成“运行期必须 Node”的风险。
 * 所有屏幕共用这里的构造器，保证视觉与交互一致。
 */
import { api, ApiError, AppState, RenderParams, ThemeInfo, pngUrl } from "./api";

// ---------------------------------------------------------------- DOM 构造

type Attrs = Record<string, string | number | boolean | undefined | null>;
type Child = Node | string | null | undefined | false;

/** 创建元素：`h("div", { class: "card" }, "文本")` */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Attrs,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = String(v);
      else if (k === "html") node.innerHTML = String(v);
      else if (k.startsWith("data-") || k.startsWith("aria-")) node.setAttribute(k, String(v));
      else if (k in node) (node as unknown as Record<string, unknown>)[k] = v;
      else node.setAttribute(k, String(v));
    }
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

export function clear(node: HTMLElement): HTMLElement {
  node.replaceChildren();
  return node;
}

/** 图标（内联 SVG，取自 lucide 的路径风格，避免引入图标包） */
const ICONS: Record<string, string> = {
  folder: "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",
  layers: "m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z M2 12l8.58 3.91a2 2 0 0 0 1.66 0L21 12 M2 17l8.58 3.91a2 2 0 0 0 1.66 0L21 17",
  lightbulb: "M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5 M9 18h6 M10 22h4",
  sparkles: "M9.9 2.6 8.5 6.2 4.9 7.6 8.5 9 9.9 12.6 11.3 9 14.9 7.6 11.3 6.2Z M18 14l-.9 2.4-2.4.9 2.4.9.9 2.4.9-2.4 2.4-.9-2.4-.9Z M5 15l-.7 1.8-1.8.7 1.8.7L5 20l.7-1.8 1.8-.7-1.8-.7Z",
  workflow: "M3 3v18h18 M7 16v-5 M11 16V8 M15 16v-3 M19 16V6",
  wand: "m15 4 5 5L9 20H4v-5Z M14 5l5 5",
  puzzle: "M15.4 3a3 3 0 0 1 3 3v.5h.6a2 2 0 0 1 2 2v.6H21a3 3 0 0 1 0 6h-.5v.6a2 2 0 0 1-2 2H18v.6a3 3 0 0 1-6 0v-.6h-.6a2 2 0 0 1-2-2v-.6H9a3 3 0 0 1 0-6h.4v-.6a2 2 0 0 1 2-2h.5V6a3 3 0 0 1 3-3Z",
  palette: "M12 22a10 10 0 1 1 0-20c5.5 0 10 4 10 9 0 2.2-1.8 4-4 4h-1.6a1.9 1.9 0 0 0-1.4 3.2A1.9 1.9 0 0 1 12 22Z M7.5 11.5h.01 M11 8h.01 M15.5 9.5h.01",
  settings: "M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.6a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7V20a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.6a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2Z M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
  sliders: "M4 21v-7 M4 10V3 M12 21v-9 M12 8V3 M20 21v-5 M20 12V3 M1 14h6 M9 8h6 M17 16h6",
  image: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z m21 15-5-5L5 21",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  play: "m6 3 14 9-14 9Z",
  pause: "M7 4h3v16H7Z M14 4h3v16h-3Z",
  plus: "M12 5v14 M5 12h14",
  minus: "M5 12h14",
  x: "M18 6 6 18 M6 6l12 12",
  check: "m4 12 5 5L20 6",
  trash: "M3 6h18 M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2 M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6",
  crosshair: "M12 2v4 M12 18v4 M2 12h4 M18 12h4 M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z",
  rotate: "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
  film: "M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z M7 3v18 M17 3v18 M3 8h4 M17 8h4 M3 16h4 M17 16h4 M3 12h18",
  star: "m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1Z",
  gauge: "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z m12 12 4-4",
  radar: "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z M12 12 19 5 M12 12h.01",
  terminal: "m4 17 6-6-6-6 M12 19h8",
  bell: "M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8 M10.3 21a2 2 0 0 0 3.4 0",
  info: "M12 22a10 10 0 1 1 0-20 10 10 0 0 1 0 20Z M12 16v-4 M12 8h.01",
  fire: "M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5-1.4-1.1-2-2.7-2-4.5 0-1.4.5-2.7 1-3.5-3 0-6 2-6 5 0 1 .5 2 1 2.5-1.5 0-3 1.5-3 3.5a7 7 0 0 0 7 7Z",
  camera: "M14.5 4h-5L7 7H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-3Z M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
  power: "M18.4 6.6a9 9 0 1 1-12.7 0 M12 2v10",
};

export function icon(name: string, size = 14, color = "currentColor"): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", color);
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const d of (ICONS[name] ?? ICONS.info).split(" M").map((s, i) => (i === 0 ? s : "M" + s))) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

// ---------------------------------------------------------------- 控件

export function button(
  label: string,
  onClick: () => void,
  opts?: { icon?: string; variant?: "primary" | "ghost" | "danger" | "icon"; on?: boolean; title?: string },
): HTMLButtonElement {
  const cls = ["btn"];
  if (opts?.variant === "primary") cls.push("btn--primary");
  if (opts?.variant === "ghost") cls.push("btn--ghost");
  if (opts?.variant === "danger") cls.push("btn--danger");
  if (opts?.variant === "icon") cls.push("btn--icon");
  if (opts?.on) cls.push("btn--on");
  const b = h("button", { class: cls.join(" "), type: "button", title: opts?.title ?? label });
  if (opts?.icon) b.append(icon(opts.icon, 14));
  if (opts?.variant !== "icon") b.append(h("span", {}, label));
  b.addEventListener("click", onClick);
  return b;
}

/** 滑块行：标签 + 数值 + range */
export function slider(opts: {
  label: string;
  min: number; max: number; step?: number; value: number;
  format?: (v: number) => string;
  onInput: (v: number) => void;
}): HTMLElement {
  const fmt = opts.format ?? ((v: number) => String(Math.round(v * 100) / 100));
  const val = h("b", {}, fmt(opts.value));
  const input = h("input", {
    type: "range", min: opts.min, max: opts.max,
    step: opts.step ?? 1, value: opts.value,
  });
  input.addEventListener("input", () => {
    const v = Number(input.value);
    val.textContent = fmt(v);
    opts.onInput(v);
  });
  return h("div", { class: "slider-row" },
    h("div", { class: "slider-row__head" }, h("span", {}, opts.label), val),
    input,
  );
}

/** 分段控件（互斥选项） */
export function segmented<T extends string>(
  options: { id: T; label: string }[],
  active: T,
  onPick: (id: T) => void,
): HTMLElement {
  const wrap = h("div", { class: "seg" });
  for (const o of options) {
    const b = h("button", { type: "button", class: o.id === active ? "on" : "" }, o.label);
    b.addEventListener("click", () => onPick(o.id));
    wrap.append(b);
  }
  return wrap;
}

/** 数字输入（带范围限幅） */
export function numberField(
  label: string, value: number, min: number, max: number, step: number,
  onInput: (v: number) => void,
): HTMLElement {
  const input = h("input", { class: "input", type: "number", value, min, max, step });
  input.addEventListener("input", () => {
    const raw = Number(input.value);
    if (Number.isNaN(raw)) return;
    onInput(Math.min(max, Math.max(min, raw)));
  });
  return h("div", { class: "field" }, h("label", {}, label), input);
}

export function toast(message: string, kind: "info" | "error" | "ok" = "info"): void {
  const color = kind === "error" ? "#f87171" : kind === "ok" ? "var(--hls-success)" : "var(--hls-accent)";
  const box = h("div", {
    style: `position:fixed;left:50%;top:22px;transform:translateX(-50%);z-index:200;
            background:var(--hls-bg-elevated);border:1px solid ${color};color:var(--hls-text-primary);
            padding:9px 14px;border-radius:8px;font-size:12px;max-width:70vw;box-shadow:0 8px 30px #000a`,
  }, message);
  document.body.append(box);
  setTimeout(() => box.remove(), kind === "error" ? 5200 : 2600);
}

export function reportError(e: unknown): void {
  const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
  toast(msg, "error");
}

// ---------------------------------------------------------------- 全局状态

export interface AppCtx {
  state: AppState | null;
  themeId: string;
  themes: ThemeInfo[];
  params: RenderParams;
  /** 当前媒体 */
  imageId: string | null;
  videoId: string | null;
  mediaName: string;
  mediaKind: "image" | "video" | null;
  videoInfo: { width: number; height: number; fps: number; duration: number; frame_count: number; has_audio: boolean } | null;
  frameIndex: number;
  originalPng: string | null;
  lastRender: string | null;
  depthPng: string | null;
  backend: string;
  fromCache: boolean;
  status: string;
}

type Listener = () => void;

class Store {
  ctx: AppCtx = {
    state: null, themeId: "blue", themes: [], params: null as unknown as RenderParams,
    imageId: null, videoId: null, mediaName: "", mediaKind: null, videoInfo: null,
    frameIndex: 0, originalPng: null, lastRender: null, depthPng: null,
    backend: "", fromCache: false, status: "就绪",
  };
  #listeners = new Set<Listener>();

  subscribe(fn: Listener): () => void {
    this.#listeners.add(fn);
    return () => this.#listeners.delete(fn);
  }

  set(patch: Partial<AppCtx>): void {
    Object.assign(this.ctx, patch);
    for (const fn of this.#listeners) fn();
  }
}

export const store = new Store();

/** 应用主题：把 /api/theme/{id} 的令牌与变量写到 <html>，并持久化选择 */
export async function applyTheme(id: string): Promise<void> {
  const t = await api.theme(id);
  const root = document.documentElement;
  for (const [name, value] of Object.entries(t.tokens)) {
    root.style.setProperty(`--hls-${name}`, value);
  }
  // 非颜色变量（字体/圆角/描边/密度）同样来自主题引擎；
  // 旧后端未提供时保持默认，不影响换肤。
  if (t.variables) {
    for (const [name, value] of Object.entries(t.variables)) {
      root.style.setProperty(`--hls-${name}`, value);
    }
  }
  document.documentElement.dataset.themeDark = t.dark ? "1" : "0";
  store.set({ themeId: id });
  localStorage.setItem("hls.theme", id);
  // 主题选择持久化到后端配置，重启后保持（失败不阻断界面换肤）
  try {
    await api.setTheme(id);
  } catch { /* 离线或旧后端：仍已在本地生效 */ }
}

export async function loadState(): Promise<void> {
  const s = await api.state();
  store.set({
    state: s,
    params: s.params,
    themes: s.themes?.themes ?? [],
    themeId: s.themes?.current ?? s.themes?.default ?? "blue",
  });
  // 优先级：后端已保存的主题 > 本机记忆 > 默认。
  // 必须优先后端：打包版由 desktop.py 自动选端口，端口一变 localStorage
  // 就是另一个来源域，只靠本地记忆会导致主题悄悄回到默认。
  const saved = localStorage.getItem("hls.theme");
  await applyTheme(s.themes?.current || saved || s.themes?.default || "blue");
}

// ---------------------------------------------------------------- 3D 轨迹球

/** 拖拽球面上的手柄设置光源方向（dz 恒 > 0，光源始终在朝向观察者的半球） */
export class Trackball {
  readonly el: HTMLDivElement;
  #canvas: HTMLCanvasElement;
  #dx: number; #dy: number; #dz: number;
  #drag = false;
  #onChange: (dx: number, dy: number, dz: number) => void;

  constructor(dx: number, dy: number, dz: number, onChange: (dx: number, dy: number, dz: number) => void) {
    this.#dx = dx; this.#dy = dy; this.#dz = dz;
    this.#onChange = onChange;
    this.#canvas = h("canvas");
    this.el = h("div", { class: "trackball" }, this.#canvas);
    this.#canvas.addEventListener("pointerdown", (e) => { this.#drag = true; this.#apply(e); });
    this.#canvas.addEventListener("pointermove", (e) => { if (this.#drag) this.#apply(e); });
    window.addEventListener("pointerup", () => { this.#drag = false; });
    window.addEventListener("resize", () => this.draw());
  }

  set(dx: number, dy: number, dz: number): void {
    this.#dx = dx; this.#dy = dy; this.#dz = dz;
    this.draw();
  }

  #apply(e: PointerEvent): void {
    const r = this.#canvas.getBoundingClientRect();
    const cx = r.width / 2, cy = r.height / 2;
    const rad = Math.min(r.width, r.height) / 2 - 12;
    let x = (e.clientX - r.left - cx) / rad;
    let y = (e.clientY - r.top - cy) / rad;
    const d2 = x * x + y * y;
    if (d2 > 1) { const k = 1 / Math.sqrt(d2); x *= k; y *= k; }
    this.#dx = x;
    this.#dy = y;
    this.#dz = Math.sqrt(Math.max(1 - Math.min(d2, 1), 0.0016));
    this.draw();
    this.#onChange(this.#dx, this.#dy, this.#dz);
  }

  draw(): void {
    const dpr = window.devicePixelRatio || 1;
    const r = this.el.getBoundingClientRect();
    const w = Math.max(r.width, 60), hgt = Math.max(r.height, 60);
    this.#canvas.width = w * dpr; this.#canvas.height = hgt * dpr;
    const g = this.#canvas.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hgt);
    const cx = w / 2, cy = hgt / 2, rad = Math.min(w, hgt) / 2 - 12;
    const css = getComputedStyle(document.documentElement);
    const surface = css.getPropertyValue("--hls-bg-elevated").trim() || "#161c27";
    const border = css.getPropertyValue("--hls-border").trim() || "#1d2532";
    const textMuted = css.getPropertyValue("--hls-text-muted").trim() || "#5e6a7e";

    const grad = g.createRadialGradient(cx - rad * 0.35, cy - rad * 0.35, rad * 0.1, cx, cy, rad * 1.5);
    grad.addColorStop(0, surface);
    grad.addColorStop(1, "#0b0e13");
    g.beginPath(); g.arc(cx, cy, rad, 0, Math.PI * 2); g.fillStyle = grad; g.fill();
    g.strokeStyle = border; g.lineWidth = 1; g.stroke();

    g.strokeStyle = "rgba(255,255,255,0.14)";
    g.beginPath(); g.ellipse(cx, cy, rad, rad * 0.32, 0, 0, Math.PI * 2); g.stroke();
    g.beginPath(); g.ellipse(cx, cy, rad * 0.32, rad, 0, 0, Math.PI * 2); g.stroke();

    const px = cx + this.#dx * rad, py = cy + this.#dy * rad;
    g.strokeStyle = "#ffd54a"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(cx, cy); g.lineTo(px, py); g.stroke();
    g.beginPath(); g.arc(px, py, 7, 0, Math.PI * 2); g.fillStyle = "#ffd54a"; g.fill();

    g.fillStyle = textMuted; g.font = "10px ui-monospace, Consolas, monospace";
    g.textAlign = "center";
    g.fillText(`dx ${this.#dx.toFixed(2)}  dy ${this.#dy.toFixed(2)}  dz ${this.#dz.toFixed(2)}`, cx, hgt - 8);
  }
}

// ---------------------------------------------------------------- 画布

/** 图片画布：滚轮缩放、拖拽平移、可选点击拾取 */
export class CanvasView {
  readonly el: HTMLDivElement;
  #img: HTMLImageElement;
  #zoom = 1;
  #panX = 0; #panY = 0;
  #dragging = false;
  #lastX = 0; #lastY = 0;
  #pick = false;
  #onPick?: (x: number, y: number) => void;
  #onHover?: (x: number, y: number) => void;
  #naturalW = 0; #naturalH = 0;

  constructor() {
    this.#img = h("img", { class: "canvas-img", alt: "画布" });
    this.el = h("div", { class: "canvas-wrap" });
    this.el.append(this.#img);

    this.el.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.#zoom = Math.min(8, Math.max(0.05, this.#zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
      this.#img.style.transform = `translate(${this.#panX}px, ${this.#panY}px) scale(${this.#zoom})`;
    }, { passive: false });

    this.#img.addEventListener("pointerdown", (e) => {
      this.#dragging = true; this.#lastX = e.clientX; this.#lastY = e.clientY;
      this.#img.setPointerCapture(e.pointerId);
    });
    this.#img.addEventListener("pointermove", (e) => {
      if (this.#dragging) {
        this.#panX += e.clientX - this.#lastX;
        this.#panY += e.clientY - this.#lastY;
        this.#lastX = e.clientX; this.#lastY = e.clientY;
        this.#img.style.transform = `translate(${this.#panX}px, ${this.#panY}px) scale(${this.#zoom})`;
      }
      if (this.#onHover) {
        const p = this.#toImage(e);
        if (p) this.#onHover(p.x, p.y);
      }
    });
    this.#img.addEventListener("pointerup", (e) => {
      this.#dragging = false;
      if (this.#pick && this.#onPick) {
        const p = this.#toImage(e);
        if (p) this.#onPick(p.x, p.y);
      }
    });
  }

  #toImage(e: PointerEvent): { x: number; y: number } | null {
    const r = this.#img.getBoundingClientRect();
    const x = Math.floor(((e.clientX - r.left) / r.width) * this.#naturalW);
    const y = Math.floor(((e.clientY - r.top) / r.height) * this.#naturalH);
    if (x < 0 || y < 0 || x >= this.#naturalW || y >= this.#naturalH) return null;
    return { x, y };
  }

  setImage(dataUrl: string | null): void {
    if (!dataUrl) { this.#img.removeAttribute("src"); return; }
    this.#img.src = dataUrl;
    this.#img.onload = () => {
      this.#naturalW = this.#img.naturalWidth;
      this.#naturalH = this.#img.naturalHeight;
    };
  }

  setPicking(on: boolean): void {
    this.#pick = on;
    this.#img.classList.toggle("picking", on);
  }

  onPick(fn: (x: number, y: number) => void): void { this.#onPick = fn; }
  onHover(fn: (x: number, y: number) => void): void { this.#onHover = fn; }

  fit(): void {
    this.#zoom = 1; this.#panX = 0; this.#panY = 0;
    this.#img.style.transform = "none";
  }
}

export { pngUrl };
