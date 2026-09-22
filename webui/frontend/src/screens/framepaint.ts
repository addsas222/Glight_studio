/**
 * B3 逐帧光绘（Frame Painter）—— 在视频单帧上直接绘制发光笔触。
 *
 * 数据流（本屏幕持有 Map<number, Stroke[]> 光绘轨道，帧号是动态键，Map 合适）：
 *  - 预览：api.strokesPreview(video_id, frame, strokes)，由后端把笔触合成到真实帧上；
 *  - 导出：api.strokesExport({video_id, params, track}) → pollJob → api.strokesResultBlob(job_id)。
 *  - 导出的 track 与预览走**同一份** effective() 变换，保证「所见即所得」。
 *
 * Stroke 接口只有这些字段，界面旋钮的映射关系如下（其余一律只作界面值）：
 *  - 强度 → intensity，半径 → radius，色温 → kelvin，柔边 → softness
 *  - 混合模式 → blend，光束方向 → angle，光束张角 → spread，星芒条数 → spikes
 *  - 图层「不透明度」→ 乘到该层笔触的 intensity；「辉光扩散」→ 乘到 radius（真实参与合成）
 *  - 「闪烁频率」：Stroke 无时间维度字段，纯界面数值，不参与预览与导出；
 *  - 「压感曲线」：Stroke 无笔压字段，仅切换曲线示意图（线性 / S 曲线 / 快衰减），不改变引擎行为；
 *  - 「逐帧跟随 / 自动补间」：界面开关，当前引擎逐帧独立合成，不做自动补间。
 */
import { api, downloadBlobUrl, pngUrl, pollJob } from "../api";
import type { JobState, Stroke } from "../api";
import { button, clear, h, icon, reportError, segmented, slider, store, toast } from "../ui";
import { currentParams, type Screen } from "../shell";

type BrushType = Stroke["type"];
type Blend = Stroke["blend"];

const BRUSHES: { id: BrushType; name: string; sub: string; glyph: string }[] = [
  { id: "point", name: "点光源", sub: "锐利边缘·高聚集", glyph: "sun" },
  { id: "glow", name: "柔光晕", sub: "柔和扩散·氛围", glyph: "sparkles" },
  { id: "beam", name: "光束", sub: "定向投射·锥形", glyph: "wand" },
  { id: "starburst", name: "星芒", sub: "十字衍射·闪耀", glyph: "star" },
];

const BLEND_LABEL: Record<Blend, string> = { add: "叠加", screen: "滤色", soft: "柔光" };

/** 光色预设：色温预设，色值由色温推导（与 core/render.py 的近似式一致） */
const KELVIN_PRESETS: { k: number; label: string }[] = [
  { k: 2400, label: "暖钨丝" },
  { k: 3200, label: "暖白" },
  { k: 4300, label: "中性" },
  { k: 5600, label: "日光" },
  { k: 9800, label: "冷月" },
];

const ICON_EYE = ["M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6Z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"];
const ICON_EYE_OFF = [
  "M3 3l18 18",
  "M10.6 6.2A9.4 9.4 0 0 1 12 6c6.4 0 10 6 10 6a17.6 17.6 0 0 1-3.3 3.9",
  "M6.4 8.2A17.4 17.4 0 0 0 2 12s3.6 6 10 6c1.6 0 3-.3 4.3-.9",
];
const ICON_LOCK = ["M5 11h14v10H5Z", "M8 11V7a4 4 0 0 1 8 0v4"];

// ---------------------------------------------------------------- 小工具

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

function sleep(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  window.setTimeout(resolve, Math.max(0, ms));
  return promise;
}

/** 色温 → 十六进制色。同一近似式（Tanner Helland）见 core/render.py:kelvin_to_rgb，仅用于界面显示 */
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

/** hh:mm:ss:ff —— 由真实帧号与 fps 推导 */
function timecode(index: number, fps: number): string {
  const f = Math.max(0, Math.round(index));
  const r = Math.max(1, Math.round(fps));
  const totalSec = Math.floor(f / r);
  const pad = (v: number) => String(v).padStart(2, "0");
  return `${pad(Math.floor(totalSec / 3600))}:${pad(Math.floor(totalSec / 60) % 60)}:${pad(totalSec % 60)}:${pad(f % r)}`;
}

/** 本地内联图标（ui.ts 的 ICONS 里没有眼睛/锁，按约定就地内联） */
function svgIcon(paths: string[], size = 14, color = "currentColor"): SVGSVGElement {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("width", String(size));
  el.setAttribute("height", String(size));
  el.setAttribute("fill", "none");
  el.setAttribute("stroke", color);
  el.setAttribute("stroke-width", "1.7");
  el.setAttribute("stroke-linecap", "round");
  el.setAttribute("stroke-linejoin", "round");
  for (const d of paths) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    el.append(p);
  }
  return el;
}

// ---------------------------------------------------------------- 屏幕

export function createFramePaintScreen(): Screen {
  // -------- 跨挂载保留的状态 --------
  interface LayerUi { on: boolean; blend: Blend; opacity: number; glow: number; flicker: number }

  const track = new Map<number, Stroke[]>();
  /** 「新建」显式建立的空白绘制帧（时间轴用空心/虚线标记区分于有笔触的帧） */
  const created = new Set<number>();
  const layers: Record<BrushType, LayerUi> = {
    point: { on: true, blend: "add", opacity: 1, glow: 1, flicker: 0 },
    glow: { on: true, blend: "screen", opacity: 1, glow: 1, flicker: 0.12 },
    beam: { on: true, blend: "add", opacity: 1, glow: 1, flicker: 0 },
    starburst: { on: true, blend: "add", opacity: 1, glow: 1, flicker: 0 },
  };
  const brush = { type: "glow" as BrushType, intensity: 1.8, radius: 0.18, kelvin: 5600, softness: 0.6, spikes: 6, spread: 30 };
  const ui = { curve: "s" as "linear" | "s" | "decay", onion: 0, snap: true, loop: true, onlyPainted: false };
  let selected: BrushType = "glow";
  let frame = 0;
  let playing = false;

  // -------- 运行时（每次挂载重建） --------
  let disposed = false;
  let previewTimer: number | null = null;
  let previewSeq = 0;
  let busy = false;
  let exporting = false;
  let loadingFrame = false;

  interface Dom {
    brushGrid: HTMLElement; brushProps: HTMLElement; swatches: HTMLElement; curveBox: HTMLElement;
    stage: HTMLElement; frameBox: HTMLElement; img: HTMLImageElement; overlay: HTMLCanvasElement;
    hud: HTMLElement; progress: HTMLElement; exportBtn: HTMLButtonElement;
    layers: HTMLElement; props: HTMLElement; strip: HTMLElement; ruler: HTMLCanvasElement;
    tracks: HTMLElement; transport: HTMLElement;
  }

  let el: Dom | null = null;
  let basePng: string | null = null;
  let previewPng: string | null = null;
  let thumbs: { frames: number[]; png: string[] } | null = null;
  let loadedVideo: string | null = null;
  let lastDrag: { x: number; y: number } | null = null;
  let dragStroke: Stroke | null = null;
  let painting = false;

  /** 视频总帧数（后端 frame_count，缺省按 0） */
  const frameCount = (): number => Math.max(store.ctx.videoInfo?.frame_count ?? 0, 0);
  /** 视频帧率（后端 fps，缺省 24） */
  const fps = (): number => Math.max(1, Math.round(store.ctx.videoInfo?.fps ?? 24));

  // -------- 轨道变换（预览与导出共用） --------

  /** 当前帧实际参与合成的笔触（滤掉隐藏图层，叠加图层增益） */
  function effective(f: number): Stroke[] {
    const raw = track.get(f);
    if (!raw) return [];
    const out: Stroke[] = [];
    for (const s of raw) {
      const L = layers[s.type];
      if (!L.on) continue;
      out.push({ ...s, intensity: clamp(s.intensity * L.opacity, 0, 4), radius: clamp(s.radius * L.glow, 0.01, 4) });
    }
    return out;
  }

  /** 导出用的轨道：与 effective() 完全一致，帧号转成字符串键交给后端 */
  function buildTrack(): Record<string, Stroke[]> {
    const out = new Map<number, Stroke[]>();
    for (const f of track.keys()) {
      const list = effective(f);
      if (list.length) out.set(f, list);
    }
    return Object.fromEntries(out);
  }

  /** 已绘制或已显式新建的帧（时间轴刻度与跳帧用） */
  function paintedFrames(): number[] {
    const set = new Set<number>(created);
    for (const [f, list] of track) if (list.length) set.add(f);
    return [...set].sort((a, b) => a - b);
  }

  function paintedCount(type: BrushType): number {
    let n = 0;
    for (const list of track.values()) if (list.some((s) => s.type === type)) n += 1;
    return n;
  }

  // -------- 叠加层绘制（界面反馈） --------

  /** 把一条笔触画到叠加层（真实合成由后端返回的预览图承担，这里只做即时反馈） */
  function paintStroke(g: CanvasRenderingContext2D, s: Stroke, w: number, hgt: number, alpha: number): void {
    const cx = s.x * w;
    const cy = s.y * hgt;
    const r = s.radius * Math.max(w, hgt);
    if (r <= 0) return;
    const hex = kelvinHex(s.kelvin);
    const a = clamp((s.intensity / 4) * 0.75 * alpha, 0, 1);
    // Stroke 的 angle/spread/spikes 是可选字段，缺省值与 core/strokes.py 一致
    const angle = s.angle ?? 0;
    if (s.type === "beam") {
      const ang = (angle * Math.PI) / 180;
      const half = (((s.spread ?? 30) * 0.5) * Math.PI) / 180;
      const grad = g.createRadialGradient(cx, cy, 0, cx, cy, r);
      grad.addColorStop(0, hexA(hex, a));
      grad.addColorStop(1, hexA(hex, 0));
      g.fillStyle = grad;
      g.beginPath();
      g.moveTo(cx, cy);
      g.arc(cx, cy, r, ang - half, ang + half);
      g.closePath();
      g.fill();
      return;
    }
    if (s.type === "starburst") {
      g.strokeStyle = hexA(hex, a);
      g.lineWidth = 1.4;
      const spikes = Math.max(2, Math.round(s.spikes ?? 6));
      for (let i = 0; i < spikes; i += 1) {
        const ang = (i / spikes) * Math.PI * 2 + (angle * Math.PI) / 180;
        g.beginPath();
        g.moveTo(cx - Math.cos(ang) * r, cy - Math.sin(ang) * r);
        g.lineTo(cx + Math.cos(ang) * r, cy + Math.sin(ang) * r);
        g.stroke();
      }
      return;
    }
    const grad = g.createRadialGradient(cx, cy, 0, cx, cy, r);
    if (s.type === "point") {
      grad.addColorStop(0, hexA(hex, a));
      grad.addColorStop(0.55, hexA(hex, a * 0.7));
    } else {
      grad.addColorStop(0, hexA(hex, a * 0.6));
      grad.addColorStop(0.45, hexA(hex, a * 0.4));
    }
    grad.addColorStop(1, hexA(hex, 0));
    g.fillStyle = grad;
    g.beginPath();
    g.arc(cx, cy, r, 0, Math.PI * 2);
    g.fill();
  }

  function drawOverlay(): void {
    if (!el) return;
    const g = el.overlay.getContext("2d");
    if (!g) return;
    // 叠加层的 ctx 已按 dpr 变换，所有绘制坐标必须用 CSS 像素（clientWidth/Height）
    const w = el.overlay.clientWidth;
    const hgt = el.overlay.clientHeight;
    if (!w || !hgt) return;
    g.clearRect(0, 0, w, hgt);
    // 洋葱皮：以低透明度画出相邻帧的真实笔触（不是估算出来的画面）
    const onion = ui.onion / 100;
    if (onion > 0) {
      for (const f of [frame - 1, frame + 1]) {
        for (const s of track.get(f) ?? []) {
          if (layers[s.type].on) paintStroke(g, s, w, hgt, onion * 0.5);
        }
      }
    }
    for (const s of effective(frame)) paintStroke(g, s, w, hgt, 1);
  }

  function layoutStage(): void {
    if (!el) return;
    const info = store.ctx.videoInfo;
    const availW = el.stage.clientWidth - 26;
    const availH = el.stage.clientHeight - 26;
    if (!info || !info.width || !info.height || availW <= 0 || availH <= 0) return;
    const scale = Math.min(availW / info.width, availH / info.height);
    const w = Math.max(120, Math.floor(info.width * scale));
    const hgt = Math.max(80, Math.floor(info.height * scale));
    el.frameBox.style.width = `${w}px`;
    el.frameBox.style.height = `${hgt}px`;
    const dpr = window.devicePixelRatio || 1;
    el.overlay.width = Math.round(w * dpr);
    el.overlay.height = Math.round(hgt * dpr);
    el.overlay.style.width = `${w}px`;
    el.overlay.style.height = `${hgt}px`;
    const g = el.overlay.getContext("2d");
    if (g) g.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawOverlay();
  }

  // -------- 预览与帧载入 --------

  async function refreshPreview(): Promise<void> {
    const id = store.ctx.videoId;
    if (!id || !el || frameCount() === 0) return;
    const seq = (previewSeq += 1);
    busy = true;
    renderHud();
    try {
      const r = await api.strokesPreview(id, frame, effective(frame));
      if (disposed || seq !== previewSeq) return;
      previewPng = r.png_b64;
      el.img.src = pngUrl(previewPng);
    } catch (e) {
      if (seq === previewSeq) reportError(e);
    } finally {
      if (seq === previewSeq) {
        busy = false;
        renderHud();
      }
    }
  }

  function debouncePreview(): void {
    if (previewTimer !== null) window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(() => {
      previewTimer = null;
      void refreshPreview();
    }, 260);
  }

  async function loadFrame(): Promise<void> {
    const id = store.ctx.videoId;
    if (!id || !el || loadingFrame || frameCount() === 0) return;
    loadingFrame = true;
    renderHud();
    try {
      const r = await api.videoFrame(id, clamp(frame, 0, frameCount() - 1));
      if (disposed) return;
      basePng = r.png_b64;
      previewPng = null;
      el.img.src = pngUrl(basePng);
      layoutStage();
      void refreshPreview();
    } catch (e) {
      reportError(e);
    } finally {
      loadingFrame = false;
      renderHud();
    }
  }

  async function loadThumbs(): Promise<void> {
    const id = store.ctx.videoId;
    if (!id || !el) return;
    try {
      const r = await api.videoThumbnails(id, 14, 96);
      if (disposed) return;
      thumbs = { frames: r.frames, png: r.png_b64 };
      renderStrip();
    } catch {
      /* 缩略图失败不影响绘制主流程：静默保留空条 */
    }
  }

  async function bootstrap(): Promise<void> {
    if (!el) return;
    const id = store.ctx.videoId;
    if (!id) {
      loadedVideo = null;
      basePng = null;
      previewPng = null;
      thumbs = null;
      el.img.removeAttribute("src");
      el.frameBox.style.width = "0px";
      el.frameBox.style.height = "0px";
      renderHud();
      renderStrip();
      renderTimeline();
      return;
    }
    if (loadedVideo !== id) {
      loadedVideo = id;
      track.clear();
      created.clear();
      thumbs = null;
      frame = clamp(frame, 0, Math.max(frameCount() - 1, 0));
      void loadThumbs();
    }
    await loadFrame();
  }

  // -------- 在帧上绘制 --------

  function toNorm(e: PointerEvent): { x: number; y: number } | null {
    if (!el) return null;
    const r = el.img.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    if (x < 0 || y < 0 || x > 1 || y > 1) return null;
    return { x, y };
  }

  function makeStroke(x: number, y: number): Stroke {
    return {
      type: brush.type,
      x: clamp(x, 0, 1),
      y: clamp(y, 0, 1),
      radius: brush.radius,
      intensity: brush.intensity,
      kelvin: brush.kelvin,
      softness: brush.softness,
      blend: layers[brush.type].blend,
      angle: 0,
      spread: brush.spread,
      spikes: brush.spikes,
    };
  }

  /** 取当前帧的笔触数组（不存在则建空数组并登记） */
  function strokesOf(f: number): Stroke[] {
    const list = track.get(f);
    if (list) return list;
    const fresh: Stroke[] = [];
    track.set(f, fresh);
    return fresh;
  }

  function onDown(e: PointerEvent): void {
    if (!store.ctx.videoId || frameCount() === 0 || exporting || !el) return;
    const p = toNorm(e);
    if (!p) return;
    e.preventDefault();
    // 指针捕获是尽力而为：捕获失败（指针已失效）不影响后续 pointermove 继续绘制
    try {
      el.overlay.setPointerCapture(e.pointerId);
    } catch {
      /* 忽略：无捕获时仍可通过元素上的 pointermove 继续 */
    }
    painting = true;
    lastDrag = p;
    const s = makeStroke(p.x, p.y);
    if (brush.type === "beam") dragStroke = s;
    strokesOf(frame).push(s);
    created.add(frame);
    drawOverlay();
    debouncePreview();
  }

  function onMove(e: PointerEvent): void {
    if (!painting) return;
    const p = toNorm(e);
    if (!p) return;
    if (brush.type === "beam" && dragStroke) {
      // 光束：拖拽方向 = 投射方向（angle），拖拽长度 = 光束长度（radius 为归一化长度）
      const dx = p.x - dragStroke.x;
      const dy = p.y - dragStroke.y;
      dragStroke.angle = ((Math.atan2(dy, dx) * 180) / Math.PI + 360) % 360;
      dragStroke.radius = clamp(Math.hypot(dx, dy), 0.01, 0.6);
    } else {
      if (!lastDrag) return;
      if (Math.hypot(p.x - lastDrag.x, p.y - lastDrag.y) < Math.max(0.012, brush.radius * 0.3)) return;
      lastDrag = p;
      strokesOf(frame).push(makeStroke(p.x, p.y));
    }
    drawOverlay();
    debouncePreview();
  }

  function onUp(e: PointerEvent): void {
    if (!painting || !el) return;
    painting = false;
    dragStroke = null;
    lastDrag = null;
    if (el.overlay.hasPointerCapture(e.pointerId)) el.overlay.releasePointerCapture(e.pointerId);
    if (previewTimer !== null) {
      window.clearTimeout(previewTimer);
      previewTimer = null;
    }
    drawOverlay();
    renderTimeline();
    void refreshPreview();
  }

  // -------- 帧跳转 / 帧操作 --------

  async function seek(target: number, reload = true): Promise<void> {
    const total = frameCount();
    if (total <= 0) return;
    let next = clamp(Math.round(target), 0, total - 1);
    const list = paintedFrames();
    if (ui.onlyPainted && list.length) {
      next = list.reduce((a, b) => (Math.abs(b - next) < Math.abs(a - next) ? b : a), list[0]);
    } else if (ui.snap && list.length) {
      const near = list.find((f) => Math.abs(f - next) <= 2);
      if (near !== undefined) next = near;
    }
    if (next === frame && basePng) {
      renderTransport();
      renderTimeline();
      return;
    }
    frame = next;
    store.set({ frameIndex: frame });
    renderHud();
    renderTransport();
    renderTimeline();
    drawOverlay();
    if (reload) await loadFrame();
  }

  function newBlankFrame(): void {
    if (frameCount() === 0) {
      toast("请先打开一段视频", "error");
      return;
    }
    created.add(frame);
    if (!track.has(frame)) track.set(frame, []);
    drawOverlay();
    renderTimeline();
    toast(`已在帧 ${frame} 建立空白光绘标记`, "ok");
  }

  function clearFrame(): void {
    if (!track.has(frame) && !created.has(frame)) return;
    track.delete(frame);
    created.delete(frame);
    drawOverlay();
    renderTimeline();
    void refreshPreview();
    toast(`已清除帧 ${frame} 的光绘`, "info");
  }

  // -------- 导出 --------

  async function exportSequence(): Promise<void> {
    const id = store.ctx.videoId;
    if (!id || exporting) return;
    const payload = buildTrack();
    if (!Object.keys(payload).length) {
      toast("轨道为空：请先在帧上绘制笔触", "error");
      return;
    }
    exporting = true;
    renderHud();
    try {
      const { job_id } = await api.strokesExport({ video_id: id, params: currentParams(), track: payload });
      const done: JobState = await pollJob(job_id, (s) => {
        if (!el) return;
        clear(el.progress).append(
          h("div", { class: "bar" }, h("i", { style: `width:${Math.round(s.progress * 100)}%` })),
          h("div", { class: "tiny muted", style: "margin-top:4px" }, `${s.message} · ${Math.round(s.progress * 100)}%`),
        );
      });
      if (done.state !== "done") {
        toast(done.error || "导出未完成", "error");
        return;
      }
      const blob = await api.strokesResultBlob(job_id);
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, done.result?.download_name || "stroked.mp4");
      window.setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast(`导出完成：${done.result?.frames ?? 0} 帧`, "ok");
    } catch (e) {
      reportError(e);
    } finally {
      exporting = false;
      renderHud();
    }
  }

  // -------- 渲染：左栏 --------

  function renderBrushGrid(): void {
    if (!el) return;
    const grid = h("div", { class: "grid2" });
    for (const b of BRUSHES) {
      const on = brush.type === b.id;
      const card = h("button", {
        type: "button",
        style: `display:flex;flex-direction:column;gap:4px;align-items:flex-start;text-align:left;padding:9px 10px;cursor:pointer;
                background:${on ? "var(--hls-accent-tint)" : "var(--hls-bg-elevated)"};
                border:1px solid ${on ? "var(--hls-accent-line)" : "var(--hls-border)"};border-radius:var(--r-md)`,
        title: `${b.name} · ${b.sub}`,
      },
        icon(b.glyph, 15, on ? "var(--hls-accent-bright)" : "var(--hls-text-secondary)"),
        h("div", { style: "font-size:11.5px;font-weight:600" }, b.name),
        h("div", { class: "tiny muted" }, b.sub),
      );
      card.addEventListener("click", () => {
        brush.type = b.id;
        selected = b.id;
        renderBrushGrid();
        renderBrushProps();
        renderLayers();
        renderProps();
      });
      grid.append(card);
    }
    clear(el.brushGrid).append(
      grid,
      h("div", { class: "tiny muted", style: "margin-top:6px" },
        `笔触写入图层「${BRUSHES.find((b) => b.id === brush.type)?.name ?? ""}」`),
    );
  }

  function renderCurve(): void {
    if (!el) return;
    const cv = h("canvas", { width: 208, height: 84, style: "width:100%;border:1px solid var(--hls-border);border-radius:var(--r-sm)" });
    const g = cv.getContext("2d");
    if (g) {
      const w = cv.width;
      const hgt = cv.height;
      g.fillStyle = "#070910";
      g.fillRect(0, 0, w, hgt);
      g.strokeStyle = "rgba(255,255,255,0.08)";
      for (let i = 1; i < 4; i += 1) {
        g.beginPath();
        g.moveTo((w / 4) * i, 0);
        g.lineTo((w / 4) * i, hgt);
        g.stroke();
        g.beginPath();
        g.moveTo(0, (hgt / 4) * i);
        g.lineTo(w, (hgt / 4) * i);
        g.stroke();
      }
      const shape = (t: number): number => {
        if (ui.curve === "linear") return t;
        if (ui.curve === "s") return t * t * (3 - 2 * t);
        return Math.pow(t, 2.4);
      };
      g.strokeStyle = ui.curve === "decay" ? "#ffa94d" : "#6fa8ff";
      g.lineWidth = 2;
      g.beginPath();
      for (let i = 0; i <= w; i += 2) {
        const t = i / w;
        const y = hgt - shape(t) * (hgt - 8) - 4;
        if (i === 0) g.moveTo(i, y);
        else g.lineTo(i, y);
      }
      g.stroke();
      g.fillStyle = "rgba(255,255,255,0.45)";
      g.font = "9px ui-monospace, Consolas, monospace";
      g.fillText("笔压 →", 4, 11);
    }
    clear(el.curveBox).append(
      cv,
      segmented(
        [{ id: "linear", label: "线性" }, { id: "s", label: "S 曲线" }, { id: "decay", label: "快衰减" }],
        ui.curve,
        (id) => {
          ui.curve = id;
          renderCurve();
        },
      ),
      h("div", { class: "tiny muted" }, "压感曲线为界面预设值：当前引擎的笔触不含笔压字段，故不参与预览与导出。"),
    );
  }

  function renderSwatches(): void {
    if (!el) return;
    const row = h("div", { class: "row", style: "gap:7px;flex-wrap:wrap" });
    for (const p of KELVIN_PRESETS) {
      const hex = kelvinHex(p.k);
      const on = Math.abs(brush.kelvin - p.k) < 1;
      const dot = h("button", {
        type: "button",
        title: `${p.label} · ${p.k}K · ${hex}`,
        style: `width:24px;height:24px;border-radius:50%;background:${hex};cursor:pointer;
                border:2px solid ${on ? "var(--hls-accent-bright)" : "var(--hls-border-strong)"}`,
      });
      dot.addEventListener("click", () => {
        brush.kelvin = p.k;
        renderSwatches();
        renderBrushProps();
        renderLayers();
      });
      row.append(dot);
    }
    const hex = kelvinHex(brush.kelvin);
    row.append(h("div", { class: "chip", title: "由当前色温推导的显示色" },
      h("span", { style: `width:10px;height:10px;border-radius:50%;background:${hex}` }),
      `${hex} · ${Math.round(brush.kelvin)}K`));
    clear(el.swatches).append(
      row,
      h("div", { class: "tiny muted", style: "margin-top:4px" },
        "色温→色值为界面推导（与 core/render.py 同一近似式），非独立测量。"),
    );
  }

  function renderBrushProps(): void {
    if (!el) return;
    const box = clear(el.brushProps);
    box.append(slider({
      label: "强度", min: 0, max: 4, step: 0.05, value: brush.intensity,
      format: (v) => `${v.toFixed(2)} · ${Math.round((v / 4) * 100)}%`,
      onInput: (v) => { brush.intensity = v; },
    }));
    box.append(slider({
      label: "半径", min: 0.01, max: 0.6, step: 0.005, value: brush.radius,
      format: (v) => {
        const info = store.ctx.videoInfo;
        return info ? `${Math.round(v * Math.max(info.width, info.height))} px` : v.toFixed(3);
      },
      onInput: (v) => { brush.radius = v; },
    }));
    box.append(slider({
      label: "色温", min: 1500, max: 12000, step: 50, value: brush.kelvin,
      format: (v) => `${Math.round(v)} K`,
      onInput: (v) => { brush.kelvin = v; renderSwatches(); },
    }));
    box.append(slider({
      label: "柔边", min: 0, max: 1, step: 0.01, value: brush.softness,
      format: (v) => `${Math.round(v * 100)}%`,
      onInput: (v) => { brush.softness = v; },
    }));
    if (brush.type === "beam") {
      box.append(slider({
        label: "光束张角", min: 1, max: 180, step: 1, value: brush.spread,
        format: (v) => `${Math.round(v)}°`,
        onInput: (v) => { brush.spread = v; },
      }));
      box.append(h("div", { class: "tiny muted" }, "张角写入笔触 spread；绘制时光束方向由拖拽方向决定（angle）。"));
    }
    if (brush.type === "starburst") {
      box.append(slider({
        label: "星芒条数", min: 2, max: 24, step: 1, value: brush.spikes,
        format: (v) => `${Math.round(v)} 条`,
        onInput: (v) => { brush.spikes = v; },
      }));
    }
    const blendSel = h("select", { class: "select" });
    for (const b of Object.keys(BLEND_LABEL) as Blend[]) {
      blendSel.append(h("option", { value: b, selected: layers[brush.type].blend === b }, `${BLEND_LABEL[b]} ${b}`));
    }
    blendSel.addEventListener("change", () => setBlend(brush.type, blendSel.value as Blend));
    box.append(h("div", { class: "field" }, h("label", {}, "混合模式（该图层）"), blendSel));
    box.append(slider({
      label: "洋葱皮", min: 0, max: 100, step: 1, value: ui.onion,
      format: (v) => `${Math.round(v)}%`,
      onInput: (v) => { ui.onion = v; drawOverlay(); },
    }));
  }

  /** 混合模式写入该图层所有笔触（左右两处「混合模式」共用同一份图层状态） */
  function setBlend(type: BrushType, blend: Blend): void {
    layers[type].blend = blend;
    for (const list of track.values()) {
      for (const s of list) if (s.type === type) s.blend = blend;
    }
    renderBrushProps();
    renderProps();
    debouncePreview();
  }

  // -------- 渲染：右栏 --------

  function renderLayers(): void {
    if (!el) return;
    const box = clear(el.layers);
    box.append(h("div", { class: "row row--between", style: "margin-bottom:6px" },
      h("div", { class: "small sec" }, "光绘图层"),
      h("div", { class: "tiny muted mono" }, `关键帧 ${paintedFrames().length}`),
    ));
    for (const b of BRUSHES) {
      const L = layers[b.id];
      const on = selected === b.id;
      const eye = h("button", {
        type: "button", class: "btn btn--icon", title: L.on ? "隐藏该图层（预览与导出都排除）" : "显示该图层",
      }, svgIcon(L.on ? ICON_EYE : ICON_EYE_OFF, 14, L.on ? "var(--hls-text-secondary)" : "var(--hls-text-muted)"));
      eye.addEventListener("click", (ev) => {
        ev.stopPropagation();
        L.on = !L.on;
        renderLayers();
        drawOverlay();
        debouncePreview();
      });
      const row = h("div", {
        class: `item${on ? " on" : ""}`,
        style: `cursor:pointer;${L.on ? "" : "opacity:0.45"}`,
      },
        h("span", { style: `width:9px;height:9px;border-radius:50%;background:${kelvinHex(brush.kelvin)};flex:0 0 auto` }),
        h("div", { class: "grow" },
          h("div", { class: "item__t ellipsis" }, b.name),
          h("div", { class: "item__d" }, `${BLEND_LABEL[L.blend]} · ${paintedCount(b.id)} 帧`),
        ),
        eye,
      );
      row.addEventListener("click", () => {
        selected = b.id;
        renderBrushGrid();
        renderLayers();
        renderProps();
      });
      box.append(row);
    }
    box.append(h("div", { class: "item", style: "opacity:0.7;cursor:default" },
      icon("film", 13),
      h("div", { class: "grow" },
        h("div", { class: "item__t ellipsis" }, "底片 · 视频源"),
        h("div", { class: "item__d" }, store.ctx.videoInfo
          ? `${store.ctx.videoInfo.width}×${store.ctx.videoInfo.height} · ${fps()} fps`
          : "未载入"),
      ),
      svgIcon(ICON_LOCK, 13, "var(--hls-text-muted)"),
    ));
    box.append(h("div", { class: "tiny muted" }, "图层增益乘进笔触强度/半径，预览与导出共用同一变换。"));
  }

  function renderProps(): void {
    if (!el) return;
    const L = layers[selected];
    const name = BRUSHES.find((b) => b.id === selected)?.name ?? "";
    const has = track.get(frame)?.some((s) => s.type === selected) ?? false;
    const box = clear(el.props);
    box.append(h("div", { class: "row row--between" },
      h("div", { class: "small sec" }, `${name}属性`),
      h("div", { class: "tiny muted" }, `帧 ${frame} ${has ? "已打关键帧" : "无笔触"}`),
    ));
    const blendSel = h("select", { class: "select" });
    for (const b of Object.keys(BLEND_LABEL) as Blend[]) {
      blendSel.append(h("option", { value: b, selected: L.blend === b }, `${BLEND_LABEL[b]} ${b}`));
    }
    blendSel.addEventListener("change", () => setBlend(selected, blendSel.value as Blend));
    box.append(h("div", { class: "field" }, h("label", {}, "混合模式"), blendSel));
    box.append(slider({
      label: "不透明度", min: 0, max: 2, step: 0.01, value: L.opacity,
      format: (v) => `${Math.round(v * 100)}%`,
      onInput: (v) => { L.opacity = v; debouncePreview(); },
    }));
    box.append(slider({
      label: "辉光扩散", min: 0.2, max: 3, step: 0.01, value: L.glow,
      format: (v) => `×${v.toFixed(2)}`,
      onInput: (v) => { L.glow = v; drawOverlay(); debouncePreview(); },
    }));
    box.append(slider({
      label: "闪烁频率", min: 0, max: 1, step: 0.01, value: L.flicker,
      format: (v) => `${v.toFixed(2)} /帧`,
      onInput: (v) => { L.flicker = v; },
    }));
    box.append(h("div", { class: "tiny muted" },
      "不透明度 / 辉光扩散会乘进笔触强度与半径（预览与导出一致）；闪烁频率与下方两个开关是界面值：引擎逐帧独立合成，无逐帧时间轴字段。"));
    const followBox = h("input", { type: "checkbox", checked: true });
    const tweenBox = h("input", { type: "checkbox", checked: true });
    followBox.addEventListener("change", () => toast(followBox.checked ? "逐帧跟随：界面开关" : "已关闭逐帧跟随", "info"));
    tweenBox.addEventListener("change", () => toast(tweenBox.checked ? "自动补间：界面开关（不生成中间帧笔触）" : "已关闭自动补间", "info"));
    box.append(h("div", { class: "grid2" },
      h("label", { class: "row", style: "gap:6px;font-size:11px;cursor:pointer" }, followBox, "逐帧跟随"),
      h("label", { class: "row", style: "gap:6px;font-size:11px;cursor:pointer" }, tweenBox, "自动补间"),
    ));
    box.append(h("div", { class: "card" },
      h("div", { style: "font-size:11.5px;font-weight:600;margin-bottom:4px" }, "本帧概况"),
      h("div", { class: "kv" }, h("span", {}, "本帧笔触"), h("b", { class: "mono" }, String(track.get(frame)?.length ?? 0))),
      h("div", { class: "kv" }, h("span", {}, "轨道已绘帧"), h("b", { class: "mono" }, String(paintedFrames().length))),
      h("div", { class: "kv" }, h("span", {}, "隐藏图层"), h("b", { class: "mono" }, String(BRUSHES.filter((b) => !layers[b.id].on).length))),
    ));
  }

  // -------- 渲染：中部 --------

  function renderHud(): void {
    if (!el) return;
    const total = frameCount();
    el.exportBtn.disabled = exporting;
    clear(el.exportBtn);
    el.exportBtn.append(icon("download", 14), h("span", {}, exporting ? "导出中…" : "导出序列"));
    const parts: (Node | string)[] = [
      h("span", { class: "chip" },
        h("span", { class: `dot${playing ? "" : " dot--off"}` }),
        `帧 ${total ? frame : 0} / ${Math.max(total - 1, 0)}`),
      h("span", { class: "chip" }, `${fps()} FPS`),
      h("span", { class: "chip" }, store.ctx.videoInfo
        ? `${store.ctx.videoInfo.width}×${store.ctx.videoInfo.height}`
        : "未载入视频"),
      h("span", { class: "chip", title: "洋葱皮显示相邻帧的真实笔触" }, `洋葱皮 ${Math.round(ui.onion)}%`),
      h("div", { class: "spacer" }),
    ];
    if (loadingFrame) parts.push(h("span", { class: "tiny muted" }, "解码帧…"));
    if (busy) parts.push(h("span", { class: "tiny muted" }, "合成中…"));
    parts.push(h("span", { class: "tiny muted mono" }, timecode(frame, fps())));
    clear(el.hud).append(...parts);
  }

  // -------- 渲染：时间轴 --------

  function toggleChip(label: string, on: boolean, title: string, onChange: (v: boolean) => void): HTMLElement {
    const b = h("button", {
      type: "button",
      class: "chip",
      title,
      style: `cursor:pointer;${on ? "border-color:var(--hls-accent-line);color:var(--hls-text-primary)" : ""}`,
    }, h("span", { class: on ? "dot" : "dot dot--off" }), label);
    b.addEventListener("click", () => onChange(!on));
    return b;
  }

  function renderTransport(): void {
    if (!el) return;
    const stepBtn = (go: () => number, title: string, glyph: string, flip: boolean) => {
      const b = button("", () => void seek(go()), { variant: "icon", title });
      b.append(icon(glyph, 14, "var(--hls-text-muted)"));
      if (flip) b.style.transform = "scaleX(-1)";
      return b;
    };
    const playBtn = h("button", {
      type: "button", class: "btn btn--primary",
      style: "width:34px;height:34px;border-radius:50%;justify-content:center;padding:0",
      title: playing ? "暂停" : "播放",
    }, icon(playing ? "pause" : "play", 15, "#08111e"));
    playBtn.addEventListener("click", () => void togglePlay());

    clear(el.transport).append(
      stepBtn(() => 0, "回到首帧", "play", true),
      stepBtn(() => frame - 1, "上一帧", "play", true),
      playBtn,
      stepBtn(() => frame + 1, "下一帧", "play", false),
      stepBtn(() => frameCount() - 1, "跳到末帧", "play", false),
      h("span", { class: "small mono" }, timecode(frame, fps())),
      h("span", { class: "tiny muted mono" }, `/ ${timecode(Math.max(frameCount() - 1, 0), fps())}`),
      h("div", { class: "spacer" }),
      toggleChip("磁吸", ui.snap, "跳帧时吸附到 ±2 帧内的关键帧", (v) => { ui.snap = v; renderTransport(); }),
      toggleChip("循环", ui.loop, "播放到末帧后回到首帧", (v) => { ui.loop = v; renderTransport(); }),
      toggleChip("仅已绘帧", ui.onlyPainted, "跳帧时只落在已绘制/已新建的帧上", (v) => {
        ui.onlyPainted = v;
        renderTransport();
        renderTimeline();
      }),
      h("span", { class: "tiny muted mono" }, `${fps()} fps`),
    );
  }

  function renderStrip(): void {
    if (!el) return;
    const info = store.ctx.videoInfo;
    const box = clear(el.strip);
    if (!info) {
      box.append(h("div", { class: "tiny muted", style: "padding:8px 0" },
        "未载入视频：请先在「视频调光」打开视频，或点上方「打开视频」。"));
      renderRuler();
      return;
    }
    const row = h("div", { class: "row", style: "gap:6px;overflow-x:auto;padding-bottom:4px" });
    if (thumbs) {
      thumbs.frames.forEach((f, i) => {
        const cur = f === frame;
        const cell = h("button", {
          type: "button",
          title: `跳到帧 ${f}`,
          style: `flex:0 0 auto;padding:2px;border-radius:6px;background:var(--hls-bg-canvas);cursor:pointer;
                  border:2px solid ${cur ? "var(--hls-accent-bright)" : "var(--hls-border)"}`,
        },
          h("img", { src: pngUrl(thumbs?.png[i] ?? ""), alt: `帧 ${f}`, style: "display:block;width:74px;height:auto;border-radius:4px" }),
          h("div", { class: `tiny mono${cur ? "" : " muted"}`, style: "text-align:center;margin-top:2px" },
            cur ? "当前帧" : String(f)),
        );
        cell.addEventListener("click", () => void seek(f));
        row.append(cell);
      });
    } else {
      row.append(h("div", { class: "tiny muted", style: "padding:6px" }, "缩略图加载中…"));
    }
    const add = h("button", {
      type: "button", class: "btn", title: "在当前帧建立空白光绘标记",
      style: `flex:0 0 auto;width:76px;height:56px;justify-content:center;border-style:dashed;
              border-color:${created.has(frame) ? "var(--hls-accent-line)" : "var(--hls-border-strong)"}`,
    }, icon("plus", 15), h("span", {}, "新建"));
    add.addEventListener("click", newBlankFrame);
    row.append(add);
    box.append(row, h("div", { class: "tiny muted", style: "margin-top:2px" },
      `已绘制 / 已建立 ${paintedFrames().length} 帧 · 共 ${frameCount()} 帧（缩略图为后端均匀采样，点击即跳帧）`));
    renderRuler();
  }

  function renderRuler(): void {
    if (!el) return;
    const total = Math.max(frameCount(), 1);
    const w = Math.max(el.ruler.clientWidth || 0, 320);
    const hgt = 26;
    const dpr = window.devicePixelRatio || 1;
    el.ruler.width = Math.round(w * dpr);
    el.ruler.height = Math.round(hgt * dpr);
    el.ruler.style.width = `${w}px`;
    el.ruler.style.height = `${hgt}px`;
    const g = el.ruler.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hgt);
    const at = (f: number) => (f / Math.max(total - 1, 1)) * (w - 30) + 15;
    const step = Math.max(1, Math.ceil(total / 12));
    g.font = "9px ui-monospace, Consolas, monospace";
    g.textAlign = "center";
    for (let f = 0; f < total; f += step) {
      g.strokeStyle = "rgba(255,255,255,0.16)";
      g.beginPath();
      g.moveTo(at(f), 2);
      g.lineTo(at(f), 8);
      g.stroke();
      g.fillStyle = "#5e6a7e";
      g.fillText(String(f), at(f), 19);
    }
    for (const f of paintedFrames()) {
      const x = at(f);
      g.fillStyle = "#34d399";
      g.beginPath();
      g.moveTo(x, 1);
      g.lineTo(x + 3, 5);
      g.lineTo(x, 9);
      g.lineTo(x - 3, 5);
      g.closePath();
      g.fill();
    }
    const cx = at(frame);
    g.fillStyle = "#6fa8ff";
    g.fillRect(cx - 1, 0, 2, hgt);
    g.beginPath();
    g.moveTo(cx - 4, 0);
    g.lineTo(cx + 4, 0);
    g.lineTo(cx, 6);
    g.closePath();
    g.fill();
  }

  function renderTimeline(): void {
    if (!el) return;
    renderStrip();
    const tracks = clear(el.tracks);
    const total = Math.max(frameCount(), 1);
    let any = false;
    for (const b of BRUSHES) {
      const frames = paintedFrames().filter((f) => track.get(f)?.some((s) => s.type === b.id));
      if (!frames.length) continue;
      any = true;
      const bar = h("div", { style: "position:relative;flex:1;height:8px;border-radius:4px;background:var(--hls-bg-elevated)" });
      for (const f of frames) {
        bar.append(h("span", {
          title: `帧 ${f}`,
          style: `position:absolute;left:${(f / Math.max(total - 1, 1)) * 100}%;top:-2px;width:6px;height:6px;
                  transform:translateX(-50%) rotate(45deg);background:var(--hls-success)`,
        }));
      }
      bar.append(h("span", {
        title: `当前帧 ${frame}`,
        style: `position:absolute;left:${(frame / Math.max(total - 1, 1)) * 100}%;top:-5px;width:1px;height:18px;
                background:var(--hls-accent-bright)`,
      }));
      tracks.append(h("div", { class: "row", style: "height:22px;gap:8px" },
        h("div", { class: "tiny ellipsis", style: "width:78px;color:var(--hls-text-secondary)" }, b.name),
        bar,
        h("span", { class: "tiny muted mono", style: "width:30px;text-align:right" }, String(frames.length)),
      ));
    }
    if (!any) {
      tracks.append(h("div", { class: "tiny muted", style: "padding:6px 0" }, "尚无光绘帧：在画面中按住拖动即可绘制笔触"));
    }
  }

  // -------- 播放 --------

  async function togglePlay(): Promise<void> {
    if (frameCount() === 0) return;
    playing = !playing;
    renderTransport();
    renderHud();
    while (playing && !disposed) {
      const t0 = performance.now();
      const next = frame + 1;
      if (next >= frameCount()) {
        if (!ui.loop) break;
        await seek(0);
      } else {
        await seek(next);
      }
      await sleep(Math.max(20, 1000 / fps() - (performance.now() - t0)));
    }
    playing = false;
    renderTransport();
    renderHud();
  }

  // -------- 组装 --------

  function pickVideo(): void {
    const input = h("input", { type: "file", accept: "video/*", style: "display:none" });
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      input.remove();
      if (!file) return;
      void (async () => {
        try {
          const media = await api.videoUpload(file, file.name);
          if (media.kind !== "video") return;
          store.set({
            videoId: media.video_id,
            videoInfo: media.info,
            mediaKind: "video",
            mediaName: media.source_name || file.name,
            frameIndex: 0,
          });
          frame = 0;
          await bootstrap();
          renderAll();
          toast(`已载入 ${media.info.width}×${media.info.height} · ${media.info.frame_count} 帧`, "ok");
        } catch (e) {
          reportError(e);
        }
      })();
    });
    document.body.append(input);
    input.click();
  }

  function buildDom(root: HTMLElement): Dom {
    // 左栏
    const brushGrid = h("div");
    const brushProps = h("div", { style: "display:flex;flex-direction:column;gap:9px" });
    const swatches = h("div");
    const curveBox = h("div", { style: "display:flex;flex-direction:column;gap:6px" });
    const left = h("div", { class: "panel", style: "width:238px;flex:0 0 238px" },
      h("div", { class: "panel__head" }, icon("wand", 13), "光绘笔刷"),
      h("div", { class: "panel__body" },
        h("div", { class: "small sec" }, "光笔类型"), brushGrid,
        h("div", { class: "divider" }), brushProps,
        h("div", { class: "divider" }),
        h("div", { class: "small sec" }, "光色"), swatches,
        h("div", { class: "divider" }),
        h("div", { class: "small sec" }, "压感曲线"), curveBox,
      ),
    );

    // 中栏
    const hud = h("div", { class: "row", style: "gap:8px;padding:10px 14px;border-bottom:1px solid var(--hls-border)" });
    const frameBox = h("div", { style: "position:relative;width:0;height:0" });
    const img = h("img", { class: "canvas-img", alt: "当前帧", style: "width:100%;height:100%;object-fit:contain" });
    const overlay = h("canvas", { style: "position:absolute;inset:0;cursor:crosshair;touch-action:none" });
    frameBox.append(img, overlay);
    const stage = h("div", { class: "stage" }, frameBox);
    const progress = h("div", { style: "padding:0 14px 6px" });
    const exportBtn = button("导出序列", () => void exportSequence(), { variant: "primary", icon: "download" });
    const toolbar = h("div", { class: "row", style: "gap:8px;padding:8px 14px;border-bottom:1px solid var(--hls-border)" },
      button("打开视频", pickVideo, { icon: "upload", title: "与「视频调光」同一个上传接口" }),
      button("预览", () => void refreshPreview(), { icon: "rotate", title: "重新请求本帧合成结果" }),
      button("清除本帧", clearFrame, { icon: "trash", title: "删除当前帧的全部笔触" }),
      button("新建", newBlankFrame, { icon: "plus", title: "在当前帧建立空白光绘标记" }),
      h("div", { class: "spacer" }),
      exportBtn,
    );
    const main = h("div", { class: "main" },
      hud, toolbar, stage, progress,
      h("div", { class: "tiny muted", style: "text-align:center;padding:6px 14px 10px" },
        "在画面上按住拖动即可绘制 · 光束由拖拽方向决定投射角与长度 · 快捷键 ← → 跳帧 / N 新建空白帧 / [ ] 调半径 · 预览与导出使用同一份笔触数据"),
    );

    // 右栏
    const layerBox = h("div");
    const props = h("div", { style: "display:flex;flex-direction:column;gap:9px" });
    const right = h("div", { class: "panel panel--right", style: "width:276px;flex:0 0 276px" },
      h("div", { class: "panel__head" }, icon("layers", 13), "光绘图层"),
      h("div", { class: "panel__body" }, layerBox, h("div", { class: "divider" }), props),
    );

    // 底部时间轴
    const transport = h("div", { class: "row", style: "gap:8px" });
    const strip = h("div", { style: "min-height:0" });
    const ruler = h("canvas", { style: "display:block;cursor:pointer" });
    const tracks = h("div", { style: "display:flex;flex-direction:column;gap:2px;overflow-y:auto;min-height:0" });
    ruler.addEventListener("click", (e) => {
      const r = ruler.getBoundingClientRect();
      const total = Math.max(frameCount(), 1);
      const t = (e.clientX - r.left - 15) / Math.max(r.width - 30, 1);
      void seek(Math.round(clamp(t, 0, 1) * (total - 1)));
    });
    const timeline = h("div", {
      style: "flex:0 0 216px;display:flex;flex-direction:column;gap:6px;padding:8px 14px;background:var(--hls-bg-panel);border-top:1px solid var(--hls-border);min-height:0;overflow:hidden",
    }, transport, strip, ruler, tracks);

    root.append(h("div", { style: "flex:1;display:flex;min-height:0" }, left, main, right), timeline);
    return { brushGrid, brushProps, swatches, curveBox, stage, frameBox, img, overlay, hud, progress, exportBtn, layers: layerBox, props, strip, ruler, tracks, transport };
  }

  function renderAll(): void {
    renderBrushGrid();
    renderCurve();
    renderSwatches();
    renderBrushProps();
    renderLayers();
    renderProps();
    renderHud();
    renderTransport();
    renderTimeline();
  }

  // -------- 生命周期 --------

  let resizeObserver: ResizeObserver | null = null;
  let unsub: (() => void) | null = null;
  let lastStoreVideo: string | null = null;

  /** 快捷键：←/→ 跳帧，N 新建空白帧，[ / ] 调整笔刷半径 */
  function onKey(e: KeyboardEvent): void {
    const t = e.target;
    if (t instanceof HTMLInputElement || t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement) return;
    if (e.key === "ArrowLeft") void seek(frame - 1);
    else if (e.key === "ArrowRight") void seek(frame + 1);
    else if (e.key === "n" || e.key === "N") newBlankFrame();
    else if (e.key === "[" || e.key === "]") {
      brush.radius = clamp(brush.radius + (e.key === "[" ? -0.01 : 0.01), 0.01, 0.6);
      renderBrushProps();
    } else return;
    e.preventDefault();
  }

  return {
    id: "framepaint",
    name: "逐帧光绘",
    subtitle: "光绘笔刷 · 帧级标记 · 导出序列",
    icon: "wand",
    mount(root: HTMLElement): void {
      disposed = false;
      root.style.flexDirection = "column";
      el = buildDom(root);
      const dom = el;
      dom.overlay.addEventListener("pointerdown", onDown);
      dom.overlay.addEventListener("pointermove", onMove);
      dom.overlay.addEventListener("pointerup", onUp);
      dom.overlay.addEventListener("pointercancel", onUp);

      resizeObserver = new ResizeObserver(() => {
        layoutStage();
        renderRuler();
      });
      resizeObserver.observe(dom.stage);
      resizeObserver.observe(dom.strip);
      window.addEventListener("keydown", onKey);

      lastStoreVideo = store.ctx.videoId;
      unsub = store.subscribe(() => {
        if (store.ctx.videoId !== lastStoreVideo) {
          lastStoreVideo = store.ctx.videoId;
          void bootstrap().then(renderAll);
          return;
        }
        renderHud();
        renderTransport();
      });

      renderAll();
      layoutStage();
      void bootstrap().then(() => {
        renderAll();
        layoutStage();
      });
    },
    unmount(): void {
      disposed = true;
      playing = false;
      painting = false;
      if (previewTimer !== null) {
        window.clearTimeout(previewTimer);
        previewTimer = null;
      }
      resizeObserver?.disconnect();
      resizeObserver = null;
      window.removeEventListener("keydown", onKey);
      unsub?.();
      unsub = null;
      el = null;
    },
  };
}
