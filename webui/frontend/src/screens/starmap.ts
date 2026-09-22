/**
 * B2 光源星图（Light Source Star Map）—— 灯组俯视平面视图。
 *
 * 数据来源：currentParams().lights（实时灯组）。任何修改都经 setParams 写回全局基准参数，
 * 因此「星图」上的改动与图片/视频渲染共用同一份灯组。
 *
 * 坐标约定（重要，避免伪造量纲）：
 *  - 平面图按设计稿刻度绘制：X 轴 0~900、Y 轴 100~800（单位：平面刻度，1 格 = 100）。
 *  - 点光源（kind=point）用真实的 px/py 归一化图像坐标映射到平面：X = px×900、Y = 100 + py×700；
 *    其 pz 为引擎的归一化高度（1 ≈ 图宽），界面按原值显示，不换算成米。
 *  - 方向光（kind=directional）在平面上没有位置，仅按 dx/dy 方向落在环上表示朝向；
 *    距离由平面刻度推算（与像素/米无关）。
 *  - 「光束参数」四个下拉框的取值只存在本文件的界面状态里（Map，按光源名索引），
 *    引擎的 Light 没有这些字段，故绝不写入 params（面板上有「示意」标注）。
 *  - 底部汇总不使用「总功率 / DMX / 温度」等本引擎无法测量的量，改用可推导量：
 *    在线数、按强度加权的平均色温、相对强度合计。
 */
import type { Light, RenderParams } from "../api";
import { button, clear, h, icon, segmented, slider, store, toast } from "../ui";
import { currentParams, setParams, type Screen } from "../shell";

type ViewId = "map" | "topo" | "list";
type FilterId = "all" | "online" | "offline";

const KELVIN_PRESETS: { k: number; label: string }[] = [
  { k: 2400, label: "暖钨丝" },
  { k: 3200, label: "暖白" },
  { k: 4300, label: "中性" },
  { k: 5600, label: "日光" },
  { k: 6500, label: "冷白" },
  { k: 9800, label: "冷月" },
];

/** 引擎强度上限（core/types.py Light.intensity 0~4），界面以百分比呈现 */
const INTENSITY_MAX = 4;

/** 光束参数的界面候选项（引擎无对应字段，仅作备注） */
const BEAM_ANGLES = ["15° 窄", "35° 中", "60° 宽", "90° 泛光"];
const FALLOFFS = ["平方反比", "线性", "恒定", "自定义曲线"];
const MODIFIERS = ["裸灯", "柔光罩 · 中", "柔光罩 · 大", "格栅", "束光筒", "反光伞"];
const STROBES = ["关闭", "闪光 1/1", "闪光 1/2", "脉冲", "频闪"];

const ICON_LOCK = ["M5 11h14v10H5Z", "M8 11V7a4 4 0 0 1 8 0v4"];
const ICON_CURSOR = ["m5 3 14 8-6 1.6L10 19Z"];
const ICON_HAND = [
  "M7 11V6.5a1.5 1.5 0 0 1 3 0V11",
  "M10 11V5.5a1.5 1.5 0 0 1 3 0V11",
  "M13 11V6.5a1.5 1.5 0 0 1 3 0V13",
  "M16 10.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.6a6 6 0 0 1-5-2.7L6 15.5",
];
const ICON_TAG = ["M3 11.5V4h7.5L21 14.5 14.5 21Z", "M7.5 7.5h.01"];
const ICON_GRID = ["M3 3h18v18H3Z", "M3 9h18 M3 15h18 M9 3v18 M15 3v18"];
const ICON_EXPAND = ["M4 9V4h5 M20 15v5h-5 M4 4l6 6 M20 20l-6-6"];

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
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

/** 本地内联图标（ui.ts 的 ICONS 里没有这些 glyph，按约定就地内联） */
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

interface BeamUi { angle: string; falloff: string; modifier: string; strobe: string }
interface NodePos { light: Light; x: number; y: number }

export function createStarMapScreen(): Screen {
  // -------- 跨挂载保留的界面状态 --------
  let view: ViewId = "map";
  let filter: FilterId = "all";
  let query = "";
  const collapsed = new Set<string>();
  const locked = new Set<string>();
  /** 光束参数：按光源名索引的界面备注（Map 的键随灯组动态变化） */
  const beamUi = new Map<string, BeamUi>();
  let selectedName: string | null = null;
  let soloName: string | null = null;
  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let showGrid = true;
  let showLabels = true;
  let tool: "select" | "pan" = "select";
  let snapCm = 10;
  let cursor: { x: number; y: number } | null = null;

  // -------- 运行时 --------
  let disposed = false;
  /** 本面板自身写入参数时置位：订阅回调据此跳过属性面板重建，避免拖拽中的滑块被替换 */
  let selfEdit = false;
  let el: {
    root: HTMLElement; groupList: HTMLElement; filterHost: HTMLElement;
    center: HTMLElement; props: HTMLElement; footer: HTMLElement;
    canvasWrap: HTMLElement; canvas: HTMLCanvasElement; readout: HTMLElement; tabs: HTMLElement;
  } | null = null;
  let nodePositions: NodePos[] = [];
  let plot = { originX: 0, originY: 0, scale: 1 };
  let hoverName: string | null = null;
  let dragging = false;
  let lastPointer = { x: 0, y: 0 };
  let resizeObserver: ResizeObserver | null = null;
  let unsub: (() => void) | null = null;

  /** 实时灯组（首次 loadState 之前 params 可能为空，按空数组兜底） */
  function lightsOf(): Light[] {
    const p: RenderParams | null = currentParams() ?? null;
    return p?.lights ?? [];
  }

  /** 写回灯组：整体替换 lights 数组，触发全局状态订阅 */
  function updateLights(next: Light[]): void {
    selfEdit = true;
    setParams({ ...currentParams(), lights: next });
    selfEdit = false;
  }

  function selectedLight(): Light | null {
    const list = lightsOf();
    return list.find((l) => l.name === selectedName) ?? list[0] ?? null;
  }

  function visibleTo(list: Light[]): Light[] {
    const q = query.trim().toLowerCase();
    return list.filter((l) => {
      if (filter === "online" && !l.visible) return false;
      if (filter === "offline" && l.visible) return false;
      if (!q) return true;
      return l.name.toLowerCase().includes(q) || (l.group ?? "").toLowerCase().includes(q);
    });
  }

  function groupNames(list: Light[]): string[] {
    const out: string[] = [];
    for (const l of list) {
      const g = (l.group ?? "").trim() || "未分组";
      if (!out.includes(g)) out.push(g);
    }
    return out;
  }

  function patchLight(name: string, patch: Partial<Light>): void {
    updateLights(lightsOf().map((l) => (l.name === name ? { ...l, ...patch } : l)));
  }

  // -------- 平面图几何 --------

  /** 平面刻度 → 画布像素。X: 0~900 → 宽度；Y: 100~800 → 高度 */
  function toPx(cmX: number, cmY: number): { x: number; y: number } {
    return { x: plot.originX + cmX * plot.scale, y: plot.originY + (cmY - 100) * plot.scale };
  }

  function toCm(px: number, py: number): { x: number; y: number } {
    return { x: (px - plot.originX) / plot.scale, y: (py - plot.originY) / plot.scale + 100 };
  }

  /** 光源在平面上的刻度坐标：点光源用真实 px/py，方向光按 dx/dy 落在环上 */
  function planCm(l: Light): { x: number; y: number } {
    if (l.kind === "point") return { x: clamp(l.px, 0, 1) * 900, y: 100 + clamp(l.py, 0, 1) * 700 };
    const n = Math.hypot(l.dx, l.dy) || 1;
    return { x: clamp(450 + (l.dx / n) * 300, 20, 880), y: clamp(450 + (l.dy / n) * 300, 120, 780) };
  }

  function computeLayout(): void {
    if (!el) return;
    const w = el.canvas.clientWidth || 640;
    const hgt = el.canvas.clientHeight || 480;
    const dpr = window.devicePixelRatio || 1;
    el.canvas.width = Math.round(w * dpr);
    el.canvas.height = Math.round(hgt * dpr);
    const scale = Math.min(w / 900, hgt / 700) * zoom;
    plot = {
      originX: (w - 900 * scale) / 2 + panX,
      originY: (hgt - 700 * scale) / 2 + panY,
      scale,
    };
    const g = el.canvas.getContext("2d");
    if (g) g.setTransform(dpr, 0, 0, dpr, 0, 0);
    nodePositions = visibleTo(lightsOf()).map((l) => {
      const cm = planCm(l);
      return { light: l, ...toPx(cm.x, cm.y) };
    });
  }

  const gridPx = () => Math.round(100 * plot.scale);
  const snapPx = () => Math.round(snapCm * plot.scale);

  /** 缩放 / 平移 / 尺寸变化后统一重算几何并刷新画布与两处读数（读数与几何必须同步） */
  function syncView(): void {
    computeLayout();
    draw();
    renderReadout();
    renderFooter();
  }

  // -------- 画布绘制 --------

  function draw(): void {
    if (!el) return;
    const g = el.canvas.getContext("2d");
    if (!g) return;
    const w = el.canvas.clientWidth || 640;
    const hgt = el.canvas.clientHeight || 480;
    g.clearRect(0, 0, w, hgt);
    g.fillStyle = "#070910";
    g.fillRect(0, 0, w, hgt);

    const all = visibleTo(lightsOf());
    const subjectCm = { x: 450, y: 450 };
    const subject = toPx(subjectCm.x, subjectCm.y);
    const ring = 300 * plot.scale;

    // 网格与刻度
    if (showGrid) {
      for (let x = 0; x <= 900; x += 50) {
        const major = x % 100 === 0;
        g.strokeStyle = major ? "rgba(255,255,255,0.10)" : "rgba(255,255,255,0.045)";
        g.beginPath();
        g.moveTo(toPx(x, 100).x, toPx(x, 100).y);
        g.lineTo(toPx(x, 800).x, toPx(x, 800).y);
        g.stroke();
      }
      for (let y = 100; y <= 800; y += 50) {
        const major = y % 100 === 0;
        g.strokeStyle = major ? "rgba(255,255,255,0.10)" : "rgba(255,255,255,0.045)";
        g.beginPath();
        g.moveTo(toPx(0, y).x, toPx(0, y).y);
        g.lineTo(toPx(900, y).x, toPx(900, y).y);
        g.stroke();
      }
      g.font = "9px ui-monospace, Consolas, monospace";
      g.fillStyle = "#5e6a7e";
      g.textAlign = "center";
      for (let x = 0; x <= 900; x += 100) g.fillText(String(x), toPx(x, 100).x, toPx(0, 100).y - 6);
      g.textAlign = "right";
      for (let y = 100; y <= 800; y += 100) g.fillText(String(y), toPx(0, 100).x - 6, toPx(0, y).y + 3);
    }

    // 星图视图额外画出环形参考线（拓扑视图只保留直角连线）
    if (view === "map") {
      g.setLineDash([4, 5]);
      g.strokeStyle = "rgba(111,168,255,0.22)";
      for (const r of [120, 220, 300]) {
        g.beginPath();
        g.ellipse(subject.x, subject.y, r * plot.scale, r * plot.scale * 0.62, 0, 0, Math.PI * 2);
        g.stroke();
      }
      g.setLineDash([]);
    }

    // 被摄体
    g.strokeStyle = "rgba(255,255,255,0.35)";
    g.setLineDash([3, 4]);
    g.beginPath();
    g.arc(subject.x, subject.y, Math.max(14, 40 * plot.scale), 0, Math.PI * 2);
    g.stroke();
    g.setLineDash([]);
    const sg = g.createRadialGradient(subject.x, subject.y, 0, subject.x, subject.y, 16);
    sg.addColorStop(0, "rgba(255,255,255,0.95)");
    sg.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = sg;
    g.beginPath();
    g.arc(subject.x, subject.y, 16, 0, Math.PI * 2);
    g.fill();
    g.fillStyle = "#e9eef8";
    g.beginPath();
    g.arc(subject.x, subject.y, 3, 0, Math.PI * 2);
    g.fill();
    g.font = "10px Inter, system-ui, sans-serif";
    g.textAlign = "center";
    g.fillStyle = "#98a5ba";
    g.fillText("被摄体", subject.x, subject.y + 30);

    // 连线：星图/拓扑都连向被摄体，拓扑走直角折线以示区分
    for (const n of nodePositions) {
      const solo = soloName === null || soloName === n.light.name;
      g.strokeStyle = n.light.visible && solo ? hexA(kelvinHex(n.light.kelvin), 0.5) : "rgba(255,255,255,0.12)";
      g.lineWidth = 1.2;
      g.beginPath();
      if (view === "topo") {
        g.moveTo(subject.x, subject.y);
        g.lineTo(n.x, subject.y);
        g.lineTo(n.x, n.y);
      } else {
        g.moveTo(subject.x, subject.y);
        g.lineTo(n.x, n.y);
      }
      g.stroke();
    }

    // 节点
    g.font = "10px Inter, system-ui, sans-serif";
    for (const n of nodePositions) {
      const l = n.light;
      const on = l.visible;
      const solo = soloName === null || soloName === l.name;
      const alpha = solo ? (on ? 1 : 0.35) : 0.18;
      const r = 4 + (clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 8;
      const hex = kelvinHex(l.kelvin);
      const glow = g.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 3.2);
      glow.addColorStop(0, hexA(hex, 0.55 * alpha));
      glow.addColorStop(1, hexA(hex, 0));
      g.fillStyle = glow;
      g.beginPath();
      g.arc(n.x, n.y, r * 3.2, 0, Math.PI * 2);
      g.fill();
      g.fillStyle = hexA(hex, alpha);
      g.beginPath();
      g.arc(n.x, n.y, r, 0, Math.PI * 2);
      g.fill();
      const isSel = selectedName ? l.name === selectedName : nodePositions[0]?.light.name === l.name;
      g.strokeStyle = isSel ? "#6fa8ff" : hoverName === l.name ? "rgba(255,255,255,0.6)" : "rgba(0,0,0,0.35)";
      g.lineWidth = isSel ? 2 : 1;
      g.beginPath();
      g.arc(n.x, n.y, r + (isSel ? 3 : 0), 0, Math.PI * 2);
      g.stroke();
      if (showLabels) {
        const text = `${l.name} ${Math.round((l.intensity / INTENSITY_MAX) * 100)}%`;
        g.fillStyle = "rgba(10,13,19,0.82)";
        const tw = g.measureText(text).width + 12;
        g.fillRect(n.x - tw / 2, n.y + r + 6, tw, 16);
        g.strokeStyle = "rgba(255,255,255,0.08)";
        g.strokeRect(n.x - tw / 2, n.y + r + 6, tw, 16);
        g.fillStyle = on ? "#e9eef8" : "#5e6a7e";
        g.fillText(text, n.x, n.y + r + 17);
        if (isSel) {
          const dx = n.x - subject.x;
          const dy = n.y - subject.y;
          const cm = Math.hypot(dx, dy) / plot.scale;
          const deg = ((Math.atan2(dy, dx) * 180) / Math.PI + 360) % 360;
          const info = `${Math.round(cm)} 格 · 方位 ${Math.round(deg)}°`;
          const iw = g.measureText(info).width + 12;
          g.fillStyle = "rgba(19,35,60,0.95)";
          g.fillRect(n.x - iw / 2, n.y - r - 24, iw, 15);
          g.fillStyle = "#6fa8ff";
          g.fillText(info, n.x, n.y - r - 13);
        }
      }
    }

    // 未匹配任何光源时的空态
    if (!all.length) {
      g.fillStyle = "#5e6a7e";
      g.font = "12px Inter, system-ui, sans-serif";
      g.textAlign = "center";
      g.fillText("当前筛选下没有光源", subject.x, subject.y - 40);
    }
  }

  // -------- 画布交互 --------

  function pick(sx: number, sy: number): Light | null {
    let best: Light | null = null;
    let bestD = 18;
    for (const n of nodePositions) {
      const d = Math.hypot(n.x - sx, n.y - sy);
      if (d < bestD) {
        bestD = d;
        best = n.light;
      }
    }
    return best;
  }

  function localPoint(e: PointerEvent | MouseEvent): { x: number; y: number } {
    const r = el?.canvas.getBoundingClientRect();
    if (!r) return { x: 0, y: 0 };
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  function onCanvasDown(e: PointerEvent): void {
    if (!el) return;
    dragging = true;
    lastPointer = { x: e.clientX, y: e.clientY };
    try {
      el.canvas.setPointerCapture(e.pointerId);
    } catch {
      /* 忽略：无捕获时仍可通过元素上的 pointermove 继续平移 */
    }
    const p = localPoint(e);
    if (tool === "select" && e.button === 0) {
      const hit = pick(p.x, p.y);
      if (hit) {
        selectedName = hit.name;
        renderGroupList();
        renderProps();
        draw();
      }
    }
  }

  function onCanvasMove(e: PointerEvent): void {
    if (!el) return;
    const p = localPoint(e);
    const cm = toCm(p.x, p.y);
    cursor = { x: Math.round(cm.x), y: Math.round(cm.y) };
    renderReadout();
    if (dragging && (tool === "pan" || e.buttons === 4 || e.buttons === 2)) {
      panX += e.clientX - lastPointer.x;
      panY += e.clientY - lastPointer.y;
      lastPointer = { x: e.clientX, y: e.clientY };
      syncView();
      return;
    }
    const hit = pick(p.x, p.y);
    const name = hit ? hit.name : null;
    if (name !== hoverName) {
      hoverName = name;
      draw();
    }
  }

  function onCanvasUp(e: PointerEvent): void {
    if (!el) return;
    dragging = false;
    if (el.canvas.hasPointerCapture(e.pointerId)) el.canvas.releasePointerCapture(e.pointerId);
  }

  function onCanvasWheel(e: WheelEvent): void {
    if (!el) return;
    e.preventDefault();
    const p = localPoint(e);
    const before = toCm(p.x, p.y);
    zoom = clamp(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), 0.35, 3);
    computeLayout();
    const after = toPx(before.x, before.y);
    panX += p.x - after.x;
    panY += p.y - after.y;
    syncView();
  }

  // -------- 渲染：左栏 --------

  function renderLeft(): void {
    if (!el) return;
    renderFilter();
    renderGroupList();
  }

  /** 筛选/搜索变化后刷新可见内容：列表视图重建表格，星图视图重算节点 */
  function refreshFiltered(): void {
    if (!el) return;
    if (view === "list") renderList();
    else syncView();
    renderReadout();
    renderFooter();
  }

  /** 筛选分段控件（独立容器，重建不影响搜索框焦点） */
  function renderFilter(): void {
    if (!el) return;
    clear(el.filterHost).append(segmented(
      [{ id: "all", label: "全部" }, { id: "online", label: "在线" }, { id: "offline", label: "离线" }],
      filter,
      (id) => {
        filter = id;
        renderFilter();
        renderGroupList();
        refreshFiltered();
      },
    ));
  }

  function renderGroupList(): void {
    if (!el) return;
    const all = lightsOf();
    const list = visibleTo(all);
    const box = clear(el.groupList);

    for (const g of groupNames(list)) {
      const members = list.filter((l) => ((l.group ?? "").trim() || "未分组") === g);
      const isCollapsed = collapsed.has(g);
      const online = members.filter((l) => l.visible).length;
      const head = h("button", { type: "button", class: "row", style: "width:100%;gap:6px;padding:6px 2px;background:transparent;border:none;color:var(--hls-text-secondary)" },
        icon(isCollapsed ? "play" : "minus", 12),
        h("span", { class: "ellipsis grow", style: "text-align:left;font-size:11px;font-weight:600" }, g),
        h("span", { class: "tiny muted mono" }, `${online}/${members.length}`),
      );
      head.addEventListener("click", () => {
        if (isCollapsed) collapsed.delete(g);
        else collapsed.add(g);
        renderGroupList();
      });
      const eye = h("button", {
        type: "button", class: "btn btn--icon", title: "整组上线 / 离线（写入 visible）",
      }, icon(online === members.length ? "sun" : "power", 13, "var(--hls-text-muted)"));
      eye.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const target = online !== members.length;
        updateLights(lightsOf().map((l) => (((l.group ?? "").trim() || "未分组") === g ? { ...l, visible: target } : l)));
        renderGroupList();
        renderProps();
        refreshFiltered();
      });
      head.append(eye);
      box.append(head);
      if (isCollapsed) continue;

      for (const l of members) {
        const hex = kelvinHex(l.kelvin);
        const isSel = selectedName ? l.name === selectedName : list[0]?.name === l.name;
        const row = h("div", {
          class: `item${isSel ? " on" : ""}`,
          style: `flex-direction:column;align-items:stretch;gap:5px;cursor:pointer;${l.visible ? "" : "opacity:0.5"}`,
        },
          h("div", { class: "row", style: "gap:7px" },
            h("span", {
              style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:${l.visible ? hex : "#3a424d"}`,
            }),
            h("span", { class: "grow ellipsis", style: "font-weight:600" }, l.name),
            l.kind === "point" ? h("span", { class: "tiny muted" }, "点") : null,
            h("span", { class: "tiny mono" }, `${Math.round((l.intensity / INTENSITY_MAX) * 100)}%`),
          ),
          h("div", { class: "row", style: "gap:6px" },
            h("span", { class: "tiny muted ellipsis grow" },
              `${(l.group ?? "").trim() || "未分组"} · ${Math.round(l.kelvin)}K`),
            h("span", { class: "tiny mono muted" }, hex),
          ),
          h("div", { class: "bar" }, h("i", { style: `width:${(l.intensity / INTENSITY_MAX) * 100}%;background:${hex}` })),
        );
        row.addEventListener("click", () => {
          selectedName = l.name;
          renderGroupList();
          renderProps();
          draw();
        });
        box.append(row);
      }
    }
    if (!list.length) box.append(h("div", { class: "tiny muted", style: "padding:8px 0" }, "没有匹配的光源"));
    box.append(h("div", { class: "tiny muted" }, `共 ${all.length} 盏 · 当前显示 ${list.length} 盏`));
  }

  function addLight(): void {
    const list = lightsOf();
    const sel = selectedLight();
    const group = (sel?.group ?? "").trim();
    const base = list.length + 1;
    let name = `光源-${String(base).padStart(2, "0")}`;
    while (list.some((l) => l.name === name)) name = `${name}·`;
    const next: Light = {
      name, kind: "directional", dx: -0.5, dy: -0.6, dz: 0.7, px: 0.5, py: 0.35, pz: 1.2,
      intensity: 1, kelvin: 5600, radius: 0.35, visible: true, group,
    };
    updateLights([...list, next]);
    selectedName = name;
    renderAll();
    toast(`已新建 ${name}${group ? ` · ${group}` : ""}`, "ok");
  }

  // -------- 渲染：列表视图 --------

  function renderList(): void {
    if (!el) return;
    const box = clear(el.center);
    const list = visibleTo(lightsOf());
    const table = h("div", { class: "panel__body", style: "overflow-y:auto;padding:12px 14px" });
    table.append(h("div", { class: "row", style: "gap:8px;font-size:10.5px;color:var(--hls-text-muted);padding:0 8px 6px" },
      h("span", { style: "width:130px" }, "名称"),
      h("span", { style: "width:92px" }, "分组"),
      h("span", { style: "width:58px" }, "类型"),
      h("span", { style: "width:52px" }, "强度"),
      h("span", { style: "width:70px" }, "色温"),
      h("span", { style: "width:76px" }, "色相"),
      h("span", { class: "grow" }, "位置 / 方向"),
      h("span", { style: "width:44px" }, "在线"),
    ));
    for (const l of list) {
      const isSel = selectedName ? l.name === selectedName : list[0]?.name === l.name;
      const pos = l.kind === "point"
        ? `X ${l.px.toFixed(2)} · Y ${l.py.toFixed(2)} · Z ${l.pz.toFixed(2)}`
        : `dx ${l.dx.toFixed(2)} · dy ${l.dy.toFixed(2)} · dz ${l.dz.toFixed(2)}`;
      const chk = h("input", { type: "checkbox", checked: l.visible, title: "在线 / 离线（写入 visible）" });
      chk.addEventListener("click", (ev) => ev.stopPropagation());
      chk.addEventListener("change", () => {
        patchLight(l.name, { visible: chk.checked });
        renderAll();
      });
      const row = h("div", {
        class: "row",
        style: `gap:8px;padding:7px 8px;border-radius:6px;cursor:pointer;font-size:11.5px;
                background:${isSel ? "var(--hls-accent-tint)" : "transparent"}`,
      },
        h("span", { class: "ellipsis", style: "width:130px;font-weight:600;display:flex;gap:6px;align-items:center" },
          h("span", { style: `width:7px;height:7px;border-radius:50%;background:${kelvinHex(l.kelvin)}` }), l.name),
        h("span", { class: "muted ellipsis", style: "width:92px" }, (l.group ?? "").trim() || "未分组"),
        h("span", { class: "muted", style: "width:58px" }, l.kind === "point" ? "点光源" : "方向光"),
        h("span", { class: "mono", style: "width:52px" }, `${Math.round((l.intensity / INTENSITY_MAX) * 100)}%`),
        h("span", { class: "mono", style: "width:70px" }, `${Math.round(l.kelvin)}K`),
        h("span", { class: "mono", style: "width:76px" }, kelvinHex(l.kelvin)),
        h("span", { class: "grow mono muted ellipsis" }, pos),
        h("span", { style: "width:44px" }, chk),
      );
      row.addEventListener("click", () => {
        selectedName = l.name;
        renderAll();
      });
      table.append(row);
    }
    if (!list.length) table.append(h("div", { class: "tiny muted", style: "padding:10px 8px" }, "没有匹配的光源"));
    box.append(table);
  }

  // -------- 渲染：右栏 --------

  function renderProps(): void {
    if (!el) return;
    const box = clear(el.props);
    const l = selectedLight();
    if (!l) {
      box.append(h("div", { class: "tiny muted", style: "padding:10px 0" }, "灯组为空：可在左栏点「+」新建光源。"));
      return;
    }
    const isLocked = locked.has(l.name);
    const hex = kelvinHex(l.kelvin);
    /** 改动只回写引擎字段 + 刷新画布/列表，不重建本面板（否则拖拽中的滑块会被替换） */
    const apply = (patch: Partial<Light>): void => {
      if (isLocked) return;
      patchLight(l.name, patch);
      draw();
      renderGroupList();
      renderFooter();
      renderReadout();
    };

    // 预览卡：显示该光源的色温色与开关状态（颜色由色温推导）
    const preview = h("canvas", { width: 240, height: 62, style: "width:100%;height:62px;border-radius:var(--r-md);border:1px solid var(--hls-border)" });
    const pg = preview.getContext("2d");
    if (pg) {
      const grad = pg.createRadialGradient(120, 40, 0, 120, 40, 110);
      grad.addColorStop(0, hexA(hex, l.visible ? 0.95 : 0.28));
      grad.addColorStop(1, hexA(hex, 0));
      pg.fillStyle = "#070910";
      pg.fillRect(0, 0, preview.width, preview.height);
      pg.fillStyle = grad;
      pg.fillRect(0, 0, preview.width, preview.height);
    }
    box.append(h("div", { style: "position:relative" },
      preview,
      h("span", { class: "chip", style: "position:absolute;right:6px;top:6px" },
        h("span", { class: l.visible ? "dot" : "dot dot--off" }), l.visible ? "在线" : "离线"),
      h("span", { class: "chip", style: "position:absolute;left:6px;bottom:6px" },
        `${l.name} · ${l.kind === "point" ? "点光源" : "方向光"}`),
    ));

    const fields = h("div", { class: "panel__body", style: "padding:0;gap:9px" });
    if (isLocked) fields.append(h("div", { class: "banner", style: "font-size:11px" },
      svgIcon(ICON_LOCK, 13), "已锁定：参数只读"));

    fields.append(slider({
      label: "强度", min: 0, max: INTENSITY_MAX, step: 0.01, value: l.intensity,
      format: (v) => `${Math.round((v / INTENSITY_MAX) * 100)}% · ${v.toFixed(2)}`,
      onInput: (v) => apply({ intensity: v }),
    }));
    fields.append(slider({
      label: "色温", min: 1500, max: 12000, step: 50, value: l.kelvin,
      format: (v) => `${Math.round(v)} K`,
      onInput: (v) => apply({ kelvin: v }),
    }));

    // 色相：只读的色温推染色 + 由色温推导的色板
    const rainbow = h("div", {
      style: "height:8px;border-radius:4px;background:linear-gradient(90deg,#ff8a3d,#ffd0a0,#ffffff,#cfe0ff,#9db6ff)",
    });
    const swatches = h("div", { class: "row", style: "gap:6px;flex-wrap:wrap" });
    for (const p of KELVIN_PRESETS) {
      const ph = kelvinHex(p.k);
      const dot = h("button", {
        type: "button", title: `${p.label} · ${p.k}K · ${ph}`,
        style: `width:20px;height:20px;border-radius:50%;background:${ph};cursor:pointer;
                border:1px solid ${Math.abs(l.kelvin - p.k) < 1 ? "var(--hls-accent-bright)" : "var(--hls-border-strong)"}`,
      });
      dot.addEventListener("click", () => {
        apply({ kelvin: p.k });
        renderProps();
      });
      swatches.append(dot);
    }
    fields.append(h("div", { class: "field" },
      h("label", {}, `色相（由 ${Math.round(l.kelvin)}K 推导 · 只读）`),
      h("div", { class: "row", style: "gap:8px" }, h("b", { class: "mono small" }, hex), h("div", { class: "grow" }, rainbow)),
      swatches,
    ));

    // 空间位置（点光源）或方向（方向光）
    if (l.kind === "point") {
      const x = h("input", { class: "input", type: "number", value: l.px, min: 0, max: 1, step: 0.01 });
      const y = h("input", { class: "input", type: "number", value: l.py, min: 0, max: 1, step: 0.01 });
      const z = h("input", { class: "input", type: "number", value: l.pz, min: 0, max: 3, step: 0.01 });
      x.addEventListener("input", () => apply({ px: clamp(Number(x.value) || 0, 0, 1) }));
      y.addEventListener("input", () => apply({ py: clamp(Number(y.value) || 0, 0, 1) }));
      z.addEventListener("input", () => apply({ pz: clamp(Number(z.value) || 0, 0, 3) }));
      fields.append(h("div", { class: "field" },
        h("label", {}, "空间位置（归一化图像坐标 X/Y · 高度 Z）"),
        h("div", { class: "grid3" },
          h("div", { class: "field" }, h("label", {}, "X"), x),
          h("div", { class: "field" }, h("label", {}, "Y"), y),
          h("div", { class: "field" }, h("label", {}, "Z"), z),
        ),
      ));
    } else {
      fields.append(slider({
        label: "方向 dx", min: -1, max: 1, step: 0.01, value: l.dx,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply({ dx: v }),
      }));
      fields.append(slider({
        label: "方向 dy", min: -1, max: 1, step: 0.01, value: l.dy,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply({ dy: v }),
      }));
      fields.append(slider({
        label: "方向 dz", min: 0.05, max: 1, step: 0.01, value: l.dz,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply({ dz: v }),
      }));
      fields.append(h("div", { class: "tiny muted" }, "方向光由 dx/dy/dz 决定朝向（指向光源的单位向量），没有平面位置。"));
    }

    // 光束参数：仅界面备注，不写入引擎参数
    const prev = beamUi.get(l.name);
    const beam: BeamUi = prev ?? {
      angle: BEAM_ANGLES[1], falloff: FALLOFFS[0], modifier: MODIFIERS[1], strobe: STROBES[0],
    };
    if (!prev) beamUi.set(l.name, beam);
    const beamSec = h("div", { style: "display:flex;flex-direction:column;gap:8px" });
    const mkSelect = (label: string, options: string[], value: string, onPick: (v: string) => void) => {
      const sel = h("select", { class: "select" });
      for (const o of options) sel.append(h("option", { value: o, selected: o === value }, o));
      sel.addEventListener("change", () => {
        onPick(sel.value);
        renderFooter();
      });
      return h("div", { class: "field" }, h("label", {}, label), sel);
    };
    beamSec.append(
      h("div", { class: "row row--between" },
        h("div", { class: "small sec" }, "光束参数"),
        h("span", { class: "chip", title: "引擎 Light 无这些字段，仅作界面备注" }, "示意 · 不写入引擎"),
      ),
      h("div", { class: "grid2" },
        mkSelect("光束角", BEAM_ANGLES, beam.angle, (v) => { beam.angle = v; }),
        mkSelect("衰减曲线", FALLOFFS, beam.falloff, (v) => { beam.falloff = v; })),
      h("div", { class: "grid2" },
        mkSelect("柔光附件", MODIFIERS, beam.modifier, (v) => { beam.modifier = v; }),
        mkSelect("频闪模式", STROBES, beam.strobe, (v) => { beam.strobe = v; })),
      h("div", { class: "tiny muted" }, "以上四项只保存在本屏幕的界面状态里，引擎当前按 intensity/kelvin/radius 与方向渲染。"),
    );
    fields.append(beamSec);

    // 独奏 / 静音 / 锁定
    const soloBtn = button("独奏", () => {
      soloName = soloName === l.name ? null : l.name;
      renderAll();
    }, { icon: "lightbulb", on: soloName === l.name, title: "界面预览：只强调这一盏，不改引擎参数" });
    const muteBtn = button(l.visible ? "静音" : "启用", () => {
      patchLight(l.name, { visible: !l.visible });
      renderAll();
    }, { icon: "power", on: !l.visible, title: "写入引擎的 visible" });
    const lockBtn = button("锁定", () => {
      if (locked.has(l.name)) locked.delete(l.name);
      else locked.add(l.name);
      renderAll();
    }, { icon: "check", on: isLocked, title: "界面锁定：禁止本面板继续改这个光源" });
    fields.append(h("div", { class: "grid3" }, soloBtn, muteBtn, lockBtn));

    const groupName = (l.group ?? "").trim();
    const sync = button(groupName ? `同步到 ${groupName}` : "同步到未分组", () => {
      const target = lightsOf().map((o) => {
        if ((o.group ?? "").trim() !== groupName || o.name === l.name) return o;
        return {
          ...o,
          kind: l.kind, dx: l.dx, dy: l.dy, dz: l.dz, px: l.px, py: l.py, pz: l.pz,
          intensity: l.intensity, kelvin: l.kelvin, radius: l.radius,
        };
      });
      updateLights(target);
      renderAll();
      toast(`已将 ${l.name} 的参数同步到「${groupName || "未分组"}」`, "ok");
    }, { variant: "primary", icon: "copy", title: "把当前光源的方向/位置/强度/色温复制到同组光源" });
    const reset = button("复位参数", () => {
      patchLight(l.name, {
        kind: "directional", dx: -0.5, dy: -0.6, dz: 0.7, px: 0.5, py: 0.35, pz: 1.2,
        intensity: 1, kelvin: 5500, radius: 0.35,
      });
      renderAll();
    }, { icon: "rotate", title: "恢复为引擎默认值（Light() 默认）" });
    fields.append(h("div", { class: "row", style: "gap:8px" }, sync, reset));

    box.append(fields);
  }

  // -------- 渲染：底部汇总 --------

  function renderFooter(): void {
    if (!el) return;
    const all = lightsOf();
    const online = all.filter((l) => l.visible);
    const weight = online.reduce((s, l) => s + Math.max(l.intensity, 0), 0);
    const avgK = weight > 0 ? online.reduce((s, l) => s + l.kelvin * Math.max(l.intensity, 0), 0) / weight : 0;
    const sumI = online.reduce((s, l) => s + l.intensity, 0);
    const groups = groupNames(all).length;
    clear(el.footer).append(
      h("span", { class: "row", style: "gap:6px" },
        h("span", { class: online.length ? "dot" : "dot dot--off" }),
        `在线 ${online.length} / ${all.length} 光源`),
      h("span", {}, `· 分组 ${groups}`),
      h("span", {}, `· 平均色温 ${weight > 0 ? Math.round(avgK) : "—"} K（按强度加权）`),
      h("span", {}, `· 相对强度合计 ${sumI.toFixed(2)}（0~4/灯）`),
      h("div", { class: "spacer" }),
      h("span", { class: "tiny" }, "功率 / DMX / 温度非本引擎可测，故以可推导量替代"),
      h("div", { class: "spacer" }),
      h("span", {}, `吸附 ${snapPx()} px`),
      h("span", {}, `· 网格 ${gridPx()} px = 100 刻度`),
      h("span", {}, `· 缩放 ${Math.round(zoom * 100)}%`),
      h("span", { class: "mono" }, cursor ? `· X ${cursor.x} · Y ${cursor.y}` : "· X — · Y —"),
    );
  }

  function renderReadout(): void {
    if (!el) return;
    const sel = selectedLight();
    const parts: Node[] = [
      h("span", { class: "chip" }, `影棚平面 · 网格 100 cm · 缩放 ${Math.round(zoom * 100)}%`),
      h("div", { class: "spacer" }),
    ];
    if (cursor) parts.push(h("span", { class: "chip mono" }, `X ${cursor.x} · Y ${cursor.y}`));
    if (sel) {
      parts.push(h("span", { class: "chip" }, h("span", { class: "dot" }),
        `已选 ${sel.name} · ${sel.kind === "point" ? "点光源" : "方向光"}`));
    }
    clear(el.readout).append(...parts);
  }

  // -------- 渲染：整体 --------

  function renderTabs(): void {
    if (!el) return;
    clear(el.tabs).append(segmented(
      [{ id: "map", label: "星图" }, { id: "topo", label: "拓扑" }, { id: "list", label: "列表" }],
      view,
      (id) => {
        view = id;
        renderBody();
      },
    ));
  }

  function renderCanvasView(): void {
    if (!el) return;
    const box = clear(el.center);
    el.canvasWrap = h("div", { style: "position:relative;flex:1;min-height:0" });
    el.canvas = h("canvas", { style: "display:block;width:100%;height:100%;cursor:crosshair;touch-action:none" });
    const rail = h("div", {
      style: "position:absolute;right:10px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:4px;padding:4px;border-radius:var(--r-md);background:color-mix(in srgb, var(--hls-bg-root) 85%, transparent);border:1px solid var(--hls-border)",
    });
    const railBtn = (glyph: string[] | string, title: string, on: boolean, onClick: () => void) => {
      const b = h("button", { type: "button", class: `nav-btn${on ? " on" : ""}`, title, style: "width:30px;height:28px" },
        typeof glyph === "string" ? icon(glyph, 14) : svgIcon(glyph, 14));
      b.addEventListener("click", onClick);
      return b;
    };
    rail.append(
      railBtn(ICON_CURSOR, "选择：点击节点选中光源", tool === "select", () => { tool = "select"; renderCanvasView(); }),
      railBtn(ICON_HAND, "平移：拖拽或滚轮缩放平面", tool === "pan", () => { tool = "pan"; renderCanvasView(); }),
      railBtn(ICON_TAG, "显示/隐藏节点标签", showLabels, () => { showLabels = !showLabels; renderCanvasView(); }),
      railBtn(ICON_GRID, "显示/隐藏网格", showGrid, () => { showGrid = !showGrid; renderCanvasView(); }),
      railBtn(ICON_EXPAND, "复位视图（缩放 100%、居中）", false, () => {
        zoom = 1; panX = 0; panY = 0; syncView();
      }),
    );
    el.readout = h("div", { class: "row", style: "gap:8px;position:absolute;left:10px;bottom:10px;font-size:10.5px;color:var(--hls-text-secondary)" });
    const zoomPill = h("div", {
      style: "position:absolute;right:10px;bottom:10px;display:flex;align-items:center;gap:6px;padding:4px 6px;border-radius:var(--r-md);background:color-mix(in srgb, var(--hls-bg-root) 85%, transparent);border:1px solid var(--hls-border)",
    });
    const zoomTo = (next: number) => { zoom = clamp(next, 0.35, 3); syncView(); };
    const minus = button("", () => zoomTo(zoom / 1.2), { variant: "icon", title: "缩小" });
    clear(minus).append(icon("minus", 13));
    const plus = button("", () => zoomTo(zoom * 1.2), { variant: "icon", title: "放大" });
    clear(plus).append(icon("plus", 13));
    zoomPill.append(minus, h("span", { class: "tiny mono" }, `${Math.round(zoom * 100)}%`), plus);
    el.canvasWrap.append(el.canvas, rail, el.readout, zoomPill);
    box.append(el.canvasWrap);

    el.canvas.addEventListener("pointerdown", onCanvasDown);
    el.canvas.addEventListener("pointermove", onCanvasMove);
    el.canvas.addEventListener("pointerup", onCanvasUp);
    el.canvas.addEventListener("pointerleave", () => { cursor = null; renderReadout(); });
    el.canvas.addEventListener("wheel", onCanvasWheel, { passive: false });
    el.canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    computeLayout();
    draw();
    renderReadout();
    resizeObserver?.disconnect();
    resizeObserver = new ResizeObserver(() => {
      if (!disposed && el) syncView();
    });
    resizeObserver.observe(el.canvasWrap);
  }

  function renderBody(): void {
    if (!el) return;
    renderTabs();
    if (view === "list") {
      resizeObserver?.disconnect();
      el.canvas = h("canvas");
      renderList();
    } else {
      renderCanvasView();
    }
    renderFooter();
  }

  function renderAll(): void {
    renderLeft();
    renderProps();
    renderBody();
  }

  return {
    id: "starmap",
    name: "光源星图",
    subtitle: "光源分组 · 拓扑 · 光束参数",
    icon: "radar",
    mount(root: HTMLElement): void {
      disposed = false;
      root.style.flexDirection = "column";
      const tabs = h("div", { class: "row", style: "gap:10px;padding:9px 14px;border-bottom:1px solid var(--hls-border)" },
        h("span", { class: "row", style: "gap:7px" },
          icon("radar", 15, "var(--hls-accent)"),
          h("b", {}, "光源星图"),
          h("span", { class: "tiny muted" }, "LIGHT SOURCE STAR MAP")),
        h("div", { style: "width:220px" }),
      );
      const filterHost = h("div");
      const groupList = h("div", { style: "display:flex;flex-direction:column;gap:8px" });
      const search = h("input", { class: "input", type: "search", placeholder: "搜索光源 / 分组…" });
      search.addEventListener("input", () => {
        query = search.value;
        renderGroupList();
        refreshFiltered();
      });
      const addBtn = button("", () => addLight(), { variant: "icon", title: "新建光源（归入当前分组）" });
      clear(addBtn).append(icon("plus", 14));
      const center = h("div", { class: "main" });
      const propsBox = h("div", { style: "display:flex;flex-direction:column;gap:9px" });
      const footer = h("div", { class: "row", style: "gap:10px;padding:7px 14px;background:var(--hls-bg-panel);border-top:1px solid var(--hls-border);font-size:10.5px;color:var(--hls-text-secondary)" });

      const left = h("div", { class: "panel", style: "width:242px;flex:0 0 242px" },
        h("div", { class: "panel__head" }, icon("lightbulb", 13), "光源分组", h("div", { class: "spacer" }), addBtn),
        h("div", { class: "panel__body" }, filterHost, search, groupList),
      );
      const right = h("div", { class: "panel panel--right", style: "width:300px;flex:0 0 300px" },
        h("div", { class: "panel__head" }, icon("sliders", 13), "光源属性"),
        h("div", { class: "panel__body" }, propsBox),
      );
      root.append(tabs, h("div", { style: "flex:1;display:flex;min-height:0" }, left, center, right), footer);

      el = {
        root, groupList, filterHost, center, props: propsBox, footer,
        canvasWrap: center, canvas: h("canvas"), readout: h("div"), tabs,
      };
      // 其它屏幕/流程改了灯组时刷新；本面板自身写入（selfEdit）时不动属性面板，避免拖拽被打断
      unsub = store.subscribe(() => {
        if (disposed || !el) return;
        if (!selfEdit) renderProps();
        renderGroupList();
        renderFooter();
        if (view === "list") renderList();
        else draw();
      });
      renderAll();
    },
    unmount(): void {
      disposed = true;
      resizeObserver?.disconnect();
      resizeObserver = null;
      unsub?.();
      unsub = null;
      el = null;
    },
  };
}
