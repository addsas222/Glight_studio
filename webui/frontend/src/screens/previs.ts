/**
 * B5 三维布光预演：在 <canvas> 上用软件光栅化（无 Three.js）预演灯组与平面主体。
 *
 * 量纲与估算说明（诚实规则）：
 *  - 引擎的光源坐标是**归一化**量：px/py ∈ [0,1] 为图像归一化坐标，pz 为相对
 *    图像长边的归一化距离（见 core/render.py：lx = px*w、lz = pz*w）。预演把它
 *    线性映射到虚拟棚体 3.0 × 3.0 × 2.6 m：X 1 单位 = 3.0 m、Z 1 单位 = 3.0 m、
 *    Y 1 单位 = 2.6 m。这是界面约定的预演尺度，不是实拍米数。
 *  - 平行光在引擎里没有物理灯位（core/render.py 只用方向向量），预演把它放在沿
 *    方向 2.4 m 处，标注为「虚拟灯位」，仅用于显示。
 *  - 光斑直径与光束角由 radius 通过界面估算系数换算（θ = 2·atan(radius×0.35)），
 *    界面上标注「估算」；引擎不计算真实光度学量，因此不显示 lx / CRI / 功率。
 *  - 反光板只是界面对象，不写入 RenderParams，也不影响引擎渲染。
 */
import type { Light, RenderParams } from "../api";
import type { Screen } from "../shell";
import { currentParams, setParams } from "../shell";
import { button, clear, h, icon, numberField, segmented, slider, toast } from "../ui";

// ---------------------------------------------------------------- 几何

interface V3 { x: number; y: number; z: number }

const UP: V3 = { x: 0, y: 1, z: 0 };
const sub = (a: V3, b: V3): V3 => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z });
const cross = (a: V3, b: V3): V3 => ({
  x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x,
});
const dot = (a: V3, b: V3): number => a.x * b.x + a.y * b.y + a.z * b.z;
const len = (a: V3): number => Math.hypot(a.x, a.y, a.z);
const scale = (a: V3, k: number): V3 => ({ x: a.x * k, y: a.y * k, z: a.z * k });
function norm(a: V3): V3 {
  const l = len(a);
  return l < 1e-6 ? { x: 0, y: 0, z: 1 } : scale(a, 1 / l);
}
function lerp3(a: V3, b: V3, t: number): V3 {
  return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, z: a.z + (b.z - a.z) * t };
}
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));
/** 输入框只显示两位小数，避免出现 1.4588999332434598 这类浮点尾巴 */
const r2 = (v: number): number => Math.round(v * 100) / 100;

/** 虚拟棚体尺寸（米），同时定义归一化坐标 → 米制的线性映射 */
const STUDIO = { w: 3.0, h: 2.6, d: 3.0 };
/** 平行光的虚拟灯位距离（米，仅显示用） */
const VIRTUAL_LIGHT_DIST = 2.4;
/** radius → 光斑直径的界面估算系数（标注「估算」） */
const SPOT_K = 0.35;
/** 预演相机视角（等效 35 mm 镜头约 54° 水平视角） */
const FOV = (54 * Math.PI) / 180;
const NEAR = 0.15;

/** 色温 → 近似 RGB（Tanner Helland 近似，仅用于预演灯色） */
function kelvinRgb(k: number): [number, number, number] {
  const t = clamp(k, 1500, 12000) / 100;
  let r: number, g: number, b: number;
  if (t <= 66) { r = 255; g = 99.47 * Math.log(t) - 161.12; }
  else { r = 329.7 * Math.pow(t - 60, -0.1332); g = 288.12 * Math.pow(t - 60, -0.0755); }
  if (t >= 66) b = 255;
  else if (t <= 19) b = 0;
  else b = 138.52 * Math.log(t - 10) - 305.04;
  return [clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255)];
}
const rgbCss = (c: [number, number, number], a = 1): string =>
  `rgba(${c[0].toFixed(0)},${c[1].toFixed(0)},${c[2].toFixed(0)},${a})`;

type ViewId = "persp" | "top" | "side" | "free";

const VIEWS: { id: ViewId; label: string }[] = [
  { id: "persp", label: "透视" },
  { id: "top", label: "俯视" },
  { id: "side", label: "侧视" },
  { id: "free", label: "自由" },
];

interface Reflector { id: number; name: string; x: number; y: number; z: number; }

const SUBJECT_LABEL = "被摄体";

// ---------------------------------------------------------------- 屏幕

/** 屏幕对象在 startApp() 之前构建，此时全局参数尚未加载；这里用引擎默认值占位，
 *  mount() 会用 store 里的真实参数覆盖（core/types.py 的 RenderParams 默认值）。 */
function engineDefaultParams(): RenderParams {
  return {
    lights: [], ambient_intensity: 0.45, ambient_kelvin: 6500,
    shadow_mode: "soft", lighting_mode: "linear",
    preview_size: 512, lighting_size: 1024,
    shadow_strength: 0.85, specular_strength: 0.35, tone_preserve: 0.6,
    exposure: 1.0, reference_offset: null,
  };
}

export function createPrevisScreen(): Screen {
  let draft: RenderParams = engineDefaultParams();
  let selected = 0;
  let locked = false;
  let view: ViewId = "persp";
  let yaw = -0.7;
  let pitch = 0.32;
  let dist = 7.6;
  let subjectW = 1.6;
  let subjectH = 0.9;
  const reflectors: Reflector[] = [];
  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  let cam = { pos: { x: 0, y: 2, z: 8 }, right: { x: 1, y: 0, z: 0 }, up: { x: 0, y: 1, z: 0 }, fwd: { x: 0, y: 0, z: -1 } };

  const cv = h("canvas", { style: "display:block;width:100%;flex:1;min-height:0;cursor:grab" });
  const objList = h("div", { class: "list" });
  const spaceHost = h("div", { class: "panel__body" });
  const rightBody = h("div", { class: "panel__body" });
  const viewHost = h("div", { style: "display:flex;align-items:center;gap:8px" });
  const viewLabel = h("span", { class: "small", style: "font-weight:600" });
  const footerHost = h("div", {
    style: "flex:0 0 auto;display:flex;align-items:center;gap:14px;padding:0 14px;height:32px;border-top:1px solid var(--hls-border);background:var(--hls-bg-panel);color:var(--hls-text-muted);font-size:11px",
  });
  const headChip = h("span", { class: "chip" });

  const ro = new ResizeObserver(() => draw());

  // -------------------------------------------------------------- 坐标映射

  /** 光源 → 虚拟棚体坐标（米）；平行光用方向推导虚拟灯位 */
  function lightPos(l: Light): V3 {
    if (l.kind === "point") {
      return {
        x: (clamp(l.px, 0, 1) - 0.5) * STUDIO.w,
        y: (1 - clamp(l.py, 0, 1)) * STUDIO.h,
        z: clamp(l.pz, 0, STUDIO.d),
      };
    }
    const d = norm({ x: l.dx, y: -l.dy, z: l.dz });
    return scale(d, VIRTUAL_LIGHT_DIST);
  }

  function posToNormalized(l: Light, p: V3): void {
    l.px = clamp(p.x / STUDIO.w + 0.5, 0, 1);
    l.py = clamp(1 - p.y / STUDIO.h, 0, 1);
    l.pz = clamp(p.z, 0, 3);
  }

  const subjectCenter = (): V3 => ({ x: 0, y: subjectH / 2, z: 0 });
  const lightDistance = (l: Light): number => len(sub(subjectCenter(), lightPos(l)));
  const spotDiameter = (l: Light): number => 2 * clamp(l.radius, 0.05, 1.5) * SPOT_K * lightDistance(l);
  const beamAngle = (l: Light): number =>
    (2 * Math.atan(clamp(l.radius, 0.05, 1.5) * SPOT_K) * 180) / Math.PI;
  const radiusFromAngle = (deg: number): number =>
    clamp(Math.tan((clamp(deg, 3, 80) * Math.PI) / 360) / SPOT_K, 0.05, 1.5);
  const azimuth = (l: Light): number => (Math.atan2(l.dx, l.dz) * 180) / Math.PI;
  const elevation = (l: Light): number => (Math.asin(clamp(-l.dy, -1, 1)) * 180) / Math.PI;

  function setDirection(l: Light, az: number, el: number): void {
    const a = (az * Math.PI) / 180;
    const e = (clamp(el, -85, 85) * Math.PI) / 180;
    const d = norm({ x: Math.sin(a) * Math.cos(e), y: -Math.sin(e), z: Math.cos(a) * Math.cos(e) });
    l.dx = d.x; l.dy = d.y; l.dz = d.z;
  }

  /** 主光:补光 光比：按强度降序取最高与次高（引擎强度 0~4） */
  function keyFillRatio(): number | null {
    const xs = draft.lights.filter((l) => l.visible && l.intensity > 0).map((l) => l.intensity);
    if (xs.length < 2) return null;
    xs.sort((a, b) => b - a);
    return xs[1] <= 1e-6 ? null : xs[0] / xs[1];
  }

  // -------------------------------------------------------------- 渲染

  function makeCam(): void {
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const target = { x: 0, y: 1.0, z: 0.6 };
    const pos = {
      x: target.x + dist * cp * Math.sin(yaw),
      y: Math.max(target.y + dist * sp, 0.25),
      z: target.z + dist * cp * Math.cos(yaw),
    };
    const fwd = norm(sub(target, pos));
    const cr = cross(fwd, UP);
    const cl = len(cr);
    const right = cl < 1e-4 ? { x: 1, y: 0, z: 0 } : scale(cr, 1 / cl);
    cam = { pos, fwd, right, up: cross(right, fwd) };
  }

  function toCam(p: V3): V3 {
    const d = sub(p, cam.pos);
    return { x: dot(d, cam.right), y: dot(d, cam.up), z: dot(d, cam.fwd) };
  }

  function projCam(c: V3, w: number, hh: number): { x: number; y: number } {
    const f = (hh / 2) / Math.tan(FOV / 2);
    return { x: w / 2 + (c.x / c.z) * f, y: hh / 2 - (c.y / c.z) * f };
  }

  function drawSeg(g: CanvasRenderingContext2D, a: V3, b: V3, w: number, hh: number): void {
    let ca = toCam(a), cb = toCam(b);
    if (ca.z < NEAR && cb.z < NEAR) return;
    if (ca.z < NEAR) ca = lerp3(ca, cb, (NEAR - ca.z) / (cb.z - ca.z));
    else if (cb.z < NEAR) cb = lerp3(cb, ca, (NEAR - cb.z) / (ca.z - cb.z));
    const pa = projCam(ca, w, hh), pb = projCam(cb, w, hh);
    g.beginPath();
    g.moveTo(pa.x, pa.y);
    g.lineTo(pb.x, pb.y);
    g.stroke();
  }

  function drawPoly(g: CanvasRenderingContext2D, pts: V3[], w: number, hh: number, fill: string, stroke: string): void {
    const scr: { x: number; y: number }[] = [];
    for (const p of pts) {
      const c = toCam(p);
      if (c.z < NEAR) return;          // 主体平面在相机后方时整块跳过（预演相机不会贴到背面）
      scr.push(projCam(c, w, hh));
    }
    g.beginPath();
    g.moveTo(scr[0].x, scr[0].y);
    for (let i = 1; i < scr.length; i += 1) g.lineTo(scr[i].x, scr[i].y);
    g.closePath();
    if (fill) { g.fillStyle = fill; g.fill(); }
    if (stroke) { g.strokeStyle = stroke; g.stroke(); }
  }

  function draw(): void {
    const w = cv.clientWidth, hh = cv.clientHeight;
    if (!w || !hh) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(hh * dpr);
    const g = cv.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hh);
    makeCam();

    const css = getComputedStyle(document.documentElement);
    const tok = (name: string, fallback: string): string =>
      css.getPropertyValue(name).trim() || fallback;
    const border = tok("--hls-border", "#1d2532");
    const strong = tok("--hls-border-strong", "#2b3648");
    const muted = tok("--hls-text-muted", "#5e6a7e");
    const accent = tok("--hls-accent", "#3b82f6");
    const canvasBg = tok("--hls-bg-canvas", "#070910");

    g.fillStyle = canvasBg;
    g.fillRect(0, 0, w, hh);

    // ---- 地面网格（0.5 m）与棚体线框
    g.lineWidth = 1;
    g.strokeStyle = border;
    const halfW = STUDIO.w / 2, halfD = STUDIO.d / 2;
    for (let i = -6; i <= 6; i += 1) {
      const x = (i * STUDIO.w) / 12;
      drawSeg(g, { x, y: 0, z: -halfD }, { x, y: 0, z: halfD }, w, hh);
      const z = (i * STUDIO.d) / 12;
      drawSeg(g, { x: -halfW, y: 0, z }, { x: halfW, y: 0, z }, w, hh);
    }
    g.strokeStyle = strong;
    const box: V3[] = [
      { x: -halfW, y: 0, z: -halfD }, { x: halfW, y: 0, z: -halfD },
      { x: halfW, y: 0, z: halfD }, { x: -halfW, y: 0, z: halfD },
    ];
    for (let i = 0; i < 4; i += 1) {
      const a = box[i], b = box[(i + 1) % 4];
      const a2 = { x: a.x, y: STUDIO.h, z: a.z }, b2 = { x: b.x, y: STUDIO.h, z: b.z };
      drawSeg(g, a, b, w, hh);          // 地面（棚体下沿）
      drawSeg(g, a, a2, w, hh);         // 立柱
      drawSeg(g, a2, b2, w, hh);        // 天花
    }

    // ---- 被摄体平面（平面主体，尺寸来自「主体平面」控件）
    const hw = subjectW / 2;
    drawPoly(g, [
      { x: -hw, y: 0, z: 0 }, { x: hw, y: 0, z: 0 },
      { x: hw, y: subjectH, z: 0 }, { x: -hw, y: subjectH, z: 0 },
    ], w, hh, "rgba(59,130,246,0.10)", accent);

    // ---- 反光板（仅界面对象）
    for (const r of reflectors) {
      drawPoly(g, [
        { x: r.x - 0.3, y: r.y, z: r.z }, { x: r.x + 0.3, y: r.y, z: r.z },
        { x: r.x + 0.3, y: r.y + 0.9, z: r.z }, { x: r.x - 0.3, y: r.y + 0.9, z: r.z },
      ], w, hh, "rgba(152,165,186,0.10)", muted);
    }

    // ---- 灯具：光锥 + 灯位标记 + 标签
    const target = subjectCenter();
    const labels: { x: number; y: number; text: string; color: string }[] = [];
    const markers: { x: number; y: number; color: string; sel: boolean }[] = [];
    for (let i = 0; i < draft.lights.length; i += 1) {
      const l = draft.lights[i];
      if (!l.visible) continue;
      const p = lightPos(l);
      const col = rgbCss(kelvinRgb(l.kelvin));
      const dir = norm(sub(target, p));
      const rSpot = clamp(spotDiameter(l) / 2, 0.05, 2.2);
      let u = norm(cross(dir, UP));
      if (len(cross(dir, UP)) < 1e-4) u = { x: 1, y: 0, z: 0 };
      const v = cross(dir, u);
      const ring: V3[] = [];
      for (let k = 0; k < 12; k += 1) {
        const t = (k / 12) * Math.PI * 2;
        ring.push({
          x: target.x + rSpot * (Math.cos(t) * u.x + Math.sin(t) * v.x),
          y: target.y + rSpot * (Math.cos(t) * u.y + Math.sin(t) * v.y),
          z: target.z + rSpot * (Math.cos(t) * u.z + Math.sin(t) * v.z),
        });
      }
      const alpha = 0.05 + 0.10 * (l.intensity / 4);
      g.fillStyle = rgbCss(kelvinRgb(l.kelvin), alpha);
      g.strokeStyle = rgbCss(kelvinRgb(l.kelvin), 0.5);
      const scrPos = toCam(p);
      const scrRing = ring.map((q) => (toCam(q).z < NEAR ? null : projCam(toCam(q), w, hh)));
      if (scrPos.z >= NEAR && scrRing.every((q) => q !== null)) {
        const sp = projCam(scrPos, w, hh);
        g.beginPath();
        g.moveTo(sp.x, sp.y);
        for (const q of scrRing) if (q) g.lineTo(q.x, q.y);
        g.closePath();
        g.fill();
        g.stroke();
      }
      // 4 条主光线，强调方向
      g.strokeStyle = rgbCss(kelvinRgb(l.kelvin), 0.35);
      for (let k = 0; k < 12; k += 3) drawSeg(g, p, ring[k], w, hh);

      if (scrPos.z >= NEAR) {
        const sp = projCam(scrPos, w, hh);
        markers.push({ x: sp.x, y: sp.y, color: col, sel: i === selected });
        labels.push({
          x: sp.x, y: sp.y,
          text: `${l.name} · ${Math.round((l.intensity / 4) * 100)}% · ${Math.round(l.kelvin)}K`,
          color: col,
        });
      }
    }

    for (const m of markers) {
      g.beginPath();
      g.arc(m.x, m.y, m.sel ? 7 : 5, 0, Math.PI * 2);
      g.fillStyle = m.color;
      g.fill();
      if (m.sel) {
        g.strokeStyle = "#ffffff";
        g.lineWidth = 2;
        g.stroke();
        g.lineWidth = 1;
      }
    }
    g.font = "11px ui-monospace, Consolas, monospace";
    g.textBaseline = "middle";
    const placed: { x: number; y: number; w: number; h: number }[] = [];
    for (const t of labels) {
      const tw = g.measureText(t.text).width;
      const bx = clamp(t.x + 10, 4, Math.max(w - tw - 12, 4));
      let by = clamp(t.y - 18, 4, hh - 20);
      // 简单贪心避让：与已放置的标签重叠时下移一行，避免灯位标签互相覆盖
      for (let k = 0; k < 5; k += 1) {
        const hit = placed.some((q) => bx - 4 < q.x + q.w && bx + tw + 4 > q.x && by < q.y + q.h && by + 17 > q.y);
        if (!hit) break;
        by = by + 19 > hh - 20 ? 4 : by + 19;
      }
      placed.push({ x: bx - 4, y: by, w: tw + 8, h: 17 });
      g.fillStyle = "rgba(7,9,16,0.78)";
      g.fillRect(bx - 4, by, tw + 8, 17);
      g.fillStyle = t.color;
      g.fillText(t.text, bx, by + 9);
    }

    // ---- 主体标注
    const topMid = toCam({ x: 0, y: subjectH, z: 0 });
    if (topMid.z >= NEAR) {
      const sp = projCam(topMid, w, hh);
      const lab = `${SUBJECT_LABEL} ${subjectW.toFixed(2)}×${subjectH.toFixed(2)} m`;
      const lw = g.measureText(lab).width;
      g.fillStyle = "rgba(7,9,16,0.8)";
      g.fillRect(sp.x - lw / 2 - 6, sp.y - 24, lw + 12, 17);
      g.fillStyle = accent;
      g.textAlign = "center";
      g.fillText(lab, sp.x, sp.y - 15);
      g.textAlign = "left";
    }

    // ---- 左下角坐标轴（由相机基向量推导）
    const ox = 40, oy = hh - 34, L = 20;
    const axes: { v: V3; color: string; name: string }[] = [
      { v: { x: 1, y: 0, z: 0 }, color: "#f87171", name: "X" },
      { v: { x: 0, y: 1, z: 0 }, color: "#34d399", name: "Y" },
      { v: { x: 0, y: 0, z: 1 }, color: "#60a5fa", name: "Z" },
    ];
    for (const ax of axes) {
      const sx = dot(ax.v, cam.right) * L;
      const sy = -dot(ax.v, cam.up) * L;
      g.strokeStyle = ax.color;
      g.lineWidth = 1.6;
      g.beginPath();
      g.moveTo(ox, oy);
      g.lineTo(ox + sx, oy + sy);
      g.stroke();
      g.fillStyle = ax.color;
      g.font = "10px ui-monospace, Consolas, monospace";
      g.fillText(ax.name, ox + sx * 1.35 - 3, oy + sy * 1.35 + 3);
    }
    g.lineWidth = 1;
    g.fillStyle = muted;
    g.font = "10px ui-monospace, Consolas, monospace";
    g.textAlign = "left";
    g.fillText(`${VIEWS.find((v) => v.id === view)?.label ?? ""} · 网格 0.5 m`, 14, hh - 12);
  }

  // -------------------------------------------------------------- 左栏

  function updateObjectRows(): void {
    clear(objList);
    objList.append(h("div", {
      class: "item",
      style: "cursor:default",
    },
      icon("image", 14, "var(--hls-accent)"),
      h("div", { class: "grow" },
        h("div", { class: "item__t" }, `主体 · ${SUBJECT_LABEL}`),
        h("div", { class: "item__d" }, `${subjectW.toFixed(2)} × ${subjectH.toFixed(2)} m · 平面`),
      ),
      h("span", { class: "tiny muted" }, "X 0.0 Y 0.0 Z 0.0"),
    ));

    for (let i = 0; i < draft.lights.length; i += 1) {
      const l = draft.lights[i];
      const p = lightPos(l);
      const b = h("button", { class: i === selected ? "item on" : "item", type: "button" },
        icon("lightbulb", 14, rgbCss(kelvinRgb(l.kelvin))),
        h("div", { class: "grow" },
          h("div", { class: "item__t ellipsis" }, l.name),
          h("div", { class: "item__d" },
            `${l.kind === "point" ? "点光" : "平行光"} · 强度 ${Math.round((l.intensity / 4) * 100)}% · ${Math.round(l.kelvin)}K`),
        ),
        h("span", { class: "tiny muted mono" },
          `X ${p.x.toFixed(1)} Y ${p.y.toFixed(1)} Z ${p.z.toFixed(1)}`),
      );
      b.addEventListener("click", () => { selected = i; renderAll(); });
      objList.append(b);
    }

    for (const r of reflectors) {
      objList.append(h("div", { class: "item", style: "cursor:default" },
        icon("layers", 14, "var(--hls-text-muted)"),
        h("div", { class: "grow" },
          h("div", { class: "item__t" }, r.name),
          h("div", { class: "item__d" }, "仅界面对象 · 不参与引擎渲染"),
        ),
        h("span", { class: "tiny muted mono" },
          `X ${r.x.toFixed(1)} Y ${r.y.toFixed(1)} Z ${r.z.toFixed(1)}`),
      ));
    }
  }

  function renderObjects(): void {
    objListHost.replaceChildren(objList);
    updateObjectRows();
    objCount.textContent = `${1 + draft.lights.length + reflectors.length} 对象`;
  }

  const objListHost = h("div", { class: "panel__body" });
  const objCount = h("span", { class: "chip" });

  function renderSpace(): void {
    clear(spaceHost);
    spaceHost.append(h("div", { class: "card" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:6px" },
        icon("radar", 13, "var(--hls-text-muted)"),
        h("span", { style: "font-size:11.5px;font-weight:600" }, "空间信息"),
        h("span", { class: "chip" }, "PREVIS"),
      ),
      h("div", { class: "kv" }, h("span", {}, "棚体尺寸"), h("b", {}, "3.0 × 3.0 × 2.6 m")),
      h("div", { class: "kv" }, h("span", {}, "虚拟视角"), h("b", {}, "54°（预演相机）")),
      h("div", { class: "grid2", style: "margin-top:6px" },
        numberField("主体宽 (m)", subjectW, 0.2, 3, 0.05, (v) => {
          subjectW = v; renderObjects(); draw(); renderRight();
        }),
        numberField("主体高 (m)", subjectH, 0.2, 3, 0.05, (v) => {
          subjectH = v; renderObjects(); draw(); renderRight();
        }),
      ),
      h("div", { class: "tiny muted", style: "margin-top:6px;line-height:1.6" },
        "引擎坐标是归一化量，预演按 3.0 × 3.0 × 2.6 m 虚拟棚体线性映射，非实拍米数。"),
    ));
  }

  // -------------------------------------------------------------- 右栏

  function renderRight(): void {
    clear(rightBody);
    const l = draft.lights[selected];
    if (!l) {
      rightBody.append(h("div", { class: "card small muted" }, "场景中没有灯具。点击左侧「添加灯具」新建一盏。"));
      return;
    }
    const isPoint = l.kind === "point";

    rightBody.append(h("div", { class: "card", style: "display:flex;flex-direction:column;gap:9px" },
      h("div", { class: "row row--between" },
        h("div", { class: "row", style: "gap:7px" },
          icon("lightbulb", 14, rgbCss(kelvinRgb(l.kelvin))),
          h("span", { class: "ellipsis", style: "font-size:12px;font-weight:600" }, l.name),
        ),
        h("span", { class: "chip" }, isPoint ? "点光" : "平行光"),
      ),
      h("div", { class: "divider" }),

      h("div", { class: "tiny muted" }, "空间坐标（米）"),
      (() => {
        const p = lightPos(l);
        const grid = h("div", { class: "grid3" },
          numberField("X", r2(p.x), -1.5, 1.5, 0.05, (v) => {
            const q = lightPos(l); q.x = clamp(v, -1.5, 1.5); posToNormalized(l, q); refreshAfterEdit();
          }),
          numberField("Y", r2(p.y), 0, STUDIO.h, 0.05, (v) => {
            const q = lightPos(l); q.y = clamp(v, 0, STUDIO.h); posToNormalized(l, q); refreshAfterEdit();
          }),
          numberField("Z", r2(p.z), 0, STUDIO.d, 0.05, (v) => {
            const q = lightPos(l); q.z = clamp(v, 0, STUDIO.d); posToNormalized(l, q); refreshAfterEdit();
          }),
        );
        if (!isPoint || locked) {
          for (const inp of grid.querySelectorAll("input")) inp.disabled = true;
        }
        return grid;
      })(),
      !isPoint
        ? h("div", { class: "tiny muted" }, "平行光无物理灯位（引擎只用方向），此处为沿方向 2.4 m 的虚拟灯位。")
        : locked ? h("div", { class: "tiny", style: "color:var(--hls-warm)" }, "已锁定位置：坐标输入不可编辑。") : null,

      h("div", { class: "divider" }),
      h("div", { class: "tiny muted" }, "投射角度"),
      (() => {
        const grid = h("div", { class: "grid3" },
          numberField("α 方位 (°)", r2(azimuth(l)), -90, 90, 1, (v) => { setDirection(l, v, elevation(l)); refreshAfterEdit(); }),
          numberField("β 俯仰 (°)", r2(elevation(l)), -85, 85, 1, (v) => { setDirection(l, azimuth(l), v); refreshAfterEdit(); }),
          numberField("θ 光束角 (°)", r2(beamAngle(l)), 3, 80, 1, (v) => { l.radius = radiusFromAngle(v); refreshAfterEdit(); }),
        );
        if (isPoint) {
          const inputs = grid.querySelectorAll("input");
          if (inputs[0]) inputs[0].disabled = true;
          if (inputs[1]) inputs[1].disabled = true;
        }
        return grid;
      })(),
      h("div", { class: "tiny muted" },
        isPoint ? "点光方向由灯位指向主体，故 α/β 为推导值（不可编辑）。" : "α/β 写入引擎方向向量 dx/dy/dz。"),
      h("div", { class: "tiny muted" }, "θ = 2·atan(radius × 0.35)，为界面估算。"),

      h("div", { class: "divider" }),
      slider({
        label: "色温", min: 1500, max: 12000, step: 100, value: l.kelvin,
        format: (v) => `${Math.round(v)} K`,
        onInput: (v) => { l.kelvin = v; refreshAfterEdit(); },
      }),
      slider({
        label: `强度（引擎量程 0~4）`, min: 0, max: 4, step: 0.01, value: l.intensity,
        format: (v) => `${Math.round((v / 4) * 100)}%`,
        onInput: (v) => { l.intensity = v; refreshAfterEdit(); },
      }),
      h("div", { class: "kv" }, h("span", {}, "光斑直径"),
        h("b", { "data-spot": "1" }, `≈ ${spotDiameter(l).toFixed(2)} m（估算）`)),
    ));

    rightBody.append(h("div", { class: "card" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:4px" },
        icon("image", 13, "var(--hls-text-muted)"),
        h("span", { style: "font-size:11.5px;font-weight:600" }, "受光素材"),
      ),
      h("div", { class: "kv" }, h("span", {}, "物理尺寸"), h("b", {}, `${subjectW.toFixed(2)} × ${subjectH.toFixed(2)} m`)),
      h("div", { class: "kv" }, h("span", {}, `距${isPoint ? "灯位" : "虚拟灯位"}`), h("b", {}, `${lightDistance(l).toFixed(2)} m`)),
      h("div", { class: "kv" }, h("span", {}, "表面模型"), h("b", {}, "漫反射（Lambert，引擎固定）")),
    ));

    rightBody.append(h("div", { class: "row", style: "gap:6px" },
      button(locked ? "已锁定位置" : "锁定位置", () => {
        locked = !locked;
        renderRight();
      }, { icon: "crosshair", variant: "ghost", on: locked }),
      h("span", { class: "spacer" }),
      button("应用布光", () => {
        setParams(structuredClone(draft));
        toast("布光已应用到调光台", "ok");
      }, { icon: "check", variant: "primary" }),
    ));
  }

  /** 参数改动后：重画 3D、刷新左栏数值与右栏派生读数（不重建输入控件，避免打断拖动） */
  function refreshAfterEdit(): void {
    updateObjectRows();
    draw();
    const l = draft.lights[selected];
    if (!l) return;
    const spot = rightBody.querySelector("[data-spot]");
    if (spot) spot.textContent = `≈ ${spotDiameter(l).toFixed(2)} m（估算）`;
    renderFooter();
  }

  // -------------------------------------------------------------- 底栏

  function renderFooter(): void {
    clear(footerHost);
    const ratio = keyFillRatio();
    footerHost.append(
      h("span", {}, `对象 ${1 + draft.lights.length + reflectors.length}`),
      h("span", {}, "网格 0.5 m"),
      h("span", {}, `主光:补光 ${ratio === null ? "—" : `${ratio.toFixed(2)} : 1`}`),
      h("span", {}, `相对环境强度 ${draft.ambient_intensity.toFixed(2)}（相对值，引擎量程 0~2）`),
      h("span", { class: "spacer" }),
      h("span", {}, "光比按强度降序（最高:次高）推导；反光板仅界面对象，不影响引擎渲染"),
    );
  }

  // -------------------------------------------------------------- 视图

  function applyView(v: ViewId): void {
    view = v;
    if (v === "persp") { yaw = -0.7; pitch = 0.32; dist = 7.6; }
    else if (v === "top") { yaw = 0; pitch = 1.42; dist = 7.2; }
    else if (v === "side") { yaw = Math.PI / 2; pitch = 0.12; dist = 7.0; }
    syncView();
    draw();
  }

  function syncView(): void {
    viewLabel.textContent = `${VIEWS.find((x) => x.id === view)?.label ?? ""}视图 · 54°`;
    viewHost.replaceChildren(viewLabel, segmented(VIEWS, view, applyView));
  }

  // -------------------------------------------------------------- 交互

  cv.addEventListener("pointerdown", (e) => {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    cv.setPointerCapture(e.pointerId);
    cv.style.cursor = "grabbing";
  });
  cv.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    yaw -= dx * 0.007;
    pitch = clamp(pitch + dy * 0.007, -1.45, 1.45);
    if (view !== "free") { view = "free"; syncView(); }
    draw();
  });
  cv.addEventListener("pointerup", (e) => {
    dragging = false;
    cv.style.cursor = "grab";
    cv.releasePointerCapture(e.pointerId);
  });
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    dist = clamp(dist * (e.deltaY > 0 ? 1.1 : 0.9), 2.6, 22);
    draw();
  }, { passive: false });

  // -------------------------------------------------------------- 装配

  function renderAll(): void {
    renderObjects();
    renderSpace();
    renderRight();
    renderFooter();
    headChip.textContent = `${1 + draft.lights.length + reflectors.length} 对象`;
    window.setTimeout(() => draw(), 0);
  }

  return {
    id: "previs",
    name: "三维布光预演",
    subtitle: "3D LIGHTING PREVIS · 平面主体灯位预演（相对光照模型）",
    icon: "crosshair",
    mount(root: HTMLElement): void {
      draft = structuredClone(currentParams());
      selected = 0;
      const inner = h("div", { style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column" });
      const head = h("div", {
        style: "flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--hls-border);background:var(--hls-bg-panel)",
      },
        icon("crosshair", 15, "var(--hls-accent)"),
        h("div", {},
          h("div", { style: "font-size:13px;font-weight:600" }, "三维布光预演"),
          h("div", { class: "tiny muted" }, "3D LIGHTING PREVIS · 平面主体灯位预演（相对光照模型）"),
        ),
        h("span", { class: "spacer" }),
        headChip,
      );
      const body = h("div", { style: "flex:1;min-height:0;display:flex" },
        h("div", { class: "panel", style: "width:290px;flex:0 0 290px" },
          h("div", { class: "panel__head" }, "场景对象 · SCENE OBJECTS", h("span", { class: "spacer" }), objCount),
          objListHost,
          h("div", { class: "panel__body", style: "padding-top:0" },
            h("div", { class: "row", style: "gap:6px" },
              button("添加灯具", () => {
                const n = draft.lights.length + 1;
                draft.lights.push({
                  name: `新增灯具 ${n}`, kind: "point",
                  dx: 0, dy: -0.5, dz: 0.85,
                  px: 0.5, py: 0.35, pz: 1.2,
                  intensity: 0.7, kelvin: 5500, radius: 0.5, visible: true,
                });
                selected = draft.lights.length - 1;
                renderAll();
              }, { icon: "plus" }),
              button("添加反光板", () => {
                const n = reflectors.length;
                reflectors.push({ id: n + 1, name: `反光板 · 银面 ${n + 1}`, x: 1.2 - n * 0.7, y: 0, z: 0.6 });
                renderAll();
              }, { icon: "plus", variant: "ghost" }),
            ),
            h("div", { class: "tiny muted", style: "margin-top:6px;line-height:1.6" },
              "反光板为界面对象，不写入渲染参数、不影响引擎出图。"),
          ),
          h("div", { class: "panel__head" }, "空间信息"),
          spaceHost,
        ),
        h("div", { class: "main" },
          h("div", { style: "flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:10px 14px" },
            viewHost,
            h("span", { class: "spacer" }),
            h("span", { class: "tiny muted" }, "左键旋转 · 滚轮缩放"),
          ),
          cv,
        ),
        h("div", { class: "panel panel--right", style: "width:330px;flex:0 0 330px" },
          h("div", { class: "panel__head" }, "灯具参数 · FIXTURE"),
          rightBody),
      );
      inner.append(head, body, footerHost);
      root.append(inner);
      applyView("persp");
      renderAll();
      ro.observe(cv);
    },
    unmount(): void {
      ro.disconnect();
      dragging = false;
    },
  };
}
