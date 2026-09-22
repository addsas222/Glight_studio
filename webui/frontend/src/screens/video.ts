/**
 * 屏幕 A4 · 视频调光：上传视频、逐帧检查、时间轴定位、批处理与导出。
 *
 * 职责：
 *   - 打开视频走 /api/video（统一入口按魔数识别），显示宽高 / fps / 时长 / 帧数 / 音轨；
 *   - 中央预览用 /api/video/frame 取真实帧，滚轮缩放 + 平移 + 点击拾取主光方向；
 *   - 时间轴用 /api/video/thumbnails 生成缩略图条，点击定位，关键帧打点；
 *   - 左侧配置分析模式（关键帧传播 / 逐帧分析）、关键帧数量、时间轴平滑；
 *   - 右侧是与图片屏幕共用同一份 params 的光源控制；
 *   - 开始处理 → /api/video/process → 轮询任务进度，可取消，完成后下载成品。
 *
 * 诚实性：逐帧分析的剩余时间只在**本次任务真正测到**每帧耗时后按实测值外推并标注「实测」，
 * 未测到之前一律标注「估算」并且不给具体数字。
 */
import {
  api, JobState, Light, MediaVideo, RenderParams, VideoInfo,
  downloadBlobUrl, pngUrl, pollJob,
} from "../api";
import { button, CanvasView, h, icon, reportError, segmented, slider, store, toast, Trackball } from "../ui";
import { Screen, currentParams, setParams } from "../shell";

// ---------------------------------------------------------------- 小工具

const cloneParams = (p: RenderParams): RenderParams => structuredClone(p);

const pad2 = (n: number): string => (n < 10 ? `0${n}` : String(n));

/** 秒 → mm:ss.ff（按 fps 折算帧内时间） */
function fmtTime(sec: number, fps: number): string {
  const s = Math.max(0, sec);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  const frame = Math.floor((rest - Math.floor(rest)) * Math.max(fps, 1));
  return `${pad2(m)}:${pad2(Math.floor(rest))}.${pad2(frame)}`;
}

function fmtDur(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${pad2(s - m * 60)} 秒`;
}

// ---------------------------------------------------------------- 屏幕

export function createVideoScreen(): Screen {
  // ---- 媒体
  let videoId: string | null = null;
  let info: VideoInfo | null = null;
  let sourceName = "";
  let index = 0;

  // ---- 播放
  let playing = false;
  let playToken = 0;
  const timers = new Set<number>();

  // ---- 拾取
  let pickOn = false;

  // ---- 分析配置
  let mode: "keyframe" | "perframe" = "keyframe";
  let keyframeCount = 8;
  let smooth = 0.6;
  let keyframes: number[] = [];

  // ---- 任务
  let jobId: string | null = null;
  let running = false;
  let progress = 0;
  let stageMsg = "未开始";
  let errorMsg = "";
  let finished = false;
  let downloadName = "result.mp4";
  /** 本次任务真正测到的每帧耗时（毫秒），没有样本时为 null */
  let perFrameMs: number | null = null;
  let lastTick: { n: number; at: number; total: number } | null = null;
  let measuredTotal = 0;
  let measuredDone = 0;

  let mounted = false;

  // ---- DOM
  let topInfoEl!: HTMLElement;
  let overlayEl!: HTMLElement;
  let stripEl!: HTMLElement;
  let cursorEl: HTMLElement | null = null;
  let transportEl!: HTMLElement;
  let leftEl!: HTMLElement;
  let lightRootEl!: HTMLElement;
  let rowChips: HTMLElement[] = [];
  let processEl!: HTMLElement;
  let statusEl!: HTMLElement;
  let progressBar!: HTMLElement;
  let progressText!: HTMLElement;
  let etaText!: HTMLElement;
  let errorBox!: HTMLElement;
  let playBtn!: HTMLButtonElement;
  let cancelBtn!: HTMLButtonElement;
  let processBtn!: HTMLButtonElement;
  let downloadBtn!: HTMLButtonElement;

  let view: CanvasView | null = null;
  let ball: Trackball | null = null;
  let ballHandler: (dx: number, dy: number, dz: number) => void = () => {};
  let selectedLight = 0;

  // ------------------------------------------------------- 通用

  function sleep(ms: number): Promise<void> {
    const { promise, resolve } = Promise.withResolvers<void>();
    const id = window.setTimeout(() => {
      timers.delete(id);
      resolve();
    }, ms);
    timers.add(id);
    return promise;
  }

  const lastIndex = (): number => Math.max((info?.frame_count ?? 1) - 1, 0);

  const lights = (): Light[] => currentParams().lights;

  /** 拖动滑块时只把参数同步进全局状态，不重建光源面板（重建会打断正在拖的滑块） */
  function touchParams(): void {
    setParams(currentParams());
    renderStatus();
  }

  /** 更新光源列表里某一行的强度/色温摘要 */
  function updateRowChip(i: number): void {
    const light = currentParams().lights[i];
    const chip = rowChips[i];
    if (!light || !chip) return;
    chip.textContent = `${Math.round(light.intensity * 100)}% · ${Math.round(light.kelvin)}K`;
  }

  // ------------------------------------------------------- 媒体

  /** 采用外壳中已导入的视频（光影工作台导入后自动切到本屏时调用）。 */
  async function adoptExisting(): Promise<void> {
    const id = store.ctx.videoId;
    const vi = store.ctx.videoInfo;
    if (!id || !vi || !mounted) return;
    videoId = id;
    sourceName = store.ctx.mediaName || "已导入视频";
    // VideoInfo 在 store 里是精简结构，补齐渲染所需的字段
    info = {
      path: "", width: vi.width, height: vi.height, fps: vi.fps,
      duration: vi.duration, frame_count: vi.frame_count, codec: "",
      has_audio: vi.has_audio, container: "",
    };
    index = 0;
    keyframes = [];
    finished = false;
    errorMsg = "";
    progress = 0;
    stageMsg = "未开始";
    perFrameMs = null;
    lastTick = null;
    store.set({ status: `已载入视频 ${sourceName}` });
    renderTopInfo();
    renderTimeline();
    renderTransport();
    renderProcess();
    renderStatus();
    await seek(0);
    void loadThumbs();
  }

  async function openVideo(file: File): Promise<void> {
    try {
      const res: MediaVideo = await api.videoUpload(file, file.name);
      if (!mounted) return;
      videoId = res.video_id;
      info = res.info;
      sourceName = res.source_name || file.name;
      index = 0;
      keyframes = [];
      finished = false;
      errorMsg = "";
      progress = 0;
      stageMsg = "未开始";
      perFrameMs = null;
      lastTick = null;
      store.set({
        videoId,
        mediaKind: "video",
        mediaName: sourceName,
        videoInfo: {
          width: info.width, height: info.height, fps: info.fps,
          duration: info.duration, frame_count: info.frame_count, has_audio: info.has_audio,
        },
        frameIndex: 0,
        status: `已载入视频 ${sourceName}`,
      });
      renderTopInfo(res);
      renderTimeline();
      renderTransport();
      renderProcess();
      renderStatus();
      await seek(0);
      void loadThumbs();
    } catch (e) {
      reportError(e);
    }
  }

  async function loadThumbs(): Promise<void> {
    if (!videoId) return;
    try {
      const res = await api.videoThumbnails(videoId, 12, 96);
      if (!mounted) return;
      stripEl.replaceChildren();
      const strip = h("div", {
        style: "position:relative;flex:1;min-width:0;display:flex;gap:2px;height:100%;align-items:stretch",
      });
      res.png_b64.forEach((b64, i) => {
        const frame = res.frames[i] ?? 0;
        const cell = h("div", {
          title: `帧 ${frame}`,
          style: `flex:1;min-width:0;position:relative;border-radius:4px;overflow:hidden;cursor:pointer;
                  border:1px solid var(--hls-border);background:var(--hls-bg-canvas)`,
        },
          h("img", {
            src: pngUrl(b64), alt: `帧 ${frame}`,
            style: "width:100%;height:100%;object-fit:cover;display:block;pointer-events:none",
          }),
          h("span", {
            class: "tiny mono",
            style: `position:absolute;left:3px;bottom:2px;color:var(--hls-text-secondary);
                    text-shadow:0 1px 3px #000`,
          }, String(frame)),
        );
        cell.addEventListener("click", () => { void seek(frame); });
        strip.append(cell);
      });

      // 关键帧打点
      const line = h("div", { style: "position:absolute;inset:0;pointer-events:none" });
      for (const kf of keyframes) {
        const pct = (kf / Math.max(info?.frame_count ?? 1, 1)) * 100;
        line.append(h("div", {
          title: `关键帧 帧 ${kf}`,
          style: `position:absolute;top:0;bottom:0;left:${pct}%;width:2px;background:var(--hls-warm);
                  box-shadow:0 0 6px var(--hls-warm)`,
        }));
      }
      // 当前帧游标
      const cursorPct = (index / Math.max(info?.frame_count ?? 1, 1)) * 100;
      cursorEl = h("div", {
        style: `position:absolute;top:-6px;bottom:-6px;left:${cursorPct}%;width:2px;background:var(--hls-accent-bright)`,
      });
      line.append(cursorEl);
      strip.append(line);

      stripEl.append(strip);
    } catch (e) {
      reportError(e);
    }
  }

  async function seek(target: number): Promise<void> {
    if (!videoId || !info) return;
    const i = Math.max(0, Math.min(Math.round(target), lastIndex()));
    index = i;
    store.set({ frameIndex: i });
    renderTransport();
    renderTimelineCursor();
    renderStatus();
    try {
      const res = await api.videoFrame(videoId, i);
      if (!mounted) return;
      view?.setImage(pngUrl(res.png_b64));
      store.set({ originalPng: res.png_b64 });
      renderOverlay();
    } catch (e) {
      reportError(e);
    }
  }

  async function play(): Promise<void> {
    if (!videoId || playing) return;
    const token = ++playToken;
    playing = true;
    updatePlayButton();
    renderStatus();
    while (playing && token === playToken && mounted) {
      if (index >= lastIndex()) break;
      await seek(index + 1);
      if (!playing || token !== playToken || !mounted) break;
      await sleep(Math.max(50, 1000 / Math.max(info?.fps ?? 30, 1)));
    }
    if (token === playToken) {
      playing = false;
      updatePlayButton();
      renderStatus();
    }
  }

  function stopPlay(): void {
    playing = false;
    playToken += 1;
    updatePlayButton();
    renderStatus();
  }

  function updatePlayButton(): void {
    if (!playBtn) return;
    playBtn.replaceChildren(icon(playing ? "pause" : "play", 14));
    playBtn.title = playing ? "暂停" : "播放";
  }

  // ------------------------------------------------------- 渲染

  function renderTopInfo(media?: MediaVideo): void {
    if (!topInfoEl || !info) {
      if (topInfoEl) topInfoEl.replaceChildren();
      return;
    }
    const limits = store.ctx.state?.video_limits;
    const parts: string[] = [
      `${info.width} × ${info.height}`,
      `${info.fps.toFixed(2)} fps`,
      `${info.duration.toFixed(2)} s`,
      `${info.frame_count} 帧`,
      info.has_audio ? "含音轨" : "无音轨",
      info.codec || info.container,
    ];
    topInfoEl.replaceChildren();
    const chips: HTMLElement[] = [
      h("span", { class: "chip", style: "border-color:var(--hls-accent-line)" },
        icon("film", 12, "var(--hls-accent)"), sourceName || "视频"),
      ...parts.map((t) => h("span", { class: "chip" }, t)),
    ];
    if (media?.hints.over_recommended_size) {
      chips.push(h("span", { class: "chip", style: "color:var(--hls-warm);border-color:var(--hls-warm)" },
        `分辨率高于推荐值${limits ? `（推荐长边 ≤ ${limits.recommend_max_side}）` : ""}`));
    }
    if (media?.hints.over_recommended_duration) {
      chips.push(h("span", { class: "chip", style: "color:var(--hls-warm);border-color:var(--hls-warm)" },
        `时长超过推荐值${limits ? `（推荐 ≤ ${limits.recommend_max_duration}s）` : ""}`));
    }
    if (media?.hints.cloud_suggested) {
      chips.push(h("span", { class: "chip" }, "该规格建议启用云端 AI 以缩短耗时"));
    }
    topInfoEl.replaceChildren(...chips);
  }

  function renderOverlay(): void {
    if (!overlayEl || !info) return;
    const t = index / Math.max(info.fps, 0.001);
    overlayEl.replaceChildren(
      h("span", {}, `帧 ${index} / ${lastIndex()} · ${fmtTime(t, info.fps)}`),
      h("span", { class: "muted" }, pickOn ? "拾取模式：点击画面指定主光方向" : "滚轮缩放 · 拖拽平移"),
    );
  }

  function renderTransport(): void {
    if (!transportEl) return;
    const fps = info?.fps ?? 0;
    const frameInput = h("input", {
      class: "input", type: "number", min: 0, max: lastIndex(), step: 1,
      value: String(index), style: "width:86px",
    });
    // 只在提交（回车 / 失焦）时跳帧，避免输入过程中被重建打断
    frameInput.addEventListener("change", () => {
      const v = Number(frameInput.value);
      if (Number.isFinite(v)) {
        stopPlay();
        void seek(v);
      }
    });
    transportEl.replaceChildren(
      button("", () => { stopPlay(); void seek(0); }, { icon: "rotate", variant: "icon", title: "回到首帧" }),
      button("", () => { stopPlay(); void seek(index - 1); }, { icon: "minus", variant: "icon", title: "上一帧" }),
      playBtn,
      button("", () => { stopPlay(); void seek(index + 1); }, { icon: "plus", variant: "icon", title: "下一帧" }),
      button("", () => { stopPlay(); void seek(lastIndex()); }, { icon: "crosshair", variant: "icon", title: "跳到末帧" }),
      h("div", { class: "field", style: "width:96px" }, h("label", {}, "帧号"), frameInput),
      h("span", { class: "mono small sec" }, info
        ? `${fmtTime(index / Math.max(fps, 0.001), fps)} / ${fmtTime(info.duration, fps)}`
        : "—"),
      h("div", { class: "spacer" }),
      h("span", { class: "tiny muted" }, info ? `${info.fps.toFixed(2)} fps · ${info.codec}` : ""),
    );
  }

  function renderTimeline(): void {
    if (!stripEl) return;
    if (!videoId) {
      stripEl.replaceChildren(h("div", { class: "small muted", style: "padding:10px 2px" },
        "打开视频后此处显示时间轴缩略图条。"));
    }
  }

  /** 只移动当前帧游标，不重建整条缩略图 */
  function renderTimelineCursor(): void {
    if (cursorEl && info) {
      const pct = (index / Math.max(info.frame_count, 1)) * 100;
      cursorEl.style.left = `${pct}%`;
    }
  }

  function renderLights(): void {
    if (!lightRootEl) return;
    const p = currentParams();
    const list = p.lights;
    if (selectedLight >= list.length) selectedLight = Math.max(list.length - 1, 0);

    lightRootEl.replaceChildren();

    // 预设
    const presets = store.ctx.state?.presets ?? [];
    if (presets.length) {
      lightRootEl.append(h("div", { class: "tiny muted", style: "letter-spacing:.5px;margin-bottom:4px" }, "预设场景"));
      const grid = h("div", { class: "grid2", style: "margin-bottom:6px" });
      for (const preset of presets) {
        const b = h("button", { class: "btn", type: "button", style: "justify-content:center" }, preset.name);
        b.addEventListener("click", () => {
          setParams(cloneParams(preset.params));
          selectedLight = 0;
          renderLights();
          renderStatus();
          toast(`已应用预设：${preset.name}`, "ok");
        });
        grid.append(b);
      }
      lightRootEl.append(grid);
    }

    // 环境光 / 曝光
    lightRootEl.append(
      slider({
        label: "环境光强度", min: 0, max: 2, step: 0.01, value: p.ambient_intensity,
        onInput: (v) => { p.ambient_intensity = v; touchParams(); },
      }),
      slider({
        label: "环境光色温 (K)", min: 1500, max: 12000, step: 50, value: p.ambient_kelvin,
        format: (v) => `${Math.round(v)}K`,
        onInput: (v) => { p.ambient_kelvin = v; touchParams(); },
      }),
      slider({
        label: "曝光", min: 0.2, max: 2.5, step: 0.01, value: p.exposure,
        onInput: (v) => { p.exposure = v; touchParams(); },
      }),
      h("div", { class: "divider" }),
      h("div", { class: "row row--between" },
        h("span", { class: "tiny muted" }, `光源 · 共 ${list.length} 个`),
        h("div", { class: "row", style: "gap:6px" },
          button("添加", () => {
            const n = list.length + 1;
            list.push({
              name: `光源 ${n}`, kind: "directional",
              dx: 0.5, dy: -0.5, dz: 0.7, px: 0.5, py: 0.5, pz: 1,
              intensity: 0.8, kelvin: 5600, radius: 0.5, visible: true,
            });
            selectedLight = list.length - 1;
            setParams(currentParams());
            renderLights();
            renderStatus();
          }, { icon: "plus", variant: "ghost" }),
          button("删除", () => {
            if (list.length <= 1) {
              toast("至少保留一个光源", "error");
              return;
            }
            list.splice(selectedLight, 1);
            selectedLight = Math.max(selectedLight - 1, 0);
            setParams(currentParams());
            renderLights();
            renderStatus();
          }, { icon: "trash", variant: "ghost" }),
          button("重置", () => {
            const base = store.ctx.state?.params;
            if (base) {
              setParams(cloneParams(base));
              selectedLight = 0;
              renderLights();
              renderStatus();
              toast("已重置为默认光源", "ok");
            }
          }, { icon: "power", variant: "ghost" }),
        )),
    );

    // 光源列表
    const listBox = h("div", { class: "list" });
    rowChips = list.map((light, i) => {
      const chip = h("span", { class: "tiny mono muted" },
        `${Math.round(light.intensity * 100)}% · ${Math.round(light.kelvin)}K`);
      const row = h("button", {
        class: `item${i === selectedLight ? " on" : ""}`, type: "button",
      },
        h("i", {
          style: `width:8px;height:8px;border-radius:50%;flex:0 0 auto;
                  background:${light.visible ? "var(--hls-warm)" : "var(--hls-text-muted)"}`,
        }),
        h("span", { class: "grow ellipsis" }, light.name || `光源 ${i + 1}`),
        chip,
      );
      row.addEventListener("click", () => {
        selectedLight = i;
        renderLights();
      });
      listBox.append(row);
      return chip;
    });
    lightRootEl.append(listBox);

    // 选中光源的编辑器
    const light = list[selectedLight];
    if (!light) return;
    const detail = h("div", { class: "card", style: "margin-top:8px;display:flex;flex-direction:column;gap:9px" },
      h("div", { class: "row row--between" },
        h("b", { class: "small" }, light.name || `光源 ${selectedLight + 1}`),
        h("span", { class: "tiny muted" }, light.kind === "point" ? "点光" : "平行光")),
      slider({
        label: "强度", min: 0, max: 4, step: 0.01, value: light.intensity,
        onInput: (v) => { light.intensity = v; touchParams(); updateRowChip(selectedLight); },
      }),
      slider({
        label: "色温 (K)", min: 1500, max: 12000, step: 50, value: light.kelvin,
        format: (v) => `${Math.round(v)}K`,
        onInput: (v) => { light.kelvin = v; touchParams(); updateRowChip(selectedLight); },
      }),
      slider({
        label: "半径", min: 0.05, max: 1.5, step: 0.01, value: light.radius,
        onInput: (v) => { light.radius = v; touchParams(); },
      }),
      h("div", { class: "row", style: "gap:6px" },
        button(light.visible ? "光源可见" : "光源隐藏", () => {
          light.visible = !light.visible;
          setParams(currentParams());
          renderLights();
          renderStatus();
        }, { icon: "lightbulb", variant: light.visible ? "ghost" : "primary", on: light.visible }),
        button("点击画布拾取方向", () => setPick(!pickOn), { icon: "crosshair", variant: "ghost" })),
    );

    if (light.kind === "directional") {
      const readout = h("b", { class: "mono tiny muted" },
        `dx ${light.dx.toFixed(2)}  dy ${light.dy.toFixed(2)}  dz ${light.dz.toFixed(2)}`);
      const ballBox = h("div", {});
      detail.append(h("div", { class: "row row--between" },
        h("span", { class: "tiny muted" }, "方向 · 3D 轨迹球"), readout), ballBox);
      if (ball) {
        ballBox.append(ball.el);
        ball.set(light.dx, light.dy, light.dz);
        ballHandler = (dx, dy, dz) => {
          light.dx = Math.round(dx * 1000) / 1000;
          light.dy = Math.round(dy * 1000) / 1000;
          light.dz = Math.round(dz * 1000) / 1000;
          readout.textContent = `dx ${light.dx.toFixed(2)}  dy ${light.dy.toFixed(2)}  dz ${light.dz.toFixed(2)}`;
          setParams(currentParams());
        };
      }
    } else {
      detail.append(h("div", { class: "grid3" },
        slider({ label: "px", min: 0, max: 1, step: 0.01, value: light.px, onInput: (v) => { light.px = v; touchParams(); } }),
        slider({ label: "py", min: 0, max: 1, step: 0.01, value: light.py, onInput: (v) => { light.py = v; touchParams(); } }),
        slider({ label: "pz", min: 0, max: 3, step: 0.01, value: light.pz, onInput: (v) => { light.pz = v; touchParams(); } })));
    }
    lightRootEl.append(detail);
  }

  let pickBtn: HTMLButtonElement | null = null;

  /** 统一的拾取开关：画布、顶栏按钮、覆盖层提示同步 */
  function setPick(on: boolean): void {
    pickOn = on;
    view?.setPicking(on);
    if (pickBtn) {
      pickBtn.classList.toggle("btn--on", on);
      pickBtn.replaceChildren(icon("crosshair", 14), h("span", {}, on ? "拾取主光 · 开" : "拾取主光"));
    }
    renderOverlay();
  }

  function renderProcess(): void {
    if (!processEl) return;
    const pct = Math.round(progress * 100);
    progressBar.style.width = `${pct}%`;
    progressText.textContent = jobId ? `${stageMsg}　${pct}%` : "未开始";
    errorBox.textContent = errorMsg;
    errorBox.style.display = errorMsg ? "flex" : "none";
    cancelBtn.disabled = !running;
    processBtn.disabled = running || !videoId;
    downloadBtn.style.display = (finished && videoId) ? "inline-flex" : "none";

    // 诚实 ETA：只有本次任务真的测到每帧耗时，才按实测值外推
    if (mode === "perframe" && running && measuredTotal > 0) {
      if (perFrameMs !== null) {
        const remainMs = perFrameMs * Math.max(measuredTotal - measuredDone, 0);
        etaText.textContent =
          `实测 ${Math.round(perFrameMs)} ms/帧 · 预计剩余 ${fmtDur(remainMs)}（按本次实测外推）`;
      } else {
        etaText.textContent = "估算：耗时随帧数线性增加；测到实际帧速后显示实测 ETA";
      }
    } else if (running && perFrameMs !== null && measuredTotal > 0) {
      etaText.textContent = `实测 ${Math.round(perFrameMs)} ms/帧（关键帧传播模式：光照全片统一）`;
    } else {
      etaText.textContent = "";
    }
  }

  function renderStatus(): void {
    if (!statusEl) return;
    const p = currentParams();
    statusEl.textContent = [
      videoId ? `${sourceName} · 帧 ${index}/${lastIndex()}` : "未打开视频",
      `${p.lights.length} 个光源`,
      mode === "keyframe" ? "关键帧传播" : "逐帧分析",
      keyframes.length ? `已标记 ${keyframes.length} 个关键帧` : "",
      playing ? "播放中" : "",
    ].filter(Boolean).join("　|　");
  }

  // ------------------------------------------------------- 拾取

  async function pick(x: number, y: number): Promise<void> {
    if (!videoId) return;
    try {
      const res = await api.videoPick(videoId, index, x, y);
      if (!mounted) return;
      const list = lights();
      if (!list.length) return;
      const target = list[Math.min(selectedLight, list.length - 1)];
      target.kind = "directional";
      target.dx = Math.round(res.dx * 1000) / 1000;
      target.dy = Math.round(res.dy * 1000) / 1000;
      target.dz = Math.round(res.dz * 1000) / 1000;
      target.visible = true;
      touchParams();
      toast(`已拾取「${target.name || "光源"}」的方向（法线逆向）`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  // ------------------------------------------------------- 关键帧

  async function detectKeyframes(): Promise<void> {
    if (!videoId) {
      toast("请先打开视频", "error");
      return;
    }
    try {
      const res = await api.videoKeyframes(videoId, keyframeCount);
      if (!mounted) return;
      keyframes = res.keyframes;
      await loadThumbs();
      renderStatus();
      toast(`已标记 ${res.count} 个关键帧`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  // ------------------------------------------------------- 处理任务

  function onTick(s: JobState): void {
    if (!mounted) return;
    progress = s.progress;
    stageMsg = s.message || "处理中";
    const m = /(\d+)\s*\/\s*(\d+)/.exec(s.message);
    if (m) {
      const n = Number(m[1]);
      const total = Number(m[2]);
      const now = performance.now();
      if (lastTick && n > lastTick.n && total === lastTick.total) {
        const sample = (now - lastTick.at) / (n - lastTick.n);
        // 单次采样做滑动平均，避免个别帧抖动导致 ETA 剧烈跳动
        perFrameMs = perFrameMs === null ? sample : perFrameMs * 0.6 + sample * 0.4;
      }
      lastTick = { n, at: now, total };
      measuredTotal = total;
      measuredDone = n;
    }
    renderProcess();
  }

  async function process(): Promise<void> {
    if (!videoId || running) return;
    running = true;
    finished = false;
    errorMsg = "";
    progress = 0;
    stageMsg = "准备中";
    perFrameMs = null;
    lastTick = null;
    measuredTotal = 0;
    measuredDone = 0;
    renderProcess();
    try {
      const res = await api.videoProcess({
        video_id: videoId,
        params: currentParams(),
        mode,
        keyframe_count: keyframeCount,
        smooth,
      });
      jobId = res.job_id;
      store.set({ status: `视频处理中（${mode === "keyframe" ? "关键帧传播" : "逐帧分析"}）` });
      const final = await pollJob(res.job_id, onTick);
      if (!mounted) return;
      running = false;
      progress = final.progress;
      stageMsg = final.message;
      if (final.state === "done") {
        finished = true;
        downloadName = final.result?.download_name || "result.mp4";
        toast("处理完成，可以下载成品", "ok");
        store.set({ status: "视频处理完成" });
      } else if (final.state === "cancelled") {
        errorMsg = "";
        toast("已取消处理", "info");
        store.set({ status: "视频处理已取消" });
      } else {
        errorMsg = final.error || "处理失败";
        store.set({ status: "视频处理失败" });
      }
      renderProcess();
    } catch (e) {
      running = false;
      errorMsg = e instanceof Error ? e.message : String(e);
      reportError(e);
      renderProcess();
    }
  }

  async function cancel(): Promise<void> {
    if (!jobId) return;
    try {
      await api.videoCancel(jobId);
      stageMsg = "正在取消…";
      renderProcess();
    } catch (e) {
      reportError(e);
    }
  }

  async function download(): Promise<void> {
    if (!videoId) return;
    try {
      const blob = await api.videoResultBlob(videoId);
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, downloadName);
      window.setTimeout(() => URL.revokeObjectURL(url), 8000);
      toast("已开始下载成品", "ok");
    } catch (e) {
      reportError(e);
    }
  }

  // ------------------------------------------------------- 组装

  const fileInput = (() => {
    const input = h("input", { type: "file", style: "display:none" });
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (file) void openVideo(file);
      input.value = "";
    });
    return input;
  })();

  function syncAccept(): void {
    const exts = store.ctx.state?.video_exts ?? [];
    fileInput.accept = exts.length ? exts.join(",") : "video/*";
  }

  function buildTopbar(): HTMLElement {
    topInfoEl = h("div", { class: "row", style: "gap:6px;min-width:0;overflow:hidden" },
      h("span", { class: "small muted" }, "尚未打开视频"));
    pickBtn = button("拾取主光", () => setPick(!pickOn), { icon: "crosshair", variant: "ghost" });
    setPick(false);
    return h("div", {
      style: `height:48px;flex:0 0 48px;display:flex;align-items:center;gap:10px;padding:0 12px;
              background:var(--hls-bg-panel);border-bottom:1px solid var(--hls-border)`,
    },
      button("打开视频", () => fileInput.click(), { icon: "upload", variant: "primary" }),
      h("div", { style: "width:1px;height:20px;background:var(--hls-border)" }),
      topInfoEl,
      h("div", { class: "spacer" }),
      pickBtn,
      fileInput,
    );
  }

  function buildLeftPanel(): HTMLElement {
    leftEl = h("div", { class: "panel__body" });
    return h("div", { class: "panel", style: "width:236px;flex:0 0 236px" },
      h("div", { class: "panel__head" }, icon("gauge", 13), "视频分析模式"),
      leftEl,
    );
  }

  function renderLeft(): void {
    if (!leftEl) return;
    const items: HTMLElement[] = [
      segmented(
        [{ id: "keyframe", label: "关键帧传播" }, { id: "perframe", label: "逐帧分析" }],
        mode,
        (id) => { mode = id; renderLeft(); renderProcess(); renderStatus(); },
      ),
      h("div", { class: "small muted" }, mode === "keyframe"
        ? "抽取关键帧做 AI 分析，深度沿时间轴传播，光照全片统一，速度快。"
        : "每一帧独立分析，光照随画面动态变化，耗时与帧数成正比。"),
    ];
    if (mode === "keyframe") {
      items.push(slider({
        label: "关键帧数量", min: 2, max: 32, step: 1, value: keyframeCount,
        format: (v) => `${Math.round(v)} 帧`,
        onInput: (v) => { keyframeCount = Math.round(v); },
      }));
    }
    items.push(
      slider({
        label: "时间轴平滑", min: 0, max: 1, step: 0.01, value: smooth,
        // 拖动中不重建面板：否则正在拖的滑块会被替换掉，拖拽中断
        onInput: (v) => { smooth = v; },
      }),
      button("检测关键帧", () => { void detectKeyframes(); }, { icon: "sparkles", variant: "ghost" }),
      keyframes.length
        ? h("div", { class: "card small" },
          h("div", { class: "kv" }, h("span", {}, "已标记关键帧"), h("b", {}, String(keyframes.length))),
          h("div", { class: "tiny muted mono ellipsis" }, keyframes.join("、")))
        : h("div", { class: "small muted" }, "尚未检测关键帧；检测后会在时间轴上打点。"),
      h("div", { class: "divider" }),
      h("div", { class: "card", style: "font-size:10.5px;line-height:1.6" },
        h("b", { style: "display:block;color:var(--hls-text-secondary);margin-bottom:3px" }, "耗时提示"),
        h("span", { class: "muted" },
          "关键帧传播只分析少量关键帧，明显快于逐帧分析；逐帧分析的耗时随帧数线性增加，"
          + "处理时的剩余时间只按本次任务实测到的帧速外推。")),
    );
    leftEl.replaceChildren(...items);
  }

  function buildRightPanel(): HTMLElement {
    lightRootEl = h("div", { class: "panel__body" });
    processEl = h("div", { class: "panel__body", style: "gap:8px" });

    progressBar = h("i", { style: "width:0%" });
    progressText = h("span", { class: "small sec" }, "未开始");
    etaText = h("span", { class: "tiny muted" });
    errorBox = h("div", {
      class: "card",
      style: "display:none;align-items:center;gap:6px;border-color:#f87171;color:#fca5a5;font-size:11.5px",
    });
    cancelBtn = button("取消", () => { void cancel(); }, { icon: "x", variant: "danger" });
    processBtn = button("开始处理", () => { void process(); }, { icon: "play", variant: "primary" });
    downloadBtn = button("下载成品", () => { void download(); }, { icon: "download", variant: "primary" });
    downloadBtn.style.display = "none";

    processEl.append(
      h("div", { class: "row", style: "gap:8px" }, processBtn, cancelBtn, downloadBtn),
      h("div", { class: "bar" }, progressBar),
      h("div", { class: "row", style: "gap:8px" }, progressText),
      etaText,
      errorBox,
      h("div", { class: "tiny muted" },
        "处理在本机后台线程执行；处理过程中可以继续调整预览，但本次任务使用的是点击「开始处理」时的参数。"),
    );

    return h("div", { class: "panel panel--right", style: "width:300px;flex:0 0 300px" },
      h("div", { class: "panel__head" }, icon("lightbulb", 13), "光源 · 与图片模式共用参数"),
      lightRootEl,
      h("div", { class: "panel__head" }, icon("film", 13), "处理与导出"),
      processEl,
    );
  }

  function buildTimeline(): HTMLElement {
    transportEl = h("div", { class: "row", style: "gap:6px" });
    stripEl = h("div", { style: "flex:1;min-width:0;min-height:0;display:flex;align-items:stretch" });
    playBtn = button("", () => {
      if (playing) stopPlay();
      else void play();
    }, { icon: "play", variant: "icon", title: "播放" });
    return h("div", {
      style: `height:150px;flex:0 0 150px;display:flex;flex-direction:column;gap:6px;padding:8px 12px;
              background:var(--hls-bg-panel);border-top:1px solid var(--hls-border)`,
    },
      transportEl,
      h("div", { style: "flex:1;min-height:0;display:flex" }, stripEl),
    );
  }

  function buildStage(): HTMLElement {
    view = new CanvasView();
    view.onPick((x, y) => { void pick(x, y); });
    overlayEl = h("div", { class: "overlay" });
    return h("div", { class: "stage" }, view.el, overlayEl);
  }

  function buildStatusLine(): HTMLElement {
    statusEl = h("div", { class: "ellipsis", style: "flex:1;min-width:0" }, "未打开视频");
    return h("div", {
      style: `height:26px;flex:0 0 26px;display:flex;align-items:center;gap:10px;padding:0 14px;
              background:var(--hls-bg-panel);border-top:1px solid var(--hls-border);
              color:var(--hls-text-muted);font-size:10.5px`,
    },
      statusEl,
      h("span", { class: "tiny muted" }, "帧预览由本机 ffmpeg 逐帧解码，播放速度受解码速度限制"),
    );
  }

  // ------------------------------------------------------- 生命周期

  return {
    id: "video",
    name: "视频调光",
    subtitle: "关键帧传播 · 逐帧分析 · 处理与导出",
    icon: "film",
    mount(root: HTMLElement): void {
      mounted = true;
      videoId = null;
      info = null;
      sourceName = "";
      index = 0;
      playing = false;
      playToken += 1;
      pickOn = false;
      keyframes = [];
      jobId = null;
      running = false;
      progress = 0;
      stageMsg = "未开始";
      errorMsg = "";
      finished = false;
      perFrameMs = null;
      lastTick = null;
      measuredTotal = 0;
      measuredDone = 0;
      selectedLight = 0;
      ballHandler = () => {};
      ball = new Trackball(0, 0, 1, (dx, dy, dz) => ballHandler(dx, dy, dz));

      const wrap = h("div", {
        style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--hls-bg-root)",
      });
      const center = h("div", {
        style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--hls-bg-root)",
      }, buildStage(), buildTimeline());
      wrap.append(
        buildTopbar(),
        h("div", { style: "flex:1;min-height:0;display:flex" },
          buildLeftPanel(), center, buildRightPanel()),
        buildStatusLine(),
      );
      root.append(wrap);

      syncAccept();
      renderLeft();
      renderTopInfo();
      renderTimeline();
      renderTransport();
      renderLights();
      renderProcess();
      // 若外壳中已有导入的视频（例如从光影工作台自动切过来），直接采用它；
      // 只有在没有现成视频时才提示「等待打开视频」。
      if (store.ctx.videoId) {
        void adoptExisting();
      } else {
        renderOverlay();
        renderStatus();
        store.set({ status: "视频调光 · 等待打开视频" });
      }
    },

    unmount(): void {
      mounted = false;
      playing = false;
      playToken += 1;
      for (const id of timers) window.clearTimeout(id);
      timers.clear();
      view = null;
      ball = null;
      pickBtn = null;
    },
  };
}
