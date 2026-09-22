/**
 * 应用外壳：顶栏 + 左侧导航 + 屏幕路由 + 状态栏。
 *
 * 屏幕契约（各屏幕模块统一实现）：
 *   export function createXxxScreen(): Screen
 *   Screen = { id, name, icon, mount(root), unmount?() }
 *
 * 屏幕挂载时拿到一个空的 root 容器，自行构建全部 DOM；
 * 切换屏幕时先调用旧屏幕的 unmount（清理定时器/轮询），再挂载新屏幕。
 */
import { api, AppState, ModelKind, ModelProgress, RenderParams } from "./api";
import { applyTheme, button, clear, h, icon, loadState, reportError, store, toast } from "./ui";

export interface Screen {
  id: string;
  name: string;
  icon: string;
  /** 设计稿副标题（B8–B18 顶部说明性短语） */
  subtitle?: string;
  /** 挂载到给定容器（容器已清空） */
  mount(root: HTMLElement): void;
  /** 卸载：清理定时器、轮询、事件监听 */
  unmount?(): void;
}

const REGISTRY: Screen[] = [];

export function registerScreen(s: Screen): void {
  REGISTRY.push(s);
}

/** 应用当前渲染参数（供屏幕内部改参数后同步到全局基准） */
export function setParams(p: RenderParams): void {
  store.set({ params: p });
}

export function currentParams(): RenderParams {
  return store.ctx.params;
}

/**
 * 切换界面（由 startApp 在启动时填充）。
 *
 * 屏幕之间需要互相跳转——例如在光影工作台导入视频后应自动切到「视频调光」，
 * 这就是用户要求的「智能识别并更改模式」。选择函数是 startApp 的局部函数，
 * 因此通过这个变量对外暴露。
 */
export let showScreen: (id: string) => void = () => {
  /* 外壳尚未启动：忽略跳转请求 */
};

// ---------------------------------------------------------------- 顶栏

function buildTopbar(): HTMLElement {
  const brand = h("div", { class: "row", style: "gap:9px" },
    h("div", {
      style: `width:28px;height:28px;border-radius:8px;display:flex;align-items:center;
              justify-content:center;background:linear-gradient(135deg,var(--hls-accent-bright),#2251c4)`,
    }, icon("sun", 15, "#fff")),
    h("div", {},
      h("div", { style: "font-size:13.5px;font-weight:600" }, "凌日光影棚"),
      h("div", { style: "font-size:8.5px;letter-spacing:0.9px;color:var(--hls-text-muted)" }, "HORIZON LIGHT STUDIO"),
    ),
  );

  const project = h("div", { class: "chip", id: "topbar-project" }, icon("folder", 12), "未打开媒体");

  const screenCrumb = h("div", { style: "min-width:0;display:flex;flex-direction:column;justify-content:center;max-width:46vw" },
    h("div", { id: "topbar-screen-name", style: "font-size:12px;font-weight:600;color:var(--hls-text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, ""),
    h("div", { id: "topbar-screen-sub", style: "font-size:9px;color:var(--hls-text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, ""));

  const backendChip = h("div", { class: "chip" }, h("i", { class: "dot" }), h("span", { id: "topbar-backend" }, "本地 AI 后端"));

  const themeSelect = h("select", { class: "select", id: "topbar-theme", style: "width:auto" });
  themeSelect.addEventListener("change", () => {
    applyTheme(themeSelect.value).catch(() => toast("主题切换失败", "error"));
  });

  const bar = h("div", { class: "topbar" },
    brand,
    h("div", { style: "width:1px;height:20px;background:var(--hls-border)" }),
    project,
    screenCrumb,
    h("div", { class: "spacer" }),
    backendChip,
    h("div", { class: "row", style: "gap:6px" }, icon("palette", 13, "var(--hls-text-muted)"), themeSelect),
    button("", () => openSettings(), { icon: "settings", variant: "icon", title: "设置" }),
    button("", () => quitApp(), { icon: "power", variant: "icon", title: "退出软件" }),
  );
  return bar;
}

/** 退出软件：先请求后端停止服务，再关闭页面。
 *  打包版没有控制台窗口，没有这个入口用户只能去任务管理器结束进程。 */
function quitApp(): void {
  const mask = h("div", { class: "modal-mask" },
    h("div", { class: "modal", style: "max-width:420px" },
      h("h2", {}, "退出软件？"),
      h("div", { class: "small sec" }, "本地服务将一并停止；未导出的调整会丢失。"),
      h("div", { class: "row", style: "justify-content:flex-end;gap:8px;margin-top:14px" },
        button("取消", () => mask.remove(), { variant: "ghost" }),
        button("退出", async () => {
          try {
            await api.shutdown();
          } catch {
            // 即使接口不可用也要让用户能关掉页面
          }
          document.body.replaceChildren(
            h("div", { style: "padding:40px;font-family:var(--font-ui);color:var(--hls-text-primary)" },
              h("div", { style: "font-size:15px;font-weight:600;margin-bottom:8px" }, "已退出"),
              h("div", { class: "small sec" }, "本地服务已停止，可以直接关闭此页面。")));
          setTimeout(() => window.close(), 200);
        }, { variant: "primary" }),
      ),
    ));
  document.body.append(mask);
}

// ---------------------------------------------------------------- 设置弹窗

/** 设置弹窗里「模型状态」那一行的刷新钩子（弹窗关闭后为空）。 */
let modelStatusSync: (() => void) | null = null;

/** 三种内置模型：深度两个（择一使用，DAV2 优先）、法线一个（可选增强）。 */
const MODEL_CHOICES: { value: ModelKind; label: string; size: string }[] = [
  { value: "dav2", label: "Depth Anything V2 Small（深度，推荐）", size: "约 94MB" },
  { value: "depth", label: "MiDaS-small（深度，体积更小）", size: "约 64MB" },
  { value: "normal", label: "MoGe-2（法线，可选增强）", size: "约 134MB" },
];

/** 从 openSettings 的下载按钮里抽出来的进度条：下载期间可见，结束时清掉 */
function modelProgressBar(): { el: HTMLElement; stop: () => void; start: () => void } {
  const fill = h("i", {});
  const bar = h("div", { class: "bar", style: "display:none;margin-left:22px" }, fill);
  const text = h("div", { class: "small muted", style: "display:none;padding-left:22px" });
  let timer = 0;

  const paint = (p: ModelProgress): void => {
    bar.style.display = "";
    text.style.display = "";
    if (p.total > 0) {
      // total 已知：正常百分比进度
      bar.classList.remove("bar--indet");
      fill.style.width = `${p.percent}%`;
      text.textContent = `已下载 ${(p.done / 1e6).toFixed(1)} / ${(p.total / 1e6).toFixed(1)} MB（${p.percent.toFixed(0)}%）`;
    } else {
      // 服务器没给 Content-Length：只能报已接收字节，不编造百分比
      bar.classList.add("bar--indet");
      fill.style.width = "";
      text.textContent = `已接收 ${(p.done / 1e6).toFixed(1)} MB（对方未提供总大小）`;
    }
  };

  return {
    el: h("div", { style: "display:flex;flex-direction:column;gap:5px" }, bar, text),
    start: (): void => {
      bar.style.display = "";
      text.style.display = "";
      text.textContent = "正在连接下载源…";
      timer = window.setInterval(() => {
        api.modelProgress().then(paint).catch(() => { /* 轮询失败不影响下载本身 */ });
      }, 400);
    },
    stop: (): void => {
      if (timer) { clearInterval(timer); timer = 0; }
      bar.style.display = "none";
      text.style.display = "none";
    },
  };
}

export function openSettings(): void {
  const st: AppState | null = store.ctx.state;
  const s = st?.settings;

  const chk = (id: string, label: string, checked: boolean) => {
    const box = h("input", { type: "checkbox", checked });
    return { box, el: h("label", { class: "row", style: "gap:7px;font-size:12px;cursor:pointer" }, box, label) };
  };

  const builtin = chk("builtin", "启用① 内置本地深度引擎（离线，推荐）", !!s?.builtin_enabled);
  const local = chk("local", "启用② 用户自建服务（HTTP localhost）", !!s?.local_enabled);
  const cloud = chk("cloud", "启用③ 云端 API（需联网）", !!s?.cloud_enabled);
  const previewOnly = chk("preview", "仅预览模式（跳过全部 AI 后端，完全离线）", !!s?.preview_only);

  const localUrl = h("input", { class: "input", value: s?.local_url ?? "http://127.0.0.1:8765" });
  const baseUrl = h("input", { class: "input", value: s?.cloud_base_url ?? "", placeholder: "https://dashscope.aliyuncs.com/compatible-mode/v1" });
  const apiKey = h("input", { class: "input", type: "password", placeholder: s?.cloud_api_key_set ? "已配置（留空表示不修改）" : "未配置" });
  const model = h("input", { class: "input", value: s?.cloud_model ?? "", placeholder: "qwen-vl-max" });
  const timeout = h("input", { class: "input", type: "number", min: 0.5, max: 60, step: 0.5, value: s?.timeout ?? 3 });
  const provider = h("select", { class: "select" },
    ...["openai_compatible", "replicate", "aliyun"].map((p) =>
      h("option", { value: p, selected: (s?.cloud_provider ?? "openai_compatible") === p }, p)));

  // 模型选择框在「下载」和「导入」两处都要读，因此建在这里而不是各自的按钮闭包里
  const kindSel = h("select", { class: "select" },
    ...MODEL_CHOICES.map((c) =>
      h("option", { value: c.value, selected: c.value === "dav2" }, `${c.label} · ${c.size}`)));
  const modelKind = (): ModelKind => kindSel.value as ModelKind;
  const modelSize = (): string =>
    MODEL_CHOICES.find((c) => c.value === modelKind())?.size ?? "";

  const dlProgress = modelProgressBar();

  const modal = h("div", { class: "modal" },
    h("h2", {}, "设置 · AI 后端与缓存"),
    h("div", { class: "small muted", style: "margin-bottom:10px" },
      "四个后端自上而下依次尝试，任一级失败或超时自动降级，界面不会卡住。"),
    h("div", { class: "card", style: "display:flex;flex-direction:column;gap:9px" },
      builtin.el,
      // 这行必须在下载/导入成功后**更新**：openSettings 期间 store 会被
      // loadState() 替换，静态文本会一直停留在打开弹窗时的旧状态
      (() => {
        const lbl = h("div", { class: "small muted", style: "padding-left:22px" });
        const sync = (): void => {
          const cur = store.ctx.state;
          const depth = cur?.model_ready
            ? "深度已就绪"
            : "深度未就绪";
          const normal = cur?.normal_model_ready
            ? "法线已就绪（模型推理）"
            : "法线未装（由深度几何派生）";
          lbl.textContent = `${depth} · ${normal}。` +
            (cur?.model_ready ? "" : "深度未就绪时会自动降级为「仅预览」或云端。");
        };
        sync();
        modelStatusSync = sync;
        return lbl;
      })(),
      h("div", { class: "row", style: "gap:8px;padding-left:22px" },
        (() => {
          const dl = button("一键下载模型", () => { void runDownload(); }, { icon: "download" });
          const runDownload = async (): Promise<void> => {
            dl.disabled = true;
            const old = dl.textContent;
            // 三种模型体积差一倍（64/94/134MB），文案必须跟着所选模型走
            dl.textContent = `下载中…（${modelSize()}）`;
            dlProgress.start();
            try {
              const r = await api.modelDownload(modelKind());
              toast(r.already ? r.message : `模型下载完成（${r.size_mb ?? "?"} MB）`, "ok");
              await loadState();
              refreshChrome();
              modelStatusSync?.();     // 状态行必须跟着更新，否则仍显示「尚未下载」
            } catch (e) {
              reportError(e);   // 含「可用 HLS_*_MODEL_URL 指定镜像」的提示
            } finally {
              dlProgress.stop();
              dl.disabled = false;
              dl.textContent = old;
            }
          };
          return h("div", { class: "row", style: "gap:8px" }, kindSel, dl);
        })(),
        (() => {
          const inp = h("input", {
            type: "file", accept: ".onnx", class: "hidden",
          });
          inp.addEventListener("change", async () => {
            const f = inp.files?.[0];
            if (!f) return;
            // 选择框在 change 时可能已被改动，这里读一次并固定下来
            const kindOfFile = modelKind();
            try {
              const buf = await f.arrayBuffer();
              const r = await api.modelImport(new Uint8Array(buf), kindOfFile);
              toast(`模型已导入（${r.size_mb} MB）`, "ok");
              await loadState();
              refreshChrome();
              modelStatusSync?.();
            } catch (e) { reportError(e); }
            inp.value = "";
          });
          const b = button("离线导入模型…", () => inp.click(), { icon: "upload" });
          b.append(inp);
          return b;
        })(),
      ),
      dlProgress.el,
      h("div", { class: "small muted", style: "padding-left:22px" },
        "软件不捆绑任何模型权重。下载源默认走 hf-mirror 镜像（GitHub / huggingface.co 在国内常不可达），" +
        "可用环境变量 HLS_DEPTH_MODEL_URL / HLS_NORMAL_MODEL_URL / HLS_MODEL_URL 覆盖；" +
        "也可自行下载 .onnx 后「离线导入模型…」——导入会按所选类型实际加载校验，放错类型会被拒绝。"),
      local.el,
      h("div", { class: "field" }, h("label", {}, "自建服务地址"), localUrl),
      h("div", { class: "small muted" }, "部署方式：server/setup_deployment 后运行 server/run_server，详见用户手册第 4 节。"),
      cloud.el,
      h("div", { class: "grid2" },
        h("div", { class: "field" }, h("label", {}, "提供商"), provider),
        h("div", { class: "field" }, h("label", {}, "模型 / Action"), model)),
      h("div", { class: "field" }, h("label", {}, "Base URL"), baseUrl),
      h("div", { class: "field" }, h("label", {}, "API Key"), apiKey),
      h("div", { class: "field" }, h("label", {}, "各级超时（秒）"), timeout),
      h("div", { class: "divider" }),
      previewOnly.el,
    ),
    h("div", { class: "card", style: "margin-top:10px" },
      h("div", { class: "row row--between" },
        h("div", {},
          h("div", { style: "font-weight:600;font-size:12px" }, "AI 分析结果缓存"),
          h("div", { class: "small muted" }, st?.cache_dir ?? "")),
        button("清理缓存", async () => {
          try {
            const r = await api.clearCache();
            toast(`已清理 ${r.removed} 条缓存`, "ok");
          } catch (e) { toast("清理失败", "error"); }
        }, { variant: "danger" }),
      )),
    h("div", { class: "row", style: "justify-content:flex-end;margin-top:14px;gap:8px" },
      button("取消", () => close(), { variant: "ghost" }),
      button("保存", async () => {
        try {
          const payload = {
            builtin_enabled: builtin.box.checked,
            local_enabled: local.box.checked,
            local_url: localUrl.value.trim(),
            cloud_enabled: cloud.box.checked,
            cloud_provider: provider.value,
            cloud_base_url: baseUrl.value.trim(),
            cloud_model: model.value.trim(),
            timeout: Number(timeout.value) || 3,
            preview_only: previewOnly.box.checked,
            ...(apiKey.value.trim() ? { cloud_api_key: apiKey.value.trim() } : {}),
          };
          await api.saveSettings(payload);
          await loadState();
          refreshChrome();
          toast("设置已保存", "ok");
          close();
        } catch (e) { toast("保存失败", "error"); }
      }, { variant: "primary" }),
    ),
  );

  const mask = h("div", { class: "modal-mask" }, modal);
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
  const close = () => {
    modelStatusSync = null;
    dlProgress.stop();   // 关掉弹窗后别再轮询（下载请求本身不受影响）
    mask.remove();
  };
  document.body.append(mask);
}

// ---------------------------------------------------------------- 外壳

/**
 * 顶栏右侧的后端 chip 文案。
 *
 * 三种情形，优先级从高到低：
 *  1. 已经分析过 —— 报告**实际**服务过的那一级后端（云端失败了就别再显示云端）；
 *  2. 还没分析过、预热正在进行 —— 显示「引擎加载中…」。这一条是必须的：
 *     首屏的 model_ready 走 allow_load=False 的快速判断，模型装好了也可能
 *     先报一次「未就绪」，直接照字面显示会冤枉引擎；
 *  3. 还没分析过、预热已有结论 —— 按结论显示就绪/未就绪。
 */
function backendChip(st: AppState | null): { label: string; tone: "ok" | "warn" | "off"; title: string } {
  const names: Record<string, string> = {
    builtin: "内置本地引擎", local: "自建服务", cloud: "云端 API", simulate: "仅预览（灰度模拟）",
  };
  if (store.ctx.backend) {
    return { label: names[store.ctx.backend] ?? store.ctx.backend, tone: "ok", title: "最近一次分析所用的后端" };
  }
  const w = st?.warmup;
  if (w && (w.state === "scheduled" || w.state === "warming")) {
    return { label: "引擎加载中…", tone: "warn", title: "后台正在加载深度/法线模型，首次分析会更快" };
  }
  // 预热结论是精确的，但「内置」这一级还受设置里的开关约束
  const builtinOn = st?.settings?.builtin_enabled !== false;
  if (w?.state === "ready" && builtinOn) {
    return { label: "内置本地引擎 · 就绪", tone: "ok", title: `模型预热耗时 ${w.seconds}s` };
  }
  if (w?.state === "missing" && builtinOn) {
    return { label: "内置本地引擎 · 未就绪", tone: "off", title: w.detail || "深度模型未就绪，可在设置里一键下载" };
  }
  return {
    label: st?.ffmpeg ? "本地 AI 后端 · 就绪" : "本地 AI 后端 · ffmpeg 缺失",
    tone: st?.ffmpeg ? "ok" : "warn",
    title: st?.ffmpeg ? "等待首次分析以确定实际后端" : "ffmpeg 缺失：视频流程不可用（图片流程不受影响）",
  };
}

function refreshChrome(): void {
  const st = store.ctx.state;
  const name = document.getElementById("topbar-project");
  if (name) {
    clear(name);
    const media = store.ctx.mediaName || "未打开媒体";
    name.append(icon(store.ctx.mediaKind === "video" ? "film" : "image", 12, "var(--hls-accent)"));
    name.append(h("span", {}, media));
    if (store.ctx.mediaKind) name.append(h("span", { class: "muted" }, store.ctx.mediaKind === "video" ? "视频" : "图片"));
  }
  const be = document.getElementById("topbar-backend");
  if (be) {
    const { label, tone, title } = backendChip(st);
    be.textContent = label;
    be.parentElement?.setAttribute("title", title);
    const dot = be.parentElement?.querySelector(".dot");
    dot?.classList.toggle("dot--warn", tone === "warn");
    dot?.classList.toggle("dot--off", tone === "off");
  }
  const sel = document.getElementById("topbar-theme") as HTMLSelectElement | null;
  if (sel && store.ctx.themes.length) {
    const want = store.ctx.themeId;
    clear(sel);
    for (const t of store.ctx.themes) {
      sel.append(h("option", { value: t.id, selected: t.id === want }, t.name));
    }
  }
}

/**
 * 预热轮询：启动后的一小段时间内预热状态会变（WARMUP_DELAY 后开始加载），
 * 顶栏要跟着从「引擎加载中…」翻到「就绪」。settle 后立即停止，不做长轮询。
 *
 * 只把**引擎相关字段**写回 store：settings / params 仍以界面当前值为准，
 * 否则用户刚改的设置会被服务端旧值覆盖。
 */
async function pollWarmup(): Promise<void> {
  const pending = new Set(["idle", "scheduled", "warming"]);
  for (let i = 0; i < 40; i++) {
    await new Promise<void>((r) => setTimeout(r, 750));
    let s: AppState;
    try {
      s = await api.state();
    } catch {
      return;                 // 服务停了就别继续打
    }
    const cur = store.ctx.state;
    if (cur) {
      store.set({
        state: { ...cur, warmup: s.warmup, model_ready: s.model_ready,
                 normal_model_ready: s.normal_model_ready },
      });
      modelStatusSync?.();    // 设置弹窗若开着，状态行要跟着变
    }
    if (!s.warmup || !pending.has(s.warmup.state)) return;
  }
}

export async function startApp(): Promise<void> {
  const app = document.getElementById("app");
  if (!app) return;
  app.className = "shell";

  const topbar = buildTopbar();
  const rail = h("div", { class: "rail" });
  const screenRoot = h("div", { class: "screen" });
  const statusbar = h("div", { class: "statusbar" },
    h("span", { id: "status-text" }, "就绪"),
    h("div", { class: "spacer" }),
    h("span", { id: "status-size" }, ""),
    h("span", { id: "status-backend" }, ""),
  );

  app.append(topbar, h("div", { class: "body" }, rail, screenRoot), statusbar);

  // 导航
  let active: Screen | null = null;
  const screens = REGISTRY;
  for (const s of screens) {
    const b = h("button", { class: "nav-btn", type: "button", title: s.subtitle ? `${s.name} · ${s.subtitle}` : s.name },
      icon(s.icon, 17), h("span", {}, s.name));
    b.addEventListener("click", () => select(s.id));
    b.dataset.screen = s.id;
    rail.append(b);
  }
  rail.append(h("div", { class: "spacer" }));
  const help = h("button", { class: "nav-btn", type: "button", title: "帮助" },
    icon("info", 15), h("span", {}, "帮助"));
  help.addEventListener("click", () => {
    toast("操作提示见 docs/用户手册.md；本地服务教程见第 4 节。");
  });
  rail.append(help);

  function select(id: string): void {
    const target = screens.find((s) => s.id === id);
    if (!target || target === active) return;
    active?.unmount?.();
    active = target;
    const sn = document.getElementById("topbar-screen-name");
    if (sn) sn.textContent = target.name;
    const ss = document.getElementById("topbar-screen-sub");
    if (ss) ss.textContent = target.subtitle || "";
    for (const el of rail.querySelectorAll<HTMLElement>(".nav-btn")) {
      el.classList.toggle("on", el.dataset.screen === id);
    }
    clear(screenRoot);
    // 所有屏幕共用同一个 root 元素：必须清掉上一个屏幕留下的**内联样式**
    // （某些屏幕会设置 flexDirection/width 等）。否则内联样式会泄漏到下一个
    // 屏幕，例如逐帧光绘设了 column 之后，插件中心的三栏会被竖着堆叠。
    screenRoot.removeAttribute("style");
    screenRoot.className = "screen";
    target.mount(screenRoot);
    refreshChrome();
  }

  // 把局部 select 接到已导出的 showScreen（供屏幕之间跳转）
  showScreen = select;

  // 状态栏跟随 store
  store.subscribe(() => {
    const t = document.getElementById("status-text");
    if (t) t.textContent = store.ctx.status || "就绪";
    const size = document.getElementById("status-size");
    if (size) {
      const v = store.ctx.videoInfo;
      size.textContent = v
        ? `${v.width} × ${v.height} · ${v.fps} fps · ${v.duration.toFixed(1)}s · ${v.frame_count} 帧`
        : "";
    }
    const be = document.getElementById("status-backend");
    if (be) be.textContent = store.ctx.backend ? `深度来源：${store.ctx.backend}${store.ctx.fromCache ? " · 缓存命中" : ""}` : "";
    refreshChrome();
  });

  try {
    await loadState();
  } catch (e) {
    screenRoot.append(h("div", { style: "padding:24px" },
      h("div", { class: "banner" }, "无法连接本地服务：请通过 `python -m webui.desktop` 启动，而不是直接打开文件。")));
    return;
  }

  if (!screens.length) {
    screenRoot.append(h("div", { style: "padding:24px" }, "界面模块未加载。"));
    return;
  }
  select(screens[0].id);
  refreshChrome();
  void pollWarmup();
}
