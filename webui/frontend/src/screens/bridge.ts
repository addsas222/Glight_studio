/**
 * B2 光影指挥屏（Lighting Command Bridge）—— 真实灯组的指挥总览。
 *
 * 顶栏：项目名、机位（界面级）、布光中/待机状态、执行 CUE。
 * 左栏：灯具阵列（搜索 + 分类筛选），行内显示真实强度%、色温与 visible 状态。
 * 中栏：光位 / 照度 / 热力 三种视图，全部由光源参数实时推导；下方为相机卡片。
 * 右栏：所选灯具的实时遥测（色温 / 相对强度 / 半径 / 方位角 / 仰角 / 相对占比）
 *       与会话内事件日志（只记录本屏幕真实发生的操作）。
 * 底栏：CUE 时间轴（真实 CUE，点击应用）+ 新建 CUE。
 *
 * 诚实规则：引擎只计算相对 Lambert/Phong 光照。照度 lx、CRI、Δuv、频闪、
 * DMX、灯具温度、GPU/磁盘遥测本引擎无法得出，一律不显示；照度/热力视图
 * 是由光源参数推导的**相对**界面示意图（明确标注非 lx、非引擎输出）。
 */
import { api, type Cue, type Light, type RenderParams } from "../api";
import { button, clear, h, icon, reportError, segmented, store, toast } from "../ui";
import { currentParams, setParams, type Screen } from "../shell";

const INTENSITY_MAX = 4;
const KELVIN_MIN = 1500;
const KELVIN_MAX = 12000;
const RADIUS_MIN = 0.05;
const RADIUS_MAX = 1.5;
/** 相对强度场网格分辨率（越小越糊、越大越慢；仅界面可视化用） */
const FIELD_N = 128;

type Cat = "all" | "key" | "amb" | "fx";
type ViewId = "plot" | "lux" | "heat";
type CamView = "main" | "side" | "top";
type Rgb = [number, number, number];

const CAM_LABELS: Record<CamView, string> = { main: "主机位 · 平视", side: "侧机位 · 45°", top: "俯拍视角" };

/** 未设置 group 时按序号回退的角色表（界面显示用，不参与渲染） */
const ROLE_ZH = ["主光", "补光", "轮廓光", "背景光", "眼神光", "氛围光"];
const ROLE_EN = ["KEY", "FILL", "RIM", "BG", "EYE", "AMB"];

/** 相对照度配色（低 → 高），仅可视化 */
const LUX_STOPS: { t: number; c: Rgb }[] = [
  { t: 0, c: [6, 10, 18] },
  { t: 0.25, c: [18, 60, 130] },
  { t: 0.6, c: [70, 175, 225] },
  { t: 1, c: [238, 249, 255] },
];

/** 热力配色（低 → 高），仅可视化 */
const HEAT_STOPS: { t: number; c: Rgb }[] = [
  { t: 0, c: [8, 8, 16] },
  { t: 0.2, c: [96, 26, 110] },
  { t: 0.5, c: [214, 78, 30] },
  { t: 0.78, c: [252, 190, 60] },
  { t: 1, c: [255, 250, 226] },
];

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** 色温 → 十六进制色（同 core/render.py 的近似式，仅用于界面显示） */
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

function sampleRamp(stops: { t: number; c: Rgb }[], t: number): Rgb {
  const k = clamp(t, 0, 1);
  for (let i = 1; i < stops.length; i++) {
    const a = stops[i - 1];
    const b = stops[i];
    if (k <= b.t) {
      const f = b.t === a.t ? 0 : (k - a.t) / (b.t - a.t);
      return [
        a.c[0] + (b.c[0] - a.c[0]) * f,
        a.c[1] + (b.c[1] - a.c[1]) * f,
        a.c[2] + (b.c[2] - a.c[2]) * f,
      ];
    }
  }
  return stops[stops.length - 1].c;
}

/** 归一化平面坐标：点光用真实 px/py；方向光按 dx/dy 落在被摄体外圈环上 */
function planUv(l: Light): { u: number; v: number } {
  if (l.kind === "point") return { u: clamp(l.px, 0, 1), v: clamp(l.py, 0, 1) };
  const n = Math.hypot(l.dx, l.dy);
  const ux = n > 1e-6 ? l.dx / n : 0;
  const uy = n > 1e-6 ? l.dy / n : -1;
  return { u: clamp(0.5 + ux * 0.4, 0.02, 0.98), v: clamp(0.5 + uy * 0.4, 0.02, 0.98) };
}

/**
 * 相对强度场：每个光源在归一化平面上按点光高斯 / 方向光方向加权叠加。
 * 这是界面可视化模型（不是引擎渲染结果），只用于展示「哪里更亮」的相对关系。
 */
function computeField(list: Light[]): Float32Array {
  const data = new Float32Array(FIELD_N * FIELD_N);
  for (const l of list) {
    if (!l.visible || l.intensity <= 0) continue;
    const w = clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX;
    const { u, v } = planUv(l);
    const sigma = 0.05 + clamp(l.radius, RADIUS_MIN, RADIUS_MAX) * 0.2;
    const n = Math.hypot(l.dx, l.dy);
    const nx = n > 1e-6 ? l.dx / n : 0;
    const ny = n > 1e-6 ? l.dy / n : -1;
    const twoSigma2 = 2 * sigma * sigma;
    for (let gy = 0; gy < FIELD_N; gy++) {
      const y = (gy + 0.5) / FIELD_N;
      for (let gx = 0; gx < FIELD_N; gx++) {
        const x = (gx + 0.5) / FIELD_N;
        let k: number;
        if (l.kind === "point") {
          const dx = x - u;
          const dy = y - v;
          k = Math.exp(-(dx * dx + dy * dy) / twoSigma2);
        } else {
          const ox = x - 0.5;
          const oy = y - 0.5;
          const len = Math.hypot(ox, oy) || 1e-6;
          const t = clamp(0.5 + 0.5 * ((nx * ox + ny * oy) / len), 0, 1);
          k = 0.12 + 0.88 * t * t;
        }
        data[gy * FIELD_N + gx] += k * w;
      }
    }
  }
  let peak = 0;
  for (let i = 0; i < data.length; i++) if (data[i] > peak) peak = data[i];
  if (peak > 0) for (let i = 0; i < data.length; i++) data[i] /= peak;
  return data;
}

export function createBridgeScreen(): Screen {
  // ------------------------------------------------------------ 跨挂载保留的界面状态
  let cues: Cue[] = [];
  let selectedCueId: string | null = null;
  let selectedName: string | null = null;
  let view: ViewId = "plot";
  let camView: CamView = "main";
  let cat: Cat = "all";
  let query = "";
  let newCueFade = 1.5;
  /** 界面级锁定：锁定的通道禁止在遥测面板里改动（不影响其他屏幕） */
  const locked = new Set<string>();
  const events: { t: string; text: string }[] = [];

  // ------------------------------------------------------------ 运行时
  let selfEdit = false;
  let paramsRef: RenderParams | null = null;
  let unsub: (() => void) | null = null;
  let resizeObs: ResizeObserver | null = null;

  const offscreen = document.createElement("canvas");
  offscreen.width = FIELD_N;
  offscreen.height = FIELD_N;

  const lightsOf = (): Light[] => currentParams().lights ?? [];

  function patchLight(name: string, patch: Partial<Light>): void {
    selfEdit = true;
    setParams({ ...currentParams(), lights: lightsOf().map((l) => (l.name === name ? { ...l, ...patch } : l)) });
    selfEdit = false;
  }

  function stamp(): string {
    const d = new Date();
    const p2 = (n: number): string => String(n).padStart(2, "0");
    return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  }

  /** 事件日志只记录本屏幕真实执行过的操作（不预置示例条目） */
  function log(text: string): void {
    events.unshift({ t: stamp(), text });
    if (events.length > 60) events.length = 60;
    renderLog();
  }

  function selectedLight(): Light | null {
    const list = lightsOf();
    return list.find((l) => l.name === selectedName) ?? list[0] ?? null;
  }

  /** 分类：优先看光源自带分组，未分组时按序号回退角色表 */
  function categoryOf(l: Light, i: number): Exclude<Cat, "all"> {
    const tag = `${(l.group ?? "").trim()} ${ROLE_EN[i % ROLE_EN.length]}`.toUpperCase();
    if (/主|补|KEY|FILL/.test(tag)) return "key";
    if (/氛|背|环境|BG|AMB/.test(tag)) return "amb";
    if (/轮|眼|特效|RIM|EYE/.test(tag)) return "fx";
    return i % 3 === 0 ? "key" : i % 3 === 1 ? "fx" : "amb";
  }

  function matches(l: Light, i: number): boolean {
    if (cat !== "all" && categoryOf(l, i) !== cat) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return l.name.toLowerCase().includes(q) || (l.group ?? "").toLowerCase().includes(q);
  }

  // ------------------------------------------------------------ DOM 骨架
  const projectChip = h("div", { class: "chip" });
  const camChip = h("div", { class: "chip", style: "cursor:pointer", title: "点击切换机位（界面示意）" });
  camChip.addEventListener("click", () => {
    const order: CamView[] = ["main", "side", "top"];
    camView = order[(order.indexOf(camView) + 1) % order.length];
    log(`切换机位 → ${CAM_LABELS[camView]}`);
    renderChrome();
  });
  const stateChip = h("div", { class: "chip" });
  const ratioReadout = h("span", { class: "small sec" }, "光比（主光:补光）—");
  const rigReadout = h("span", { class: "tiny muted" }, "在线 —");
  const viewNote = h("span", { class: "tiny muted" }, "");
  const viewHonesty = h("span", { class: "tiny muted" }, "强度/半径/色温为引擎相对量（0–4 · K）；照度 lx、CRI、Δuv、频闪、DMX、灯具温度本引擎无法计算。");

  const leftCount = h("span", { class: "chip tiny" }, "0");
  const searchInput = h("input", { class: "input", placeholder: "搜索灯具名称 / 分组", spellcheck: "false" });
  const catHost = h("div");
  const arrayList = h("div", { class: "list" });
  const ratioCard = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:4px" });

  const viewCanvas = h("canvas", { style: "position:absolute;inset:0;width:100%;height:100%;display:block" });
  const barsBox = h("div", { class: "list" });
  const camCard = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:4px" });

  const telName = h("span", { class: "chip tiny" }, "未选择");
  const telBody = h("div", { style: "display:flex;flex-direction:column;gap:9px" });
  const logBox = h("div", { class: "list" });

  const timelineName = h("input", { class: "input", placeholder: "新 CUE 名称", style: "width:160px" });
  const timelineFade = h("input", {
    class: "input", type: "number", min: 0, max: 30, step: 0.1, value: newCueFade, style: "width:78px",
  });
  const timelineBlocks = h("div", {
    style: "display:flex;gap:8px;overflow-x:auto;padding:8px 12px;border-top:1px solid var(--hls-border)",
  });

  const left = h("div", { class: "panel", style: "width:300px;flex:0 0 300px" },
    h("div", { class: "panel__head" },
      icon("lightbulb", 13), h("span", {}, "灯具阵列"), h("div", { class: "spacer" }), leftCount),
    h("div", { class: "panel__body" },
      searchInput,
      catHost,
      arrayList,
      h("div", { class: "divider" }),
      ratioCard,
      h("div", { class: "tiny muted" }, "点击行选择灯具，右侧遥测同步。"),
    ),
  );

  const viewButtons: { id: ViewId; el: HTMLButtonElement }[] = [
    { id: "plot", el: button("光位", () => setView("plot"), { variant: "ghost", icon: "crosshair" }) },
    { id: "lux", el: button("照度", () => setView("lux"), { variant: "ghost", icon: "sun" }) },
    { id: "heat", el: button("热力", () => setView("heat"), { variant: "ghost", icon: "fire" }) },
  ];

  const center = h("div", { class: "main" },
    h("div", {
      class: "row",
      style: "padding:8px 12px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--hls-border)",
    },
      h("span", { class: "chip" }, icon("gauge", 12, "var(--hls-accent)"), "光影指挥屏"),
      ratioReadout,
      rigReadout,
      h("div", { class: "spacer" }),
      h("span", { class: "chip tiny" }, "机位 · 界面示意"),
      ...viewButtons.map((b) => b.el),
    ),
    h("div", { class: "row", style: "padding:6px 12px;gap:10px;border-bottom:1px solid var(--hls-border);flex-wrap:wrap" },
      icon("info", 12, "var(--hls-text-muted)"),
      viewNote,
      h("div", { class: "spacer" }),
      viewHonesty),
    h("div", { class: "stage", style: "padding:10px" },
      h("div", { style: "position:relative;flex:1;align-self:stretch;min-height:0" }, viewCanvas)),
    h("div", { style: "padding:0 12px 10px" }, barsBox),
    h("div", { style: "padding:10px 12px;border-top:1px solid var(--hls-border)" }, camCard),
  );

  const right = h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
    h("div", { class: "panel__head" },
      icon("radar", 13), h("span", {}, "实时遥测"), h("div", { class: "spacer" }), telName),
    h("div", { class: "panel__body" },
      telBody,
      h("div", { class: "divider" }),
      h("div", { class: "tiny muted" }, "本引擎只做相对 Lambert/Phong 光照：照度(lx)、CRI、Δuv、频闪、灯具温度等无法计算，因此不显示。"),
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "事件日志"),
        h("span", { class: "tiny muted" }, "本屏幕操作")),
      logBox,
    ),
  );

  const topbar = h("div", {
    class: "row",
    style: "padding:8px 12px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--hls-border)",
  },
    projectChip,
    camChip,
    stateChip,
    h("div", { class: "spacer" }),
    button("执行 CUE", () => void applySelectedCue(), { icon: "play", variant: "primary", title: "应用时间轴上选中的 CUE" }),
  );

  const timeline = h("div", { style: "display:flex;flex-direction:column" },
    h("div", {
      class: "row",
      style: "padding:8px 12px 0;gap:8px;flex-wrap:wrap",
    },
      icon("film", 13, "var(--hls-accent)"),
      h("span", { class: "small", style: "font-weight:600" }, "CUE 时间轴"),
      h("span", { class: "tiny muted" }, "点击方块应用 · 宽度为淡变时长"),
      h("div", { class: "spacer" }),
      timelineName,
      timelineFade,
      button("新建 CUE", () => void createCue(), { icon: "plus" })),
    timelineBlocks,
  );

  const root = h("div", {
    style: "display:flex;flex-direction:column;flex:1;min-width:0;min-height:0",
  },
    topbar,
    h("div", { style: "display:flex;flex:1;min-height:0" }, left, center, right),
    timeline);

  // ------------------------------------------------------------ 顶部与相机

  function renderChrome(): void {
    clear(projectChip).append(
      icon(store.ctx.mediaKind === "video" ? "film" : "folder", 12, "var(--hls-accent)"),
      h("span", {}, `项目：${store.ctx.mediaName || "未打开媒体"}`),
    );
    if (store.ctx.mediaKind) {
      projectChip.append(h("span", { class: "muted" }, store.ctx.mediaKind === "video" ? "视频" : "图片"));
    }
    clear(camChip).append(
      icon("camera", 12, "var(--hls-text-muted)"),
      h("span", {}, `机位：${CAM_LABELS[camView]}`),
      h("span", { class: "muted" }, "示意"),
    );
    const list = lightsOf();
    const lit = list.filter((l) => l.visible && l.intensity > 0.05).length;
    clear(stateChip).append(
      h("i", { class: `dot${lit > 0 ? "" : " dot--off"}` }),
      h("span", {}, lit > 0 ? "布光中" : "待机"),
      h("span", { class: "muted" }, `${lit}/${list.length} 通道出力`),
    );
    renderCamCard();
    renderRatio();
  }

  function renderCamCard(): void {
    clear(camCard).append(
      h("div", { class: "row", style: "gap:6px" },
        h("span", { class: "small", style: "font-weight:600" }, "相机 CARD"),
        h("span", { class: "chip tiny" }, "界面示意"),
        h("div", { class: "spacer" }),
        h("span", { class: "tiny muted" }, `${CAM_LABELS[camView]} · 50 mm · f/2.8（非引擎参数）`)),
      h("div", { class: "tiny muted" }, "本引擎不含取景/曝光计算，也不读取相机元数据；此处仅作界面占位。"),
    );
  }

  function renderRatio(): void {
    const list = lightsOf();
    const key = list.find((l, i) => /主|KEY/.test(`${l.group ?? ""}${ROLE_EN[i % ROLE_EN.length]}`)) ?? list[0] ?? null;
    const fill = list.filter((l) => l !== key)
      .find((l, i) => /补|FILL/.test(`${l.group ?? ""}${ROLE_EN[i % ROLE_EN.length]}`))
      ?? list.filter((l) => l !== key)[0] ?? null;
    const ratio = key && fill && fill.intensity > 1e-6 ? key.intensity / fill.intensity : null;
    const online = list.filter((l) => l.visible).length;
    rigReadout.textContent = `在线 ${online}/${list.length} · 强度合计 ${list.reduce((s, l) => s + (l.visible ? l.intensity : 0), 0).toFixed(2)}`;
    ratioReadout.textContent = ratio !== null
      ? `光比（主光:补光）${ratio.toFixed(2)} : 1`
      : "光比（主光:补光）—";
    clear(ratioCard).append(
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "光比 KEY : FILL"),
        h("b", {}, ratio !== null ? `${ratio.toFixed(2)} : 1` : "—")),
      h("div", { class: "kv" }, h("span", {}, "主光"), h("b", {}, key ? `${key.name} · ${Math.round((clamp(key.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 100)}%` : "—")),
      h("div", { class: "kv" }, h("span", {}, "补光"), h("b", {}, fill ? `${fill.name} · ${Math.round((clamp(fill.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 100)}%` : "—")),
      h("div", { class: "tiny muted" }, "由当前灯组强度直接相除得到（可见通道），非测光读数。"),
    );
  }

  // ------------------------------------------------------------ 左栏：灯具阵列

  function renderCatFilter(): void {
    clear(catHost).append(segmented<Cat>(
      [{ id: "all", label: "全部" }, { id: "key", label: "主光" }, { id: "amb", label: "氛围" }, { id: "fx", label: "特效" }],
      cat,
      (id) => { cat = id; renderCatFilter(); renderArray(); },
    ));
  }

  function renderArray(): void {
    const list = lightsOf();
    const shown = list.map((l, i) => ({ l, i })).filter(({ l, i }) => matches(l, i));
    leftCount.textContent = `${shown.length}/${list.length}`;
    clear(arrayList);
    if (!list.length) {
      arrayList.append(h("div", { class: "card small muted" }, "当前参数里没有灯具，可到「智能打光」或调光台添加。"));
      return;
    }
    if (!shown.length) {
      arrayList.append(h("div", { class: "card small muted" }, "当前筛选下没有灯具。"));
      return;
    }
    for (const { l, i } of shown) {
      const pct = Math.round((clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 100);
      const row = h("div", {
        class: `item${selectedLight()?.name === l.name ? " on" : ""}`,
        style: "display:flex;flex-direction:column;gap:5px",
      },
        h("div", { class: "row", style: "gap:6px" },
          h("i", { style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:${kelvinHex(l.kelvin)}` }),
          h("span", { class: "item__t grow ellipsis" }, l.name),
          h("span", { class: "chip tiny" }, `${(l.group ?? "").trim() || ROLE_ZH[i % ROLE_ZH.length]} · CH${String(i + 1).padStart(2, "0")}`)),
        h("div", { class: "row", style: "gap:8px" },
          h("div", { class: "bar grow" }, h("i", { style: `width:${pct}%` })),
          h("span", { class: "tiny mono muted", style: "width:34px;text-align:right" }, `${pct}%`),
          h("span", { class: "tiny muted", style: "width:56px;text-align:right" }, `${Math.round(l.kelvin)}K`),
          h("span", { class: "row tiny", style: "gap:4px;width:52px;justify-content:flex-end" },
            h("i", { class: `dot${l.visible ? "" : " dot--off"}` }),
            h("span", { class: l.visible ? "sec" : "muted" }, l.visible ? "在线" : "停用"))),
      );
      row.addEventListener("click", () => {
        selectedName = l.name;
        renderArray();
        renderTelemetry();
        drawView();
      });
      arrayList.append(row);
    }
  }

  // ------------------------------------------------------------ 中栏：视图绘制

  function setView(v: ViewId): void {
    view = v;
    renderViewChrome();
    renderRatio();
    drawView();
  }

  function renderViewChrome(): void {
    for (const b of viewButtons) b.el.classList.toggle("btn--on", b.id === view);
    viewNote.textContent = view === "plot"
      ? "光位 · 俯视归一化平面（点光用 px/py，方向光按 dx/dy 落在环上）"
      : view === "lux"
        ? "相对照度分布（相对值，非 lx）· 由光源参数推导的界面示意"
        : "相对强度热力（归一化，非 lx）· 由光源参数推导的界面示意";
    viewHonesty.textContent = view === "plot"
      ? "强度 0–4 为引擎相对标度；照度 lx / CRI / Δuv / 频闪 / DMX / 灯具温度本引擎无法计算。"
      : "非引擎渲染结果、无光度学校准；照度 lx / CRI / Δuv / 频闪 / DMX / 灯具温度本引擎无法计算。";
    renderBars();
  }

  /** 各通道相对贡献（强度 × 半径，归一化）——相对值，非照度 */
  function renderBars(): void {
    clear(barsBox);
    if (view !== "lux") return;
    const list = lightsOf().filter((l) => l.visible);
    if (!list.length) {
      barsBox.append(h("div", { class: "tiny muted" }, "没有可见通道，无法给出相对贡献。"));
      return;
    }
    const items = list.map((l) => ({
      name: l.name,
      weight: clamp(l.intensity, 0, INTENSITY_MAX) * clamp(l.radius, RADIUS_MIN, RADIUS_MAX),
    }));
    const total = items.reduce((s, it) => s + it.weight, 0);
    barsBox.append(h("div", { class: "tiny muted" }, "各通道相对贡献（强度 × 半径 归一化，相对值，非 lx）"));
    for (const it of items) {
      const share = total > 1e-6 ? it.weight / total : 0;
      barsBox.append(h("div", { class: "row", style: "gap:8px" },
        h("span", { class: "tiny muted ellipsis", style: "width:120px" }, it.name),
        h("div", { class: "bar grow" }, h("i", { style: `width:${Math.round(share * 100)}%` })),
        h("span", { class: "tiny mono muted", style: "width:44px;text-align:right" }, `${(share * 100).toFixed(1)}%`)));
    }
  }

  function drawView(): void {
    const w = viewCanvas.clientWidth || 640;
    const hgt = viewCanvas.clientHeight || 360;
    const dpr = window.devicePixelRatio || 1;
    viewCanvas.width = Math.round(w * dpr);
    viewCanvas.height = Math.round(hgt * dpr);
    const g = viewCanvas.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hgt);
    g.fillStyle = "#070910";
    g.fillRect(0, 0, w, hgt);
    if (view === "plot") drawPlot(g, w, hgt);
    else drawField(g, w, hgt, view === "lux" ? LUX_STOPS : HEAT_STOPS);
  }

  function drawPlot(g: CanvasRenderingContext2D, w: number, hgt: number): void {
    const scale = Math.min(w - 44, hgt - 44);
    const ox = (w - scale) / 2;
    const oy = (hgt - scale) / 2;
    const toPx = (u: number, v: number): { x: number; y: number } => ({ x: ox + u * scale, y: oy + v * scale });

    for (let i = 0; i <= 10; i++) {
      const u = i / 10;
      g.strokeStyle = i === 5 ? "rgba(255,255,255,0.16)" : "rgba(255,255,255,0.06)";
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(toPx(u, 0).x, toPx(u, 0).y);
      g.lineTo(toPx(u, 1).x, toPx(u, 1).y);
      g.stroke();
      g.beginPath();
      g.moveTo(toPx(0, u).x, toPx(0, u).y);
      g.lineTo(toPx(1, u).x, toPx(1, u).y);
      g.stroke();
    }
    const subject = toPx(0.5, 0.5);
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

    const sel = selectedLight()?.name ?? null;
    for (const l of lightsOf()) {
      const uv = planUv(l);
      const p = toPx(uv.u, uv.v);
      const hex = kelvinHex(l.kelvin);
      g.strokeStyle = l.visible ? hexA(hex, 0.5) : "rgba(255,255,255,0.12)";
      g.lineWidth = 1.2;
      g.setLineDash([4, 4]);
      g.beginPath();
      g.moveTo(subject.x, subject.y);
      g.lineTo(p.x, p.y);
      g.stroke();
      g.setLineDash([]);
      const r = 4 + (clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 7;
      const glow = g.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 3.2);
      glow.addColorStop(0, hexA(hex, l.visible ? 0.5 : 0.12));
      glow.addColorStop(1, hexA(hex, 0));
      g.fillStyle = glow;
      g.beginPath();
      g.arc(p.x, p.y, r * 3.2, 0, Math.PI * 2);
      g.fill();
      g.fillStyle = hexA(hex, l.visible ? 1 : 0.3);
      g.beginPath();
      g.arc(p.x, p.y, r, 0, Math.PI * 2);
      g.fill();
      g.strokeStyle = l.name === sel ? "#6fa8ff" : "rgba(0,0,0,0.35)";
      g.lineWidth = l.name === sel ? 2 : 1;
      g.beginPath();
      g.arc(p.x, p.y, r + 2, 0, Math.PI * 2);
      g.stroke();
      const label = `${l.name} ${Math.round((clamp(l.intensity, 0, INTENSITY_MAX) / INTENSITY_MAX) * 100)}%`;
      g.fillStyle = "rgba(10,13,19,0.82)";
      const tw = g.measureText(label).width + 12;
      g.fillRect(p.x - tw / 2, p.y + r + 5, tw, 15);
      g.fillStyle = l.visible ? "#e9eef8" : "#5e6a7e";
      g.fillText(label, p.x, p.y + r + 16);
    }
    if (!lightsOf().length) {
      g.fillStyle = "#5e6a7e";
      g.font = "12px Inter, system-ui, sans-serif";
      g.fillText("当前参数里没有光源", subject.x, subject.y - 34);
    }
  }

  function drawField(g: CanvasRenderingContext2D, w: number, hgt: number, stops: { t: number; c: Rgb }[]): void {
    const side = Math.min(w - 20, hgt - 20);
    const ox = (w - side) / 2;
    const oy = (hgt - side) / 2;
    const list = lightsOf().filter((l) => l.visible);
    const data = computeField(list);
    const offCtx = offscreen.getContext("2d");
    if (offCtx) {
      const img = offCtx.createImageData(FIELD_N, FIELD_N);
      for (let i = 0; i < data.length; i++) {
        const [r, gg, b] = sampleRamp(stops, Math.pow(data[i], 0.55));
        img.data[i * 4] = Math.round(r);
        img.data[i * 4 + 1] = Math.round(gg);
        img.data[i * 4 + 2] = Math.round(b);
        img.data[i * 4 + 3] = 255;
      }
      offCtx.putImageData(img, 0, 0);
      g.imageSmoothingEnabled = true;
      g.drawImage(offscreen, ox, oy, side, side);
    }
    g.strokeStyle = "rgba(255,255,255,0.14)";
    g.lineWidth = 1;
    g.strokeRect(ox, oy, side, side);

    const sel = selectedLight()?.name ?? null;
    for (const l of list) {
      const uv = planUv(l);
      const x = ox + uv.u * side;
      const y = oy + uv.v * side;
      g.strokeStyle = l.name === sel ? "#6fa8ff" : hexA(kelvinHex(l.kelvin), 0.9);
      g.lineWidth = l.name === sel ? 2 : 1.2;
      g.beginPath();
      g.arc(x, y, l.name === sel ? 7 : 4, 0, Math.PI * 2);
      g.stroke();
      if (l.name === sel) {
        g.fillStyle = "#6fa8ff";
        g.font = "10px Inter, system-ui, sans-serif";
        g.textAlign = "left";
        g.fillText(l.name, x + 10, y + 3);
      }
    }
    if (!list.length) {
      g.fillStyle = "#5e6a7e";
      g.font = "12px Inter, system-ui, sans-serif";
      g.textAlign = "center";
      g.fillText("没有可见通道", ox + side / 2, oy + side / 2);
    }
  }

  // ------------------------------------------------------------ 右栏：遥测

  /** 带提交回调的遥测推子（input 写参数，change 才写日志，避免每一帧刷屏） */
  function telSlider(opts: {
    label: string; min: number; max: number; step: number; value: number;
    format: (v: number) => string; disabled: boolean;
    onInput: (v: number) => void; onCommit: (v: number) => void;
  }): HTMLElement {
    const val = h("b", {}, opts.format(opts.value));
    const input = h("input", {
      type: "range", min: opts.min, max: opts.max, step: opts.step, value: opts.value,
    });
    input.disabled = opts.disabled;
    input.addEventListener("input", () => {
      const v = Number(input.value);
      val.textContent = opts.format(v);
      opts.onInput(v);
    });
    input.addEventListener("change", () => opts.onCommit(Number(input.value)));
    return h("div", { class: "slider-row" },
      h("div", { class: "slider-row__head" }, h("span", {}, opts.label), val),
      input,
    );
  }

  function renderTelemetry(): void {
    const list = lightsOf();
    const l = selectedLight();
    clear(telBody);
    if (!l) {
      telName.textContent = "无灯具";
      telBody.append(h("div", { class: "small muted" }, "当前参数里没有灯具。"));
      return;
    }
    selectedName = l.name;
    const i = list.indexOf(l);
    const isLocked = locked.has(l.name);
    telName.textContent = `CH${String(i + 1).padStart(2, "0")} · ${l.name}`;

    const total = list.filter((x) => x.visible).reduce((s, x) => s + clamp(x.intensity, 0, INTENSITY_MAX), 0);
    const energy = l.visible && total > 1e-6 ? clamp(l.intensity, 0, INTENSITY_MAX) / total : 0;
    const n = Math.hypot(l.dx, l.dy, l.dz) || 1;
    const azimuth = (Math.atan2(l.dx / n, l.dz / n) * 180) / Math.PI;
    const elevation = (Math.asin(clamp(-l.dy / n, -1, 1)) * 180) / Math.PI;

    telBody.append(
      h("div", { class: "kv" }, h("span", {}, "类型"),
        h("b", {}, `${(l.group ?? "").trim() || ROLE_ZH[i % ROLE_ZH.length]} · ${l.kind === "point" ? "点光" : "方向光"}`)),
      h("div", { class: "kv" }, h("span", {}, "状态"),
        h("b", {}, l.visible ? "在线" : "停用（visible=false）")),
      telSlider({
        label: "色温 (K)", min: KELVIN_MIN, max: KELVIN_MAX, step: 50, value: l.kelvin,
        format: (v) => `${Math.round(v)} K`, disabled: isLocked,
        onInput: (v) => { patchLight(l.name, { kelvin: v }); renderRatio(); drawView(); },
        onCommit: (v) => log(`修改色温 ${l.name} → ${Math.round(v)} K`),
      }),
      telSlider({
        label: "相对强度（0–4 标度）", min: 0, max: INTENSITY_MAX, step: 0.05, value: l.intensity,
        format: (v) => `${Math.round((v / INTENSITY_MAX) * 100)}%`, disabled: isLocked,
        onInput: (v) => { patchLight(l.name, { intensity: v }); renderRatio(); drawView(); },
        onCommit: (v) => log(`修改强度 ${l.name} → ${Math.round((v / INTENSITY_MAX) * 100)}%`),
      }),
      telSlider({
        label: "半径 / 半影", min: RADIUS_MIN, max: RADIUS_MAX, step: 0.01, value: l.radius,
        format: (v) => v.toFixed(2), disabled: isLocked,
        onInput: (v) => { patchLight(l.name, { radius: v }); drawView(); },
        onCommit: (v) => log(`修改半径 ${l.name} → ${v.toFixed(2)}`),
      }),
      h("div", { class: "kv" }, h("span", {}, "相对占比（可见通道）"),
        h("b", {}, `${(energy * 100).toFixed(1)}%`)));
    if (l.kind === "point") {
      telBody.append(
        h("div", { class: "kv" }, h("span", {}, "位置 X / Y / 高度"),
          h("b", {}, `${l.px.toFixed(2)} / ${l.py.toFixed(2)} / ${l.pz.toFixed(2)}`)),
        h("div", { class: "tiny muted" }, "点光源以归一化画面坐标 (px, py) 与高度 pz 描述。"));
    } else {
      telBody.append(
        h("div", { class: "kv" }, h("span", {}, "方位角 / 仰角"),
          h("b", {}, `${azimuth.toFixed(0)}° / ${elevation.toFixed(0)}°`)),
        h("div", { class: "tiny muted" }, "由方向向量计算：方位角 atan2(dx, dz)，仰角 asin(-dy)（归一化后，图像坐标 x 向右 / y 向下）。"));
    }
    telBody.append(
      h("div", { class: "row", style: "gap:8px" },
        button(isLocked ? "解锁" : "锁定", () => {
          if (isLocked) { locked.delete(l.name); log(`解锁 ${l.name}`); }
          else { locked.add(l.name); log(`锁定 ${l.name}（界面级，禁止在遥测面板修改）`); }
          renderTelemetry();
        }, { icon: isLocked ? "check" : "crosshair", variant: isLocked ? "primary" : "ghost", on: isLocked }),
        button(l.visible ? "停用" : "启用", () => {
          patchLight(l.name, { visible: !l.visible });
          log(`${l.visible ? "停用" : "启用"} ${l.name}（visible=${!l.visible}）`);
          renderArray();
          renderRatio();
          renderTelemetry();
          drawView();
          renderChrome();
        }, { icon: "power", variant: "ghost" })),
    );
  }

  function renderLog(): void {
    clear(logBox);
    if (!events.length) {
      logBox.append(h("div", { class: "card small muted" }, "本会话还没有操作记录。"));
      return;
    }
    for (const e of events) {
      logBox.append(h("div", { class: "row", style: "gap:8px;align-items:flex-start" },
        h("span", { class: "tiny mono muted" }, e.t),
        h("span", { class: "small sec grow" }, e.text)));
    }
  }

  // ------------------------------------------------------------ 底栏：CUE 时间轴

  async function reloadCues(): Promise<void> {
    try {
      const r = await api.cues();
      cues = r.cues;
    } catch (e) {
      cues = [];
      reportError(e);
    }
    if (selectedCueId && !cues.some((c) => c.id === selectedCueId)) selectedCueId = null;
    renderTimeline();
  }

  function renderTimeline(): void {
    clear(timelineBlocks);
    if (!cues.length) {
      timelineBlocks.append(h("div", { class: "card small muted" }, "还没有 CUE。调整光照后可用右侧「新建 CUE」保存当前参数。"));
      return;
    }
    cues.forEach((c, i) => {
      const width = 96 + (clamp(c.fade, 0, 30) / 30) * 150;
      const block = h("button", {
        type: "button",
        class: `item${c.id === selectedCueId ? " on" : ""}`,
        style: `flex:0 0 auto;width:${Math.round(width)}px;display:flex;flex-direction:column;gap:3px;text-align:left`,
        title: `应用「${c.name}」· 淡变 ${c.fade.toFixed(1)}s`,
      },
        h("div", { class: "row", style: "gap:6px" },
          h("span", { class: "mono tiny muted" }, String(i + 1).padStart(2, "0")),
          h("span", { class: "item__t grow ellipsis" }, c.name)),
        h("div", { class: "row" },
          h("span", { class: "tiny muted" }, `淡变 ${c.fade.toFixed(1)}s`),
          h("div", { class: "spacer" }),
          h("span", { class: "tiny muted" }, `${c.params.lights.length} 通道`)),
        h("div", { class: "bar" }, h("i", { style: `width:${Math.round((clamp(c.fade, 0, 30) / 30) * 100)}%` })),
      );
      block.addEventListener("click", () => { void applyCue(c); });
      timelineBlocks.append(block);
    });
  }

  async function applySelectedCue(): Promise<void> {
    const c = cues.find((x) => x.id === selectedCueId) ?? cues[0] ?? null;
    if (!c) { toast("还没有 CUE 可执行", "error"); return; }
    await applyCue(c);
  }

  async function applyCue(c: Cue): Promise<void> {
    const idx = cues.indexOf(c) + 1;
    try {
      const target = (await api.cueInterpolate(c.id, currentParams(), 1)).params;
      selfEdit = true;
      setParams(target);
      selfEdit = false;
      selectedCueId = c.id;
      selectedName = null;
      renderAll();
      log(`已应用 CUE ${String(idx).padStart(2, "0")}「${c.name}」（淡变 ${c.fade.toFixed(1)}s · ${target.lights.length} 通道）`);
      toast(`已应用 CUE「${c.name}」`, "ok");
      store.set({ status: `指挥屏已应用 CUE「${c.name}」· ${target.lights.length} 通道` });
    } catch (e) {
      reportError(e);
    }
  }

  async function createCue(): Promise<void> {
    const fade = clamp(Number(timelineFade.value) || 0, 0, 30);
    newCueFade = fade;
    try {
      const r = await api.cueSave(timelineName.value.trim(), currentParams(), fade);
      timelineName.value = "";
      selectedCueId = r.cue.id;
      await reloadCues();
      log(`新建 CUE「${r.cue.name}」（淡变 ${r.cue.fade.toFixed(1)}s · ${r.cue.params.lights.length} 通道）`);
      toast(`已新建 CUE「${r.cue.name}」`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  // ------------------------------------------------------------ 装配

  function renderAll(): void {
    renderChrome();
    renderCatFilter();
    renderArray();
    renderTelemetry();
    renderViewChrome();
    drawView();
    renderTimeline();
  }

  function syncFromStore(): void {
    const p = currentParams();
    if (p === paramsRef) return;
    paramsRef = p;
    if (selfEdit) {
      // 本屏幕写入参数时只刷新派生读数，避免重建正在操作的推子
      renderArray();
      renderChrome();
      drawView();
      return;
    }
    renderChrome();
    renderArray();
    renderRatio();
    renderTelemetry();
    drawView();
  }

  searchInput.addEventListener("input", () => { query = searchInput.value; renderArray(); });
  timelineFade.addEventListener("input", () => { newCueFade = Number(timelineFade.value) || 0; });

  return {
    id: "bridge",
    name: "光影指挥屏",
    icon: "gauge",
    mount(rootEl: HTMLElement): void {
      clear(rootEl).append(root);
      if (!store.ctx.params) {
        clear(rootEl).append(h("div", { style: "padding:24px" },
          h("div", { class: "banner" }, "等待后端状态…请确认本地服务已启动。")));
        return;
      }
      timelineFade.value = String(newCueFade);
      paramsRef = currentParams();
      renderAll();
      renderLog();
      void reloadCues();
      unsub = store.subscribe(() => syncFromStore());
      resizeObs = new ResizeObserver(() => drawView());
      resizeObs.observe(viewCanvas);
    },
    unmount(): void {
      unsub?.();
      unsub = null;
      resizeObs?.disconnect();
      resizeObs = null;
    },
  };
}
