/**
 * 屏幕 A1 · 光影工作台
 *
 * 三栏布局：素材库（264px） / 画布（自适应） / 光照控制（320px）。
 * 所有参数改动都先深拷贝再写入 store，随后 220ms 防抖触发一次 api.render：
 * 拖动滑杆时不会堆积请求，也不会因为就地修改而污染预设对象。
 */
import { api, downloadB64, pngUrl, type MediaResult, type Preset, type RenderParams } from "../api";
import {
  CanvasView, Trackball, button, clear, h, icon, numberField, reportError,
  segmented, slider, store, toast,
} from "../ui";
import { currentParams, openSettings, setParams, showScreen, type Screen } from "../shell";

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

/** 会话内素材条目：后端没有素材列表接口，因此只在本次运行期间保留 */
interface Asset {
  id: string;
  kind: "image" | "video";
  name: string;
  meta: string;
  /** 原图 / 视频首帧（data URL），重新选中时恢复画布 */
  png: string | null;
  videoInfo: { width: number; height: number; fps: number; duration: number; frame_count: number; has_audio: boolean } | null;
}

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

/** 把后端返回的偏移量按语义格式化（角度 / 色温 / 其余用普通数值） */
function formatOffsetValue(key: string, value: number): string {
  const sign = value > 0 ? "+" : "";
  if (key.includes("angle") || key === "dir" || key === "direction") return `${sign}${value.toFixed(1)}°`;
  if (key.includes("kelvin") || key === "temp" || key === "temperature") return `${sign}${Math.round(value)} K`;
  return `${sign}${Math.round(value * 100) / 100}`;
}

export function createWorkbenchScreen(): Screen {
  const assets: Asset[] = [];
  let filter: "all" | "image" | "video" = "all";
  let selectedLight = 0;
  let pickOn = false;
  let depthOn = false;
  let compare = false;
  let hintDismissed = false;
  let activePreset = "";
  let highResHint = false;
  let undoParams: RenderParams | null = null;
  let refOffset: Record<string, number> | null = null;
  let timer: number | null = null;
  let busy = false;
  let dirty = false;
  let format: "png" | "jpeg" = "png";
  let trackball: Trackball | null = null;
  let mounted = false;

  const canvas = new CanvasView();

  // ------------------------------------------------------------ 参数与渲染调度

  function apply(mut: (p: RenderParams) => void, rebuild = false): void {
    const next = cloneParams(currentParams());
    mut(next);
    setParams(next);
    if (rebuild) { renderToolbar(); renderRight(); }
    scheduleRender();
  }

  function scheduleRender(): void {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => { timer = null; void doRender(); }, 220);
  }

  async function doRender(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) {
      store.set({ status: "请先导入图片后再渲染" });
      return;
    }
    if (busy) { dirty = true; return; }
    busy = true;
    renderStateEl.textContent = "渲染中…";
    store.set({ status: "渲染中…" });
    try {
      const res = await api.render(imageId, currentParams());
      store.set({
        lastRender: pngUrl(res.png_b64),
        backend: res.backend,
        fromCache: res.from_cache,
        status: `渲染完成 · ${res.elapsed.toFixed(2)} s · 深度来源 ${backendLabel(res.backend)}${res.from_cache ? " · 缓存命中" : ""}`,
      });
      if (res.high_res_hint && !highResHint) { highResHint = true; refreshBanner(); }
      syncCanvasImage();
      updateStats({ elapsed: res.elapsed, backend: res.backend, from_cache: res.from_cache });
    } catch (e) {
      reportError(e);
      store.set({ status: "渲染失败" });
    } finally {
      busy = false;
      renderStateEl.textContent = "";
      if (dirty) { dirty = false; scheduleRender(); }
    }
  }

  function syncCanvasImage(): void {
    if (depthOn) canvas.setImage(store.ctx.depthPng);
    else if (compare) canvas.setImage(store.ctx.originalPng);
    else canvas.setImage(store.ctx.lastRender ?? store.ctx.originalPng);
  }

  // ------------------------------------------------------------ 左侧：素材库

  const countEl = h("span", { class: "chip tiny" }, "0");
  const filterSlot = h("div");
  const listEl = h("div", { class: "list" });
  const presetEl = h("div", { class: "list" });

  const fileInput = h("input", { type: "file", accept: "image/*,video/*", class: "hidden" });
  fileInput.addEventListener("change", () => {
    const f = fileInput.files?.[0];
    fileInput.value = "";
    if (f) void importFile(f);
  });

  const importBtn = button("导入图片 / 视频", () => fileInput.click(), { icon: "upload", variant: "primary" });
  importBtn.style.width = "100%";
  importBtn.style.justifyContent = "center";

  const left = h("div", { class: "panel", style: "width:264px;flex:0 0 264px" },
    h("div", { class: "panel__head" }, icon("layers", 13), h("span", {}, "素材库"), h("div", { class: "spacer" }), countEl),
    h("div", { class: "panel__body" },
      importBtn,
      fileInput,
      filterSlot,
      listEl,
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "预设场景"),
        h("span", { class: "tiny muted" }, "一键套用")),
      presetEl,
    ),
  );

  function presetSummary(p: Preset): string {
    return `${p.params.lights.length} 光源 · 环境 ${Math.round(p.params.ambient_intensity * 100)}%`;
  }

  function renderLeft(): void {
    clear(filterSlot).append(segmented<"all" | "image" | "video">(
      [{ id: "all", label: "全部" }, { id: "image", label: "图片" }, { id: "video", label: "视频" }],
      filter,
      (id) => { filter = id; renderLeft(); },
    ));
    countEl.textContent = String(assets.length);

    clear(listEl);
    const shown = assets.filter((a) => filter === "all" || a.kind === filter);
    if (!shown.length) {
      listEl.append(h("div", { class: "small muted", style: "padding:4px 2px" },
        assets.length ? "该类型下暂无素材" : "尚未导入素材"));
    }
    for (const a of shown) {
      const current = a.kind === "image" ? a.id === store.ctx.imageId : a.id === store.ctx.videoId;
      const row = h("button", { class: `item${current ? " on" : ""}`, type: "button" },
        icon(a.kind === "video" ? "film" : "image", 14, current ? "var(--hls-accent)" : "var(--hls-text-muted)"),
        h("div", { class: "grow", style: "min-width:0" },
          h("div", { class: "item__t ellipsis" }, a.name),
          h("div", { class: "item__d" }, a.meta)),
      );
      row.addEventListener("click", () => selectAsset(a));
      listEl.append(row);
    }

    clear(presetEl);
    for (const p of store.ctx.state?.presets ?? []) {
      const row = h("button", { class: `item${p.name === activePreset ? " on" : ""}`, type: "button" },
        h("div", { class: "grow", style: "min-width:0" },
          h("div", { class: "item__t" }, p.name),
          h("div", { class: "item__d" }, presetSummary(p))),
      );
      row.addEventListener("click", () => applyPreset(p));
      presetEl.append(row);
    }
  }

  async function importFile(file: File): Promise<void> {
    store.set({ status: `正在导入 ${file.name}…` });
    try {
      const res: MediaResult = await api.media(file, file.name);
      if (res.kind === "image") {
        const asset: Asset = {
          id: res.image_id, kind: "image", name: file.name,
          meta: `${res.width} × ${res.height}`,
          png: pngUrl(res.original_png_b64), videoInfo: null,
        };
        assets.unshift(asset);
        store.set({
          mediaKind: "image", imageId: res.image_id, videoId: null, mediaName: file.name,
          originalPng: asset.png, videoInfo: null, frameIndex: 0,
          lastRender: null, depthPng: null, backend: "", fromCache: false,
          status: `已导入图片 ${file.name} · ${res.width} × ${res.height}`,
        });
        if (res.high_res_hint) { highResHint = true; hintDismissed = false; }
      } else {
        const info = res.info;
        const frame = await api.videoFrame(res.video_id, 0);
        const asset: Asset = {
          id: res.video_id, kind: "video", name: file.name,
          meta: `${info.width} × ${info.height} · ${info.duration.toFixed(1)}s`,
          png: pngUrl(frame.png_b64),
          videoInfo: {
            width: info.width, height: info.height, fps: info.fps,
            duration: info.duration, frame_count: info.frame_count, has_audio: info.has_audio,
          },
        };
        assets.unshift(asset);
        store.set({
          mediaKind: "video", imageId: null, videoId: res.video_id, mediaName: file.name,
          originalPng: asset.png, videoInfo: asset.videoInfo, frameIndex: 0,
          lastRender: null, depthPng: null, backend: "", fromCache: false,
          status: `已导入视频 ${file.name} · ${info.width} × ${info.height} · 已自动切换到「视频调光」`,
        });
      }
      depthOn = false;
      compare = false;
      canvas.setPicking(pickOn);
      renderLeft();
      renderToolbar();
      syncCanvasImage();
      updateStats(null);
      refreshBanner();
      if (store.ctx.imageId) scheduleRender();
      // 智能识别并更改模式：导入的是视频就自动切到「视频调光」，
      // 该屏拥有逐帧检查、分析模式与导出视频的完整能力。
      if (store.ctx.mediaKind === "video") showScreen("video");
    } catch (e) {
      reportError(e);
      store.set({ status: "导入失败" });
    }
  }

  function selectAsset(a: Asset): void {
    if (a.kind === "image") {
      store.set({
        mediaKind: "image", imageId: a.id, videoId: null, mediaName: a.name,
        originalPng: a.png, videoInfo: null, frameIndex: 0,
        lastRender: null, depthPng: null, status: `已选中 ${a.name}`,
      });
    } else {
      store.set({
        mediaKind: "video", videoId: a.id, imageId: null, mediaName: a.name,
        originalPng: a.png, videoInfo: a.videoInfo, frameIndex: 0,
        lastRender: null, depthPng: null, status: `已选中视频 ${a.name}`,
      });
    }
    depthOn = false;
    compare = false;
    renderLeft();
    renderToolbar();
    syncCanvasImage();
    updateStats(null);
    refreshBanner();
    if (a.kind === "image") scheduleRender();
    // 选中视频素材同样自动切模式（与导入行为一致）
    else showScreen("video");
  }

  function applyPreset(p: Preset): void {
    activePreset = p.name;
    setParams(cloneParams(p.params));
    undoParams = null;
    refOffset = null;
    renderLeft();
    renderToolbar();
    renderRight();
    scheduleRender();
    toast(`已套用预设：${p.name}`, "ok");
  }

  function resetTarget(): RenderParams | null {
    const st = store.ctx.state;
    const p = st?.presets.find((x) => x.name === activePreset);
    if (p) return cloneParams(p.params);
    if (st?.params) return cloneParams(st.params);
    return store.ctx.params ? cloneParams(currentParams()) : null;
  }

  function doReset(): void {
    const target = resetTarget();
    if (!target) return;
    setParams(target);
    undoParams = null;
    refOffset = null;
    selectedLight = 0;
    renderLeft();
    renderToolbar();
    renderRight();
    scheduleRender();
    store.set({ status: activePreset ? `已重置为预设「${activePreset}」的默认参数` : "已重置为后端默认参数" });
  }

  // ------------------------------------------------------------ 中间：工具栏与画布

  const renderStateEl = h("span", { class: "muted" }, "");
  const coordEl = h("span", { class: "mono" }, "x —  y —");

  const toolbar = h("div", {
    class: "row",
    style: "padding:8px 12px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--hls-border)",
  });

  const bannerSlot = h("div");

  const statsEl = h("div", { class: "overlay", style: "left:auto;top:auto;right:12px;bottom:12px" });
  const stage = h("div", { class: "stage" },
    canvas.el,
    h("div", { class: "overlay" },
      icon("crosshair", 12), coordEl,
      h("span", { class: "muted" }, "滚轮缩放 · 拖拽平移")),
    statsEl,
  );

  const center = h("div", { class: "main" }, toolbar, bannerSlot, stage);

  const formatSelect = h("select", { class: "select", style: "width:auto" });
  for (const f of [{ id: "png", label: "PNG" }, { id: "jpeg", label: "JPEG" }] as const) {
    formatSelect.append(h("option", { value: f.id }, f.label));
  }
  formatSelect.addEventListener("change", () => { format = formatSelect.value === "jpeg" ? "jpeg" : "png"; });

  function renderToolbar(): void {
    const p = currentParams();
    const hasImage = !!store.ctx.imageId;
    formatSelect.value = format;
    clear(toolbar);
    const pickBtn = button("拾取主光", () => {
      pickOn = !pickOn;
      canvas.setPicking(pickOn);
      renderToolbar();
      store.set({ status: pickOn ? "拾取主光：在画布上点击取样方向" : "已退出拾取" });
    }, { icon: "crosshair", on: pickOn, title: "在画布上点击，按该点的法线方向设置主光" });
    pickBtn.disabled = !hasImage;
    const exportBtn = button("导出", () => void doExport(), { icon: "download", variant: "primary" });
    exportBtn.disabled = !hasImage;

    toolbar.append(
      segmented<"linear" | "hq">(
        [{ id: "linear", label: "快速预览" }, { id: "hq", label: "高质量物理阴影" }],
        p.lighting_mode,
        (id) => apply((q) => { q.lighting_mode = id; }, true)),
      h("div", { style: "width:1px;height:18px;background:var(--hls-border)" }),
      h("span", { class: "small muted" }, "阴影锐度"),
      segmented<"hard" | "soft">(
        [{ id: "hard", label: "硬朗·赛璐珞" }, { id: "soft", label: "柔化半影" }],
        p.shadow_mode,
        (id) => apply((q) => { q.shadow_mode = id; }, true)),
      h("div", { class: "spacer" }),
      pickBtn,
      button("显示深度图", () => void toggleDepth(), { icon: "layers", on: depthOn }),
      button("对比原图", () => {
        compare = !compare;
        syncCanvasImage();
        renderToolbar();
      }, { icon: "rotate", on: compare, title: "在原图与最近一次渲染之间切换" }),
      button("一键重置", () => doReset(), { icon: "power", title: "把全部参数恢复为当前预设的默认值" }),
      formatSelect,
      exportBtn,
      renderStateEl,
    );
  }

  async function toggleDepth(): Promise<void> {
    const imageId = store.ctx.imageId;
    if (!imageId) { toast("请先导入图片", "error"); return; }
    depthOn = !depthOn;
    if (depthOn && !store.ctx.depthPng) {
      store.set({ status: "正在生成深度预览…" });
      try {
        const r = await api.depthPreview(imageId);
        store.set({ depthPng: pngUrl(r.depth_png_b64), status: "深度预览已就绪（相对深度，非绝对尺度）" });
      } catch (e) {
        reportError(e);
        depthOn = false;
        renderToolbar();
        return;
      }
    }
    syncCanvasImage();
    renderToolbar();
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

  function updateStats(res: { elapsed: number; backend: string; from_cache: boolean } | null): void {
    const count = currentParams().lights.length;
    const item = (k: string, v: string) => h("span", {}, h("span", { class: "muted" }, k), " ", h("b", {}, v));
    clear(statsEl).append(
      item("光源", `${count} 个`),
      item("耗时", res ? `${res.elapsed.toFixed(2)} s` : "—"),
      item("深度来源", res ? backendLabel(res.backend) : "—"),
      item("缓存", res ? (res.from_cache ? "命中" : "未命中") : "—"),
    );
  }

  // ------------------------------------------------------------ 右侧：光照控制

  const rightBody = h("div", { class: "panel__body" });

  const refInput = h("input", { type: "file", accept: "image/*", class: "hidden" });
  refInput.addEventListener("change", () => {
    const f = refInput.files?.[0];
    refInput.value = "";
    if (f) void transferReference(f);
  });

  const right = h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
    h("div", { class: "panel__head" },
      icon("lightbulb", 13), h("span", {}, "光照控制"), h("div", { class: "spacer" }),
      button("全部重置", () => doReset(), { variant: "ghost", title: "恢复当前预设的默认参数" })),
    rightBody,
  );

  function renderRight(): void {
    const p = currentParams();
    if (selectedLight >= p.lights.length) selectedLight = Math.max(0, p.lights.length - 1);
    clear(rightBody);

    rightBody.append(
      slider({
        label: "环境光强度", min: 0, max: 2, step: 0.01, value: p.ambient_intensity,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply((q) => { q.ambient_intensity = v; }),
      }),
      slider({
        label: "环境光色温", min: 2000, max: 12000, step: 100, value: p.ambient_kelvin,
        format: (v) => `${Math.round(v)} K`,
        onInput: (v) => apply((q) => { q.ambient_kelvin = v; }),
      }),
      slider({
        label: "整体曝光", min: 0.2, max: 2.5, step: 0.01, value: p.exposure,
        format: (v) => `${v.toFixed(2)} ×`,
        onInput: (v) => apply((q) => { q.exposure = v; }),
      }),
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "small muted" }, `光源列表 · ${p.lights.length} 个 · 无限叠加`),
        h("div", { class: "row", style: "gap:6px" },
          button("添加光源", () => addLight(), { icon: "plus", title: "新增一个光源节点" }),
          deleteLightButton(),
        )),
      h("div", { class: "list" }, ...p.lights.map((l, i) => lightRow(l, i))),
      h("div", { class: "divider" }),
      editorSlot(p),
      h("div", { class: "divider" }),
      refCard(),
      refInput,
    );
    // 同步绘制一次轨迹球：后台标签页里 requestAnimationFrame 可能被节流甚至暂停
    if (trackball?.el.isConnected) trackball.draw();
  }

  function lightRow(l: RenderParams["lights"][number], i: number): HTMLElement {
    const row = h("button", { class: `item${i === selectedLight ? " on" : ""}`, type: "button" },
      h("i", {
        style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:${i === selectedLight ? "var(--hls-warm)" : "var(--hls-border-strong)"}`,
      }),
      h("div", { class: "grow", style: "min-width:0" },
        h("div", { class: "item__t ellipsis" }, l.name || `光源 ${i + 1}`),
        h("div", { class: "item__d" }, `${l.kind === "point" ? "点光" : "平行光"} · ${Math.round(l.kelvin)} K · 强度 ${l.intensity.toFixed(2)}`)),
      h("span", { class: "tiny muted" }, l.visible ? "启用" : "停用"),
    );
    row.addEventListener("click", () => { selectedLight = i; renderRight(); });
    return row;
  }

  function deleteLightButton(): HTMLButtonElement {
    const b = button("删除光源", () => {
      if (currentParams().lights.length <= 1) return;
      apply((q) => { q.lights.splice(selectedLight, 1); });
      selectedLight = Math.max(0, selectedLight - 1);
      renderRight();
    }, { icon: "trash", variant: "danger", title: "删除当前选中的光源（至少保留一个）" });
    b.disabled = currentParams().lights.length <= 1;
    return b;
  }

  function addLight(): void {
    apply((q) => {
      q.lights.push({
        name: `光源 ${q.lights.length + 1}`, kind: "directional",
        dx: 0, dy: -0.35, dz: 1, px: 0, py: 0, pz: -1,
        intensity: 1, kelvin: 5600, radius: 0.4, visible: true,
      });
    });
    selectedLight = currentParams().lights.length - 1;
    renderRight();
  }

  function editorSlot(p: RenderParams): HTMLElement {
    const i = selectedLight;
    const l = p.lights[i];
    if (!l) return h("div", { class: "small muted" }, "请选择光源");

    const nameInput = h("input", { class: "input", value: l.name, maxlength: 24 });
    nameInput.addEventListener("input", () => {
      const v = nameInput.value;
      apply((q) => { const t = q.lights[i]; if (t) t.name = v; });
    });

    let direction: HTMLElement;
    if (l.kind === "directional") {
      const tb = new Trackball(l.dx, l.dy, l.dz, (dx, dy, dz) => {
        apply((q) => { const t = q.lights[i]; if (t) { t.dx = dx; t.dy = dy; t.dz = dz; } });
      });
      trackball = tb;
      direction = tb.el;
    } else {
      direction = h("div", { class: "grid3" },
        numberField("X", l.px, -3, 3, 0.05, (v) => apply((q) => { const t = q.lights[i]; if (t) t.px = v; })),
        numberField("Y", l.py, -3, 3, 0.05, (v) => apply((q) => { const t = q.lights[i]; if (t) t.py = v; })),
        numberField("Z", l.pz, -3, 3, 0.05, (v) => apply((q) => { const t = q.lights[i]; if (t) t.pz = v; })),
      );
    }

    const visible = h("input", { type: "checkbox", checked: l.visible });
    visible.addEventListener("change", () => {
      const on = visible.checked;
      apply((q) => { const t = q.lights[i]; if (t) t.visible = on; });
      renderRight();
    });

    return h("div", { class: "card", style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, `光源 ${i + 1} · 参数`),
        h("span", { class: "chip tiny" }, l.kind === "point" ? "点光" : "平行光")),
      h("div", { class: "field" }, h("label", {}, "名称"), nameInput),
      h("div", { class: "field" }, h("label", {}, "类型"),
        segmented<"directional" | "point">(
          [{ id: "directional", label: "平行光" }, { id: "point", label: "点光" }],
          l.kind,
          (id) => apply((q) => { const t = q.lights[i]; if (t) t.kind = id; }, true))),
      direction,
      slider({
        label: "强度", min: 0, max: 4, step: 0.01, value: l.intensity,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply((q) => { const t = q.lights[i]; if (t) t.intensity = v; }),
      }),
      slider({
        label: "色温", min: 1500, max: 12000, step: 100, value: l.kelvin,
        format: (v) => `${Math.round(v)} K`,
        onInput: (v) => apply((q) => { const t = q.lights[i]; if (t) t.kelvin = v; }),
      }),
      slider({
        label: "半径", min: 0.05, max: 1.5, step: 0.01, value: l.radius,
        format: (v) => v.toFixed(2),
        onInput: (v) => apply((q) => { const t = q.lights[i]; if (t) t.radius = v; }),
      }),
      h("label", { class: "row", style: "gap:7px;font-size:12px;cursor:pointer" }, visible, "启用此光源"),
    );
  }

  function refCard(): HTMLElement {
    const card = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "row row--between" },
        h("span", { class: "small", style: "font-weight:600" }, "参考图迁移"),
        h("span", { class: "chip tiny" }, "保守微调")),
      button("导入参考图提取光照", () => refInput.click(), { icon: "image" }),
      h("div", { class: "tiny muted" }, "仅迁移主光方向与色温，保留原图明暗结构。"),
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
        renderToolbar();
        renderRight();
        scheduleRender();
        toast("已撤销参考图迁移", "ok");
      }, { icon: "rotate", variant: "ghost" }));
    }
    return card;
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
      renderToolbar();
      renderRight();
      scheduleRender();
      store.set({ status: "已应用参考图光照" });
    } catch (e) {
      reportError(e);
      store.set({ status: "参考图迁移失败" });
    }
  }

  // ------------------------------------------------------------ 高分辨率提示

  function refreshBanner(): void {
    clear(bannerSlot);
    const hint = highResHint || !!store.ctx.state?.high_res_hint;
    if (!hint || hintDismissed) return;
    bannerSlot.append(h("div", { class: "banner", style: "margin:8px 12px 0" },
      icon("info", 14, "var(--hls-accent-bright)"),
      h("span", { class: "grow" }, "当前素材分辨率较高：本地引擎会以较低分辨率估算深度。开启云端 AI 可获得更精细的深度与法线。"),
      button("开启云端 AI", () => openSettings(), { variant: "primary" }),
      button("忽略", () => { hintDismissed = true; refreshBanner(); }, { variant: "ghost" }),
    ));
  }

  // ------------------------------------------------------------ 画布交互

  canvas.onHover((x, y) => { coordEl.textContent = `x ${x}  y ${y}`; });
  canvas.onPick((x, y) => {
    const imageId = store.ctx.imageId;
    if (!pickOn || !imageId) return;
    void (async () => {
      try {
        const r = await api.pick(imageId, x, y);
        apply((q) => {
          const key = q.lights[0];
          if (key) { key.dx = r.dx; key.dy = r.dy; key.dz = r.dz; }
        });
        // renderRight() 会用新参数重建轨迹球并同步重绘
        renderRight();
        store.set({ status: `已按 (${x}, ${y}) 处的法线设置主光方向` });
      } catch (e) {
        reportError(e);
      }
    })();
  });

  // ------------------------------------------------------------ 装配

  function syncFromMedia(): void {
    if (!assets.length && store.ctx.mediaKind && store.ctx.mediaName) {
      const id = store.ctx.mediaKind === "image" ? store.ctx.imageId : store.ctx.videoId;
      if (id) {
        const v = store.ctx.videoInfo;
        assets.push({
          id, kind: store.ctx.mediaKind, name: store.ctx.mediaName,
          meta: v ? `${v.width} × ${v.height} · ${v.duration.toFixed(1)}s` : "本会话素材",
          png: store.ctx.originalPng, videoInfo: v,
        });
      }
    }
  }

  function matchPreset(p: RenderParams): string {
    const key = JSON.stringify(p);
    return store.ctx.state?.presets.find((x) => JSON.stringify(x.params) === key)?.name ?? "";
  }

  return {
    id: "workbench",
    name: "光影工作台",
    subtitle: "素材 · 光源 · 实时预览",
    icon: "layers",
    mount(root: HTMLElement): void {
      clear(root).append(left, center, right);
      if (!store.ctx.params) {
        clear(root).append(h("div", { style: "padding:24px" },
          h("div", { class: "banner" }, "等待后端状态…请确认本地服务已启动。")));
        return;
      }
      if (!mounted) { mounted = true; activePreset = matchPreset(currentParams()); }
      syncFromMedia();
      canvas.setPicking(pickOn);
      syncCanvasImage();
      renderLeft();
      renderToolbar();
      renderRight();
      updateStats(null);
      refreshBanner();
    },
    unmount(): void {
      if (timer !== null) { window.clearTimeout(timer); timer = null; }
      busy = false;
      dirty = false;
      trackball = null;
    },
  };
}
