/**
 * 屏幕 A2 · 智能打光
 *
 * 左栏：AI 预处理后端 / 自然语言光照描述 / 预设场景 / 参考图迁移。
 * 中栏：画布（CanvasView）+ 渲染模式与导出。
 * 右栏：生成出来的光照节点卡片（只读展示）+ 解析说明与来源。
 *
 * 仅展示后端真实返回的量：描述解析来源（离线/云端）、节点参数、渲染耗时与深度来源；
 * 不杜撰照度、CRI、GPU 温度等本引擎无法推导的读数。
 */
import { api, downloadB64, pngUrl, type AutoLightResult, type Light, type Preset, type RenderParams } from "../api";
import {
  CanvasView, button, clear, h, icon, reportError, segmented, store, toast,
} from "../ui";
import { currentParams, openSettings, setParams, type Screen } from "../shell";

/** 后端 id → 中文名（与外壳顶栏的用词保持一致） */
const BACKEND_LABELS: Record<string, string> = {
  builtin: "内置本地引擎",
  local: "本地自建服务",
  cloud: "云端 API",
  simulate: "仅预览（灰度模拟）",
};

const backendLabel = (id: string): string => BACKEND_LABELS[id] ?? (id || "未知");

/** 参考图迁移返回的偏移键名 → 展示用中文标签 */
const OFFSET_LABELS: Record<string, string> = {
  angle: "角度", angle_deg: "角度", direction: "角度", dir: "角度",
  kelvin: "色温", kelvin_shift: "色温", temp: "色温", temperature: "色温",
  intensity: "强度", intensity_scale: "强度", strength: "强度", scale: "强度",
};

/** 节点配色：仅用于区分卡片，不代表任何物理量 */
const NODE_COLORS = ["var(--hls-warm)", "var(--hls-accent-bright)", "var(--hls-violet)", "var(--hls-success)"];

const PROMPT_PLACEHOLDER = "例如：黄昏时分的舞台逆光，暖橘色主光从左后方打来，冷蓝补光勾勒人物轮廓，空气中有薄雾感。";

/** 深拷贝参数：避免就地修改预设或全局基准对象 */
function cloneParams(p: RenderParams): RenderParams {
  return {
    ...p,
    lights: p.lights.map((l) => ({ ...l })),
    reference_offset: p.reference_offset ? { ...p.reference_offset } : null,
  };
}

/** 读取本地文件为裸 base64（接口只接受裸 base64，不接受 data URL） */
async function fileToB64(file: File): Promise<string> {
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
  reader.onerror = () => reject(new Error("读取文件失败"));
  reader.readAsDataURL(file);
  return promise;
}

function formatOffsetValue(key: string, value: number): string {
  const sign = value > 0 ? "+" : "";
  if (key.includes("angle") || key === "dir" || key === "direction") return `${sign}${value.toFixed(1)}°`;
  if (key.includes("kelvin") || key === "temp" || key === "temperature") return `${sign}${Math.round(value)} K`;
  return `${sign}${Math.round(value * 100) / 100}`;
}

export function createAutoLightScreen(): Screen {
  let timer: number | null = null;
  let renderBusy = false;
  let dirty = false;
  let format: "png" | "jpeg" = "png";
  let lastResult: AutoLightResult | null = null;
  /** 生成前的参数快照，供「重置」回到生成前 */
  let snapshot: RenderParams | null = null;
  let undoParams: RenderParams | null = null;
  let refOffset: Record<string, number> | null = null;
  let generating = false;

  const canvas = new CanvasView();

  // ------------------------------------------------------------ 参数与渲染调度

  function scheduleRender(): void {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => { timer = null; void doRender(); }, 220);
  }

  async function doRender(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) {
      store.set({ status: "已更新光照参数 · 请先导入图片以查看效果" });
      return;
    }
    if (renderBusy) { dirty = true; return; }
    renderBusy = true;
    store.set({ status: "渲染中…" });
    try {
      const res = await api.render(imageId, currentParams());
      store.set({
        lastRender: pngUrl(res.png_b64),
        backend: res.backend,
        fromCache: res.from_cache,
        status: `渲染完成 · ${res.elapsed.toFixed(2)} s · 深度来源 ${backendLabel(res.backend)}${res.from_cache ? " · 缓存命中" : ""}`,
      });
      canvas.setImage(pngUrl(res.png_b64));
      renderStats();
    } catch (e) {
      reportError(e);
      store.set({ status: "渲染失败" });
    } finally {
      renderBusy = false;
      if (dirty) { dirty = false; scheduleRender(); }
    }
  }

  // ------------------------------------------------------------ 左栏

  const backendList = h("div", { class: "list" });
  const promptArea = h("textarea", { class: "textarea", maxlength: 200, placeholder: PROMPT_PLACEHOLDER });
  const counter = h("span", { class: "small muted" }, `0 / 200`);
  promptArea.addEventListener("input", () => {
    counter.textContent = `${promptArea.value.length} / 200`;
  });

  const genBtn = button("生成光照节点", () => void generate(), { icon: "sparkles", variant: "primary" });
  genBtn.style.flex = "1";
  const presetList = h("div", { class: "list" });

  const refInput = h("input", { type: "file", accept: "image/*", class: "hidden" });
  refInput.addEventListener("change", () => {
    const f = refInput.files?.[0];
    refInput.value = "";
    if (f) void transferReference(f);
  });

  const refSlot = h("div");

  const left = h("div", { class: "panel", style: "width:300px;flex:0 0 300px" },
    h("div", { class: "panel__head" }, icon("sparkles", 13), h("span", {}, "智能打光"),
      h("div", { class: "spacer" }), h("span", { class: "chip tiny" }, "描述 → 光照节点")),
    h("div", { class: "panel__body" },
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "AI 预处理后端"),
        h("span", { class: "tiny muted" }, "设置中可切换")),
      backendList,
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "光照描述（自然语言）"),
        counter),
      promptArea,
      h("div", { class: "row", style: "gap:8px" },
        genBtn,
        button("重置", () => doReset(), { icon: "rotate", title: "清空描述并恢复生成前的参数" })),
      h("div", { class: "tiny muted" }, "生成后自动应用参数并重新渲染；失败时保留原参数。"),
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "预设场景"),
        h("span", { class: "tiny muted" }, "5 种")),
      presetList,
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "参考图光照迁移"),
        h("span", { class: "chip tiny" }, "保守微调")),
      refSlot,
      refInput,
    ),
  );

  function renderBackends(): void {
    const s = store.ctx.state?.settings;
    const rows = [
      { id: "builtin", name: "内置本地引擎", desc: "离线深度推理 · 权重需自行准备", on: !!s?.builtin_enabled },
      { id: "local", name: "本地自建服务", desc: s?.local_url || "http://127.0.0.1:8765", on: !!s?.local_enabled },
      {
        id: "cloud", name: "云端 API",
        desc: [s?.cloud_provider, s?.cloud_model].filter(Boolean).join(" · ") || "需联网",
        on: !!s?.cloud_enabled,
      },
      { id: "preview", name: "仅预览模式", desc: "跳过全部 AI 后端 · 完全离线", on: !!s?.preview_only },
    ];
    clear(backendList);
    for (const r of rows) {
      const current = store.ctx.backend === r.id;
      backendList.append(h("div", {
        class: "card",
        style: `display:flex;gap:9px;align-items:flex-start${current ? ";border-color:var(--hls-accent-line);background:var(--hls-accent-tint)" : ""}`,
      },
        h("i", { class: `dot${r.on ? "" : " dot--off"}`, style: "margin-top:5px" }),
        h("div", { class: "grow", style: "min-width:0" },
          h("div", { class: "row", style: "gap:6px" },
            h("span", { style: "font-size:12px;font-weight:600" }, r.name),
            current ? h("span", { class: "chip tiny" }, "当前") : null),
          h("div", { class: "tiny muted ellipsis" }, r.desc)),
        h("span", { class: `tiny ${r.on ? "sec" : "muted"}` }, r.on ? "已启用" : "未启用"),
      ));
    }
  }

  function renderPresets(): void {
    clear(presetList);
    for (const p of store.ctx.state?.presets ?? []) {
      const row = h("button", { class: "item", type: "button" },
        h("div", { class: "grow", style: "min-width:0" },
          h("div", { class: "item__t" }, p.name),
          h("div", { class: "item__d" }, `${p.params.lights.length} 光源 · 环境 ${Math.round(p.params.ambient_intensity * 100)}%`)));
      row.addEventListener("click", () => applyPreset(p));
      presetList.append(row);
    }
  }

  function applyPreset(p: Preset): void {
    setParams(cloneParams(p.params));
    scheduleRender();
    toast(`已套用预设：${p.name}`, "ok");
  }

  function renderRefSlot(): void {
    clear(refSlot);
    const card = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
      button("导入参考图提取主光方向", () => refInput.click(), { icon: "image" }),
      h("div", { class: "tiny muted" }, "仅做方向与色温迁移，保留原图明暗结构。"),
    );
    if (refOffset) {
      const rows = Object.entries(refOffset);
      const box = h("div", { class: "card", style: "background:var(--hls-bg-canvas);display:flex;flex-direction:column;gap:3px" });
      if (!rows.length) box.append(h("div", { class: "small muted" }, "本次迁移未产生可量化的偏移"));
      for (const [k, v] of rows) {
        box.append(h("div", { class: "kv" },
          h("span", {}, OFFSET_LABELS[k] ?? k),
          h("b", {}, formatOffsetValue(k, v))));
      }
      card.append(box, button("撤销迁移", () => {
        if (!undoParams) return;
        setParams(undoParams);
        undoParams = null;
        refOffset = null;
        renderRefSlot();
        scheduleRender();
        toast("已撤销参考图迁移", "ok");
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
      undoParams = cloneParams(currentParams());
      refOffset = res.offset;
      setParams(cloneParams(res.params));
      renderRefSlot();
      scheduleRender();
      store.set({ status: "已应用参考图光照" });
    } catch (e) {
      reportError(e);
      store.set({ status: "参考图迁移失败" });
    }
  }

  // ------------------------------------------------------------ 中栏

  const modeChip = h("div", { class: "chip" });
  const analyzeLine = h("div", { class: "small muted" }, "深度图 + 法线图：等待分析");
  const statsLine = h("div", { class: "small muted" });

  const formatSelect = h("select", { class: "select", style: "width:auto" });
  for (const f of [{ id: "png", label: "PNG" }, { id: "jpeg", label: "JPEG" }] as const) {
    formatSelect.append(h("option", { value: f.id }, f.label));
  }
  formatSelect.addEventListener("change", () => { format = formatSelect.value === "jpeg" ? "jpeg" : "png"; });

  function renderToolbar(): void {
    const p = currentParams();
    formatSelect.value = format;
    clear(modeChip);
    const kind = store.ctx.mediaKind;
    modeChip.append(
      icon(kind === "video" ? "film" : "image", 12, "var(--hls-accent)"),
      h("span", {}, `智能识别：${kind === "video" ? "视频模式" : kind === "image" ? "图片模式" : "未载入媒体"}`),
    );
    if (store.ctx.mediaName) modeChip.append(h("span", { class: "muted ellipsis" }, store.ctx.mediaName));
    clear(toolbar);
    const exportBtn = button("导出", () => void doExport(), { icon: "download", variant: "primary" });
    exportBtn.disabled = !store.ctx.imageId;
    toolbar.append(
      modeChip,
      h("div", { class: "spacer" }),
      segmented<"linear" | "hq">(
        [{ id: "linear", label: "快速预览" }, { id: "hq", label: "高质量物理阴影" }],
        p.lighting_mode,
        (id) => { const next = cloneParams(currentParams()); next.lighting_mode = id; setParams(next); renderToolbar(); scheduleRender(); }),
      formatSelect,
      exportBtn,
    );
  }

  const toolbar = h("div", {
    class: "row",
    style: "padding:8px 12px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--hls-border)",
  });

  const stage = h("div", { class: "stage" },
    canvas.el,
    h("div", { class: "overlay" }, icon("sparkles", 12), h("span", {}, "光照节点已叠加到预览")),
  );

  const center = h("div", { class: "main" },
    toolbar,
    h("div", {
      class: "row",
      style: "padding:6px 12px;gap:10px;border-bottom:1px solid var(--hls-border);flex-wrap:wrap",
    }, analyzeLine, h("div", { class: "spacer" }), statsLine),
    stage,
  );

  async function runAnalyze(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) {
      analyzeLine.textContent = "深度图 + 法线图：请先导入图片";
      return;
    }
    analyzeLine.textContent = "深度图 + 法线图：分析中…";
    try {
      const res = await api.analyze(imageId);
      const hasNormal = !!res.normal_png_b64;
      store.set({ depthPng: pngUrl(res.depth_png_b64), backend: res.backend, fromCache: res.from_cache });
      analyzeLine.textContent =
        `深度图 + 法线图${hasNormal ? "已就绪" : "已就绪（未返回法线，由深度近似）"} · ${backendLabel(res.backend)}${res.from_cache ? " · 缓存命中" : ""}`;
    } catch (e) {
      reportError(e);
      analyzeLine.textContent = "深度图 + 法线图：分析失败，已降级为预览模式";
    }
  }

  function renderStats(): void {
    const res = lastResult;
    clear(statsLine).append(
      h("span", {}, h("span", { class: "muted" }, "光照节点 "), h("b", {}, String(res ? res.rig.lights.length : currentParams().lights.length))),
      h("span", {}, h("span", { class: "muted" }, " 深度来源 "), h("b", {}, backendLabel(store.ctx.backend))),
    );
  }

  async function doExport(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) { toast("请先导入图片", "error"); return; }
    store.set({ status: "正在导出…" });
    try {
      const res = await api.exportImage(imageId, currentParams(), format);
      downloadB64(res.png_b64, res.filename, format === "jpeg" ? "image/jpeg" : "image/png");
      store.set({ status: `已导出 ${res.filename}` });
    } catch (e) {
      reportError(e);
      store.set({ status: "导出失败" });
    }
  }

  // ------------------------------------------------------------ 右栏：生成的节点

  const nodeCount = h("span", { class: "chip tiny" }, "0");
  const rightBody = h("div", { class: "panel__body" });

  const right = h("div", { class: "panel panel--right", style: "width:340px;flex:0 0 340px" },
    h("div", { class: "panel__head" },
      icon("lightbulb", 13), h("span", {}, "光照节点"), h("div", { class: "spacer" }), nodeCount),
    rightBody,
  );

  function nodeCard(l: Light, i: number): HTMLElement {
    return h("div", { class: "card", style: "display:flex;flex-direction:column;gap:7px" },
      h("div", { class: "row" },
        h("i", {
          style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:${NODE_COLORS[i % NODE_COLORS.length]}`,
        }),
        h("span", { style: "font-size:12px;font-weight:600" }, l.name || `光源 ${i + 1}`),
        h("span", { class: "chip tiny" }, l.kind === "point" ? "点光" : "平行光"),
        h("div", { class: "spacer" }),
        h("span", { class: "tiny muted" }, l.visible ? "启用" : "停用")),
      h("div", { class: "kv" }, h("span", {}, "色温"), h("b", {}, `${Math.round(l.kelvin)} K`)),
      h("div", { class: "kv" }, h("span", {}, "强度"), h("b", {}, l.intensity.toFixed(2))),
      h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, Math.round((l.intensity / 4) * 100))}%` })),
      h("div", { class: "tiny muted" }, "强度条为 0–4 相对标度"),
    );
  }

  function renderRight(): void {
    const p = currentParams();
    const lights = lastResult ? lastResult.rig.lights : p.lights;
    nodeCount.textContent = String(lights.length);
    clear(rightBody);

    rightBody.append(
      h("div", { class: "panel__head", style: "padding:0" },
        h("span", { class: "small", style: "font-weight:600" }, lastResult ? "AI 生成的光照节点" : "当前参数中的光源"),
        h("div", { class: "spacer" }),
        lastResult
          ? h("span", { class: "chip" }, h("i", { class: "dot" }), lastResult.source === "cloud" ? "云端解析" : "离线解析")
          : h("span", { class: "chip tiny" }, "未生成")),
      h("div", { class: "tiny muted" }, "AI 根据文本描述生成 · 参数可无限叠加"),
      lastResult
        ? h("div", { class: "small sec", style: "line-height:1.6" }, `解析说明：${lastResult.note || lastResult.rig.note || "（后端未返回说明）"}`)
        : h("div", { class: "small muted" }, "尚未生成：以下为当前参数里的光源，可先用左侧描述生成新的光照节点。"),
    );

    if (lastResult) {
      rightBody.append(h("div", { class: "kv" },
        h("span", {}, "环境光"),
        h("b", {}, `${lastResult.rig.ambient_intensity.toFixed(2)} · ${Math.round(lastResult.rig.ambient_kelvin)} K`)));
    }

    lights.forEach((l, i) => rightBody.append(nodeCard(l, i)));

    if (lastResult) {
      rightBody.append(button("应用此光照参数", () => {
        if (!lastResult) return;
        setParams(cloneParams(lastResult.params));
        scheduleRender();
        renderStats();
        toast("已应用生成的光照参数", "ok");
      }, { icon: "check", variant: "primary", title: "把生成的参数写入当前渲染参数" }),
        button("打开设置调整后端", () => openSettings(), { icon: "settings", variant: "ghost" }));
    }
  }

  // ------------------------------------------------------------ 生成与重置

  async function generate(): Promise<void> {
    const text = promptArea.value.trim();
    if (!text) { toast("请先输入光照描述", "error"); return; }
    if (generating) return;
    generating = true;
    genBtn.disabled = true;
    store.set({ status: "正在解析光照描述…" });
    try {
      const res = await api.autolight(text, { base_params: currentParams() });
      if (!snapshot) snapshot = cloneParams(currentParams());
      lastResult = res;
      setParams(cloneParams(res.params));
      renderRight();
      renderToolbar();
      renderStats();
      scheduleRender();
      store.set({
        status: `已生成 ${res.rig.lights.length} 个光照节点 · ${res.source === "cloud" ? "云端解析" : "离线解析"}`,
      });
    } catch (e) {
      reportError(e);
      store.set({ status: "光照描述解析失败，已保留原有参数" });
    } finally {
      generating = false;
      genBtn.disabled = false;
    }
  }

  function doReset(): void {
    promptArea.value = "";
    counter.textContent = "0 / 200";
    lastResult = null;
    refOffset = null;
    undoParams = null;
    if (snapshot) {
      setParams(cloneParams(snapshot));
      snapshot = null;
    }
    renderRight();
    renderToolbar();
    renderStats();
    renderRefSlot();
    scheduleRender();
    store.set({ status: "已重置光照描述与节点" });
  }

  // ------------------------------------------------------------ 装配

  return {
    id: "autolight",
    name: "智能打光",
    subtitle: "描述 → 光照节点",
    icon: "sparkles",
    mount(root: HTMLElement): void {
      clear(root).append(left, center, right);
      if (!store.ctx.params) {
        clear(root).append(h("div", { style: "padding:24px" },
          h("div", { class: "banner" }, "等待后端状态…请确认本地服务已启动。")));
        return;
      }
      renderBackends();
      renderPresets();
      renderRefSlot();
      renderToolbar();
      renderRight();
      renderStats();
      canvas.setImage(store.ctx.lastRender ?? store.ctx.originalPng);
      void runAnalyze();
    },
    unmount(): void {
      if (timer !== null) { window.clearTimeout(timer); timer = null; }
      renderBusy = false;
      dirty = false;
    },
  };
}
