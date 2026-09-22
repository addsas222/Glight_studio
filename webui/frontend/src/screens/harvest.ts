/**
 * B4 智能拾光：从实拍参考图中提取专业灯组方案。
 *
 * 诚实说明（engines honesty rule）：
 *  - 引擎只计算相对亮度与 Lambert-Phong 光照，无法推导绝对照度(lx)、CRI、
 *    Δuv、功率等物理量，因此界面**不展示**设计稿里的“总功率 / 覆盖面积 /
 *    采样密度 / 显色指数”等读数，只展示引擎真实返回的量：角色、色温、
 *    相对强度、置信度、关键光比、对比度、光源数量，以及后端 notes 原文。
 *  - 派生量只有两个，且在界面上写明算法：
 *      色温一致性 = 1 − (最高色温 − 最低色温) / 引擎色温量程(10500K)
 *      相对强度   = light.intensity / 引擎强度上限(4.0)
 */
import { api } from "../api";
import type { HarvestResult, Light, RenderParams } from "../api";
import type { Screen } from "../shell";
import { setParams } from "../shell";
import { button, clear, h, icon, reportError, slider, store, toast } from "../ui";

// ---------------------------------------------------------------- 角色映射

interface RoleStyle { label: string; token: string; }

/** 角色 → 中文名 + 主题色令牌（与设计稿配色一致：主光暖黄 / 辅光蓝 / 轮廓光紫） */
const ROLE_TABLE: Record<string, RoleStyle> = {
  key: { label: "主光", token: "--hls-warm" },
  fill: { label: "辅光", token: "--hls-accent-bright" },
  rim: { label: "轮廓光", token: "--hls-violet" },
  back: { label: "逆光", token: "--hls-violet" },
  ambient: { label: "环境光", token: "--hls-success" },
  top: { label: "顶光", token: "--hls-accent" },
  side: { label: "侧光", token: "--hls-text-secondary" },
};

const NEUTRAL: RoleStyle = { label: "光源", token: "--hls-text-secondary" };

/** 后端 role 可能是英文键也可能是中文名，统一归一化到角色表 */
const ROLE_ALIAS: Record<string, string> = {
  主光: "key", 主光源: "key", 辅光: "fill", 补光: "fill",
  轮廓光: "rim", 轮廓: "rim", 逆光: "back", 背景光: "back",
  环境光: "ambient", 顶光: "top", 侧光: "side",
};

/** 归一化后的角色键（用于叠加层开关的稳定标识） */
function roleKey(role: string): string {
  const raw = role.trim();
  return ROLE_ALIAS[raw] ?? raw.toLowerCase();
}

function roleStyle(role: string): RoleStyle {
  const raw = role.trim();
  const hit = ROLE_TABLE[roleKey(role)];
  return hit ? hit : { ...NEUTRAL, label: raw || NEUTRAL.label };
}

function cssVar(token: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return v || "#8b98ad";
}

/** 置信度可能是 0~1 或 0~100，两种都按真实值换算为百分比 */
function confidencePct(v: number): number {
  const pct = v <= 1 ? v * 100 : v;
  return Math.max(0, Math.min(100, pct));
}

/** 色温一致性：1 − 极差 / 引擎色温量程（1500~12000K → 10500K） */
function kelvinConsistency(lights: Light[]): number {
  if (lights.length < 2) return 1;
  const ks = lights.map((l) => l.kelvin);
  const spread = Math.max(...ks) - Math.min(...ks);
  return Math.max(0, 1 - spread / 10500);
}

const INTENSITY_MAX = 4;

// ---------------------------------------------------------------- 屏幕

const PIPELINE = ["场景采样", "光源分离", "灯组映射"];
type Phase = "pending" | "active" | "done";

export function createHarvestScreen(): Screen {
  let refFile: File | null = null;
  let refUrl: string | null = null;
  let result: HarvestResult | null = null;
  let draft: RenderParams | null = null;
  let busy = false;
  let token = 0;
  const hiddenRoles = new Set<string>();
  const phases: Phase[] = ["pending", "pending", "pending"];
  let overlays: { wrap: HTMLElement; cv: HTMLCanvasElement; img: HTMLImageElement }[] = [];
  let revealTimers: number[] = [];

  const ro = new ResizeObserver(() => drawAll());

  const fileInput = h("input", { type: "file", accept: "image/*", style: "display:none" });
  fileInput.addEventListener("change", () => {
    const f = fileInput.files?.[0];
    if (f) void run(f);
  });

  const leftBody = h("div", { class: "panel__body" });
  const stage = h("div", { class: "stage" });
  const rightBody = h("div", { class: "panel__body" });
  const headRight = h("div", { class: "row", style: "gap:6px" });
  const headChip = h("div", { class: "chip" }, "待选择参考图");

  // -------------------------------------------------------------- 绘图

  function registerOverlay(wrap: HTMLElement, cv: HTMLCanvasElement, img: HTMLImageElement): void {
    overlays.push({ wrap, cv, img });
    ro.observe(wrap);
  }

  /** 让叠加层精确覆盖「contain 之后的图像内容框」，箭头坐标才不会与图像错位 */
  function fitOverlay(wrap: HTMLElement, cv: HTMLCanvasElement, img: HTMLImageElement): void {
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const bw = wrap.clientWidth, bh = wrap.clientHeight;
    if (!nw || !nh || !bw || !bh) return;
    const k = Math.min(bw / nw, bh / nh);
    const w = Math.round(nw * k), h = Math.round(nh * k);
    cv.style.width = `${w}px`;
    cv.style.height = `${h}px`;
    cv.style.left = `${Math.round((bw - w) / 2)}px`;
    cv.style.top = `${Math.round((bh - h) / 2)}px`;
  }

  /** 在参考图上绘制各光源的方向箭头（由返回的 dx/dy 推导，不代表物理灯位） */
  function drawOverlay(cv: HTMLCanvasElement, img: HTMLImageElement): void {
    const w = cv.clientWidth, hh = cv.clientHeight;
    const g = cv.getContext("2d");
    if (!g || !w || !hh || !img.getAttribute("src")) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(hh * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hh);
    if (!result) return;

    const cx = w / 2, cy = hh / 2;
    const R = Math.min(w, hh) * 0.4;
    for (const hl of result.lights) {
      if (hiddenRoles.has(roleKey(hl.role))) continue;
      const style = roleStyle(hl.role);
      const col = cssVar(style.token);
      const dx = hl.light.dx, dy = hl.light.dy;
      const len = Math.hypot(dx, dy);
      const ux = len > 1e-4 ? dx / len : 0;
      const uy = len > 1e-4 ? dy / len : 0;
      const x0 = cx + ux * R * 0.22, y0 = cy + uy * R * 0.22;
      const x1 = cx + ux * R, y1 = cy + uy * R;

      g.strokeStyle = col;
      g.fillStyle = col;
      g.lineWidth = 2;
      g.beginPath();
      g.moveTo(x0, y0);
      g.lineTo(x1, y1);
      g.stroke();
      if (len > 1e-4) {
        const ang = Math.atan2(y1 - y0, x1 - x0);
        g.beginPath();
        g.moveTo(x1, y1);
        g.lineTo(x1 - 10 * Math.cos(ang - 0.4), y1 - 10 * Math.sin(ang - 0.4));
        g.lineTo(x1 - 10 * Math.cos(ang + 0.4), y1 - 10 * Math.sin(ang + 0.4));
        g.closePath();
        g.fill();
      }
      g.beginPath();
      g.arc(x1, y1, 5, 0, Math.PI * 2);
      g.fill();

      const label = `${style.label} ${Math.round(confidencePct(hl.confidence))}%`;
      g.font = "11px ui-monospace, Consolas, monospace";
      g.textBaseline = "middle";
      const tw = g.measureText(label).width;
      const bx = Math.min(Math.max(x1 + 9, 4), Math.max(w - tw - 14, 4));
      const by = Math.min(Math.max(y1 - 9, 4), hh - 20);
      g.fillStyle = "rgba(7,9,16,0.78)";
      g.fillRect(bx - 4, by, tw + 8, 17);
      g.fillStyle = col;
      g.fillText(label, bx, by + 9);
    }
  }

  function drawAll(): void {
    for (const { wrap, cv, img } of overlays) {
      fitOverlay(wrap, cv, img);
      drawOverlay(cv, img);
    }
  }

  // -------------------------------------------------------------- 选择参考图

  async function run(f: File): Promise<void> {
    refFile = f;
    if (refUrl) URL.revokeObjectURL(refUrl);
    refUrl = URL.createObjectURL(f);
    result = null;
    draft = null;
    busy = true;
    hiddenRoles.clear();
    token += 1;
    const my = token;
    phases[0] = "active";
    phases[1] = "pending";
    phases[2] = "pending";
    renderAll();
    try {
      const res = await api.harvest(f, f.name);
      if (my !== token) return;
      result = res;
      draft = structuredClone(res.params);
      phases[0] = "done";
      phases[1] = "done";
      phases[2] = "done";
      renderAll();
      toast(`拾光完成：识别 ${res.lights.length} 个光源`, "ok");
    } catch (e) {
      if (my !== token) return;
      phases[0] = "pending";
      phases[1] = "pending";
      phases[2] = "pending";
      renderAll();
      reportError(e);
    } finally {
      if (my === token) {
        busy = false;
        renderAll();
      }
    }
  }

  // -------------------------------------------------------------- 左栏

  function stepDetail(i: number): string {
    if (!result) return i === 0 && busy ? "采样中…" : "待运行";
    if (i === 0) return `场景类型 ${result.scene_type} · 环境色温 ${Math.round(result.ambient_kelvin)}K`;
    if (i === 1) {
      const avg = result.lights.length
        ? result.lights.reduce((acc, l) => acc + confidencePct(l.confidence), 0) / result.lights.length
        : 0;
      return `识别 ${result.lights.length} 个光源 · 平均置信度 ${avg.toFixed(0)}%`;
    }
    return `映射灯组 ${result.params.lights.length} · 关键光比 ${result.key_to_fill_ratio.toFixed(2)} : 1`;
  }

  function renderLeft(): void {
    clear(leftBody);
    overlays = [];
    ro.disconnect();

    const card = h("div", { class: "card" });
    card.append(
      h("div", { class: "row row--between" },
        h("div", { class: "row", style: "gap:6px" }, icon("camera", 13, "var(--hls-text-muted)"),
          h("span", { style: "font-size:11.5px;font-weight:600" }, "场景采集")),
        h("span", { class: "tiny", style: `color:${result ? "var(--hls-success)" : "var(--hls-text-muted)"}` },
          result ? "已解析" : busy ? "解析中" : "待运行"),
      ),
    );

    if (refUrl) {
      const img = h("img", {
        alt: "参考图",
        style: "display:block;width:100%;height:100%;object-fit:contain",
      });
      img.src = refUrl;
      img.addEventListener("load", () => drawAll());
      const cv = h("canvas", { style: "position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none" });
      const wrap = h("div", {
        style: "position:relative;width:100%;height:150px;margin-top:8px;border-radius:6px;overflow:hidden;background:var(--hls-bg-canvas)",
      }, img, cv);
      card.append(wrap);
      registerOverlay(wrap, cv, img);
    }

    card.append(
      kvRow("场景类型", result ? result.scene_type : "—"),
      kvRow("环境色温", result ? `${Math.round(result.ambient_kelvin)} K` : "—"),
      kvRow("识别光源", result ? `${result.lights.length} 个` : "—"),
    );

    if (result && result.lights.length) {
      const dots = h("div", { class: "row", style: "flex-wrap:wrap;gap:6px;margin-top:6px" });
      const seen = new Set<string>();
      for (const hl of result.lights) {
        const key = roleKey(hl.role);
        if (seen.has(key)) continue;
        seen.add(key);
        const style = roleStyle(hl.role);
        dots.append(h("span", { class: "chip" },
          h("i", { class: "dot", style: `background:var(${style.token})` }), style.label));
      }
      card.append(dots);
    }

    card.append(h("div", { class: "divider" }));
    card.append(h("div", { class: "row", style: "gap:6px;flex-wrap:wrap" },
      button("选择参考图", () => fileInput.click(), { icon: "upload", variant: "primary" }),
      button("重新分析", () => { if (refFile) void run(refFile); }, { variant: "ghost", icon: "rotate" }),
    ));
    if (refFile) card.append(h("div", { class: "tiny muted ellipsis", style: "margin-top:6px" }, `参考图：${refFile.name}`));
    leftBody.append(card);

    leftBody.append(h("div", { class: "card" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:8px" },
        icon("layers", 13, "var(--hls-text-muted)"),
        h("span", { style: "font-size:11.5px;font-weight:600" }, "光源解析"),
        h("span", { class: "tiny muted" }, "场景采样 → 光源分离 → 灯组映射"),
      ),
      buildSteps(),
    ));

    leftBody.append(h("div", { class: "small muted", style: "padding:0 2px" },
      "引擎只计算相对亮度与 Lambert 光照；色温、相对强度与光比为真实推导值。"));
  }

  /** 解析流程：状态只由真实事件推进（请求开始 / 响应返回），数值即响应内容 */
  function buildSteps(): HTMLElement {
    const list = h("div", { class: "list" });
    for (let i = 0; i < PIPELINE.length; i += 1) {
      const phase = phases[i];
      const col = phase === "done" ? "var(--hls-success)" : phase === "active" ? "var(--hls-accent)" : "var(--hls-text-muted)";
      const bar = h("i", { style: `width:0%;background:${col}` });
      const row = h("div", { style: "padding:5px 0" },
        h("div", { class: "row", style: "gap:7px" },
          h("i", { class: "dot", style: `background:${col}` }),
          h("span", { class: "small", style: "font-weight:600" }, `${i + 1}. ${PIPELINE[i]}`),
          h("span", { class: "spacer" }),
          h("span", { class: "tiny muted" }, phase === "done" ? "完成" : phase === "active" ? "运行中" : "待运行"),
        ),
        h("div", { class: "tiny muted", style: "padding-left:14px;margin:3px 0 4px" }, stepDetail(i)),
        h("div", { class: "bar", style: "margin-left:14px" }, bar),
      );
      list.append(row);
      // 进度条只做呈现节奏：数据在响应返回时已确定，不虚构中间进度
      if (phase === "done") {
        revealTimers.push(window.setTimeout(() => { bar.style.width = "100%"; }, i * 160));
      }
    }
    return list;
  }

  // -------------------------------------------------------------- 中栏

  /** 叠加层开关行：只重建这一行，避免每次切换都重载参考图 */
  function renderToggles(): void {
    headRight.replaceChildren();
    const present = new Set(distinctRoles().map((role) => roleKey(role)));
    for (const role of distinctRoles()) {
      const style = roleStyle(role);
      const key = roleKey(role);
      headRight.append(button(style.label, () => {
        if (hiddenRoles.has(key)) hiddenRoles.delete(key); else hiddenRoles.add(key);
        renderToggles();
        drawAll();
      }, { variant: "ghost", on: !hiddenRoles.has(key) }));
    }
    if (!present.size) {
      headRight.append(h("span", { class: "tiny muted" }, "解析后可切换各光源的方向箭头"));
    }
    headRight.append(button("重新分析", () => { if (refFile) void run(refFile); }, { icon: "rotate", variant: "ghost" }));
  }

  function renderCenter(): void {
    renderToggles();
    renderStage();
  }

  /** 中栏舞台（每次 renderAll 重建，overlays 已在 renderLeft 里清空） */
  function renderStage(): void {
    clear(stage);
    if (!refUrl) {
      stage.append(h("div", { style: "text-align:center" },
        icon("sparkles", 30, "var(--hls-text-muted)"),
        h("div", { class: "sec", style: "margin:10px 0 4px" }, "选择一张参考图，自动提取专业灯组"),
        h("div", { class: "small muted", style: "max-width:360px;margin-bottom:12px" },
          "引擎会分离主光 / 辅光 / 轮廓光，给出真实推导的色温、相对强度与关键光比。"),
        button("选择参考图", () => fileInput.click(), { icon: "upload", variant: "primary" }),
      ));
      return;
    }

    const img = h("img", { alt: "参考图", style: "display:block;width:100%;height:100%;object-fit:contain" });
    img.src = refUrl;
    img.addEventListener("load", () => drawAll());
    const cv = h("canvas", { style: "position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none" });
    const wrap = h("div", {
      style: "position:relative;flex:1;align-self:stretch;min-width:0;min-height:0;overflow:hidden",
    }, img, cv);
    stage.append(wrap);
    registerOverlay(wrap, cv, img);
  }

  function distinctRoles(): string[] {
    if (!result) return [];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const hl of result.lights) {
      const key = roleKey(hl.role);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(hl.role);
    }
    return out;
  }

  // -------------------------------------------------------------- 右栏

  function renderRight(): void {
    clear(rightBody);
    if (!result || !draft) {
      rightBody.append(h("div", { class: "card small muted" },
        "尚未解析。选择参考图后，这里会列出每个光源的角色、色温、相对强度、置信度与推导依据。"));
      return;
    }

    for (let i = 0; i < result.lights.length; i += 1) {
      const hl = result.lights[i];
      const style = roleStyle(hl.role);
      const light = hl.light;
      const barFill = h("i", { style: `width:${(light.intensity / INTENSITY_MAX) * 100}%;background:var(${style.token})` });
      const card = h("div", { class: "card" },
        h("div", { class: "row row--between" },
          h("span", { class: "chip", style: `border-color:var(${style.token});color:var(${style.token})` }, style.label),
          h("span", { class: "tiny muted" }, `置信度 ${confidencePct(hl.confidence).toFixed(0)}%`),
        ),
        h("div", { class: "row row--between", style: "margin:7px 0 3px" },
          h("span", { class: "small", style: "font-weight:600" }, light.name),
          h("span", { class: "tiny muted" }, `${light.kind === "point" ? "点光" : "平行光"} · ${Math.round(light.kelvin)}K`),
        ),
        h("div", { class: "bar" }, barFill),
        h("div", { class: "tiny muted", style: "margin:3px 0 6px" },
          `相对强度 ${(light.intensity / INTENSITY_MAX * 100).toFixed(0)}%（引擎量程 0~4）· 方向 dx ${light.dx.toFixed(2)} dy ${light.dy.toFixed(2)}`),
        slider({
          label: "强度",
          min: 0, max: INTENSITY_MAX, step: 0.01,
          value: light.intensity,
          format: (v) => `${Math.round((v / INTENSITY_MAX) * 100)}%`,
          onInput: (v) => {
            if (!draft) return;
            draft.lights[i].intensity = v;
            barFill.style.width = `${(v / INTENSITY_MAX) * 100}%`;
          },
        }),
        h("div", { class: "small sec", style: "margin-top:7px;line-height:1.65" }, `依据：${hl.basis}`),
      );
      rightBody.append(card);
    }

    rightBody.append(h("div", { class: "row", style: "gap:6px" },
      button("应用建议", () => {
        if (!result) return;
        draft = structuredClone(result.params);
        renderRight();
        toast("已载入拾光建议强度", "ok");
      }, { icon: "wand", variant: "ghost" }),
      h("span", { class: "spacer" }),
      h("span", { class: "tiny muted" }, "重置为引擎建议值"),
    ));

    const consistency = kelvinConsistency(result.params.lights);
    rightBody.append(h("div", { class: "card" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:4px" },
        icon("gauge", 13, "var(--hls-text-muted)"),
        h("span", { style: "font-size:11.5px;font-weight:600" }, "灯光方案"),
        h("span", { class: "chip" }, `${result.lights.length} 灯`),
      ),
      kvRow("灯具数量", `${result.params.lights.length} 盘`),
      kvRow("色温一致性", `${(consistency * 100).toFixed(0)}%`),
      kvRow("关键光比", `${result.key_to_fill_ratio.toFixed(2)} : 1`),
      kvRow("对比度", result.contrast.toFixed(2)),
      kvRow("环境色温", `${Math.round(result.ambient_kelvin)} K`),
      h("div", { class: "tiny muted", style: "margin-top:6px;line-height:1.6" },
        "色温一致性 = 1 − 色温极差 ÷ 引擎量程（10500K）；光比与对比度由引擎返回。"),
    ));

    rightBody.append(button("应用到调光台", () => {
      if (!draft) return;
      setParams(draft);
      toast("已应用到调光台，其他屏幕同步生效", "ok");
    }, { icon: "sliders", variant: "primary" }));
  }

  // -------------------------------------------------------------- 说明与装配

  /** 后端 notes 原样展示：它们明确说明 lx / CRI / Δuv 不可推导 */
  function renderNotes(): void {
    clear(notesHost);
    if (!result || !result.notes.length) return;
    notesHost.append(h("div", { class: "card", style: "margin:10px 14px" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:5px" },
        icon("info", 13, "var(--hls-accent)"),
        h("span", { style: "font-size:11px;font-weight:600" }, "引擎说明（后端原文）"),
      ),
      ...result.notes.map((n) => h("div", { class: "small sec", style: "line-height:1.7" }, `· ${n}`)),
    ));
  }

  function renderAll(): void {
    for (const t of revealTimers) window.clearTimeout(t);
    revealTimers = [];
    overlays = [];
    ro.disconnect();
    headChip.textContent = result
      ? `已解析 ${result.lights.length} 个光源`
      : busy ? "解析中…" : "待选择参考图";
    renderLeft();
    renderCenter();
    renderRight();
    renderNotes();
    window.setTimeout(() => drawAll(), 0);
  }

  const notesHost = h("div", { style: "flex:0 0 auto;max-height:150px;overflow-y:auto;border-top:1px solid var(--hls-border)" });

  return {
    id: "harvest",
    name: "智能拾光",
    subtitle: "场景采集 · 识别光源 · 应用建议",
    icon: "sparkles",
    mount(root: HTMLElement): void {
      const inner = h("div", { style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column" });
      const head = h("div", {
        style: "flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--hls-border);background:var(--hls-bg-panel)",
      },
        icon("sparkles", 15, "var(--hls-accent)"),
        h("div", {},
          h("div", { style: "font-size:13px;font-weight:600" }, "智能拾光"),
          h("div", { class: "tiny muted" }, "AI LIGHT HARVEST · 从实拍场景提取专业灯光方案"),
        ),
        h("span", { class: "spacer" }),
        headChip,
        store.ctx.state ? h("span", { class: "chip" }, `引擎 v${store.ctx.state.version}`) : null,
      );
      const body = h("div", { style: "flex:1;min-height:0;display:flex" },
        h("div", { class: "panel", style: "width:270px;flex:0 0 270px" },
          h("div", { class: "panel__head" }, "场景采集 · SCENE CAPTURE"),
          leftBody),
        h("div", { class: "main", style: "min-height:0" },
          h("div", {
            style: "flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:10px 14px",
          },
            h("span", { style: "font-size:11.5px;font-weight:600" }, "光源解析"),
            h("span", { class: "tiny muted" }, "LIGHT DECOMPOSITION"),
            h("span", { class: "spacer" }),
            headRight,
          ),
          stage,
          notesHost,
        ),
        h("div", { class: "panel panel--right", style: "width:340px;flex:0 0 340px" },
          h("div", { class: "panel__head" }, "识别光源 · 方案与微调"),
          rightBody),
      );
      inner.append(head, body, fileInput);
      root.append(inner);
      renderAll();
    },
    unmount(): void {
      ro.disconnect();
      for (const t of revealTimers) window.clearTimeout(t);
      revealTimers = [];
      token += 1;
      if (refUrl) URL.revokeObjectURL(refUrl);
      refUrl = null;
      refFile = null;
      result = null;
      draft = null;
      hiddenRoles.clear();
      overlays = [];
      phases[0] = "pending";
      phases[1] = "pending";
      phases[2] = "pending";
    },
  };
}

// ---------------------------------------------------------------- 小工具

function kvRow(label: string, value: string): HTMLElement {
  return h("div", { class: "kv" }, h("span", {}, label), h("b", {}, value));
}
