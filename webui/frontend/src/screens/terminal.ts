/**
 * B6 暗房终端：直接驱动本地引擎的命令控制台。
 *
 * 诚实规则：每条命令都必须真的做事，或者明确说明「本引擎不支持」。
 * 引擎只计算相对亮度 / Lambert-Phong 光照，没有 DMX、灯具温度、GPU 温度、
 * 磁盘、分布式节点在线状态这些能力 —— 这些命令一律回「本引擎不支持」，
 * 绝不返回伪造的成功信息。
 */
import { api, ApiError, downloadB64 } from "../api";
import type { AppState, Cue, Light, RenderParams } from "../api";
import type { Screen } from "../shell";
import { currentParams, setParams } from "../shell";
import { button, clear, h, icon, store } from "../ui";

type Kind = "in" | "out" | "err" | "ok" | "info";

/** 引擎不支持的能力 → 中文原因（输入这些命令时如实说明） */
const UNSUPPORTED: Record<string, string> = {
  dmx: "DMX512 控台协议（本软件不接控台）",
  artnet: "Art-Net 网络控光",
  sacn: "sACN / E1.31 网络控光",
  gpu: "GPU 显存 / 温度监测（渲染走 CPU/Numpy）",
  显卡: "GPU 显存 / 温度监测",
  温度: "灯具 / 机身温度传感器读数",
  temp: "灯具 / 机身温度传感器读数",
  temperature: "灯具 / 机身温度传感器读数",
  热保护: "灯具热保护状态",
  风扇: "散热风扇转速",
  disk: "磁盘容量 / IO 监测",
  磁盘: "磁盘容量 / IO 监测",
  node: "分布式渲染节点在线状态（本软件为单机单进程）",
  nodes: "分布式渲染节点在线状态",
  节点: "分布式渲染节点在线状态",
  cluster: "集群 / 多机渲染",
  lux: "绝对照度 lx 测量（引擎无光度学校准）",
  照度: "绝对照度 lx 测量",
  cri: "显色指数 CRI",
  duv: "色偏差 Δuv",
  power: "灯具功率 / 能耗",
  功率: "灯具功率 / 能耗",
};

const CMDS: Record<string, { usage: string; desc: string; run: (args: string[]) => void | Promise<void> }> = {};

/** 可由终端修改的光源数值字段及其引擎范围（core/types.py） */
interface LightField { min: number; max: number; get: (l: Light) => number; set: (l: Light, v: number) => void; }
const LIGHT_FIELDS: Record<string, LightField> = {
  intensity: { min: 0, max: 4, get: (l) => l.intensity, set: (l, v) => { l.intensity = v; } },
  kelvin: { min: 1500, max: 12000, get: (l) => l.kelvin, set: (l, v) => { l.kelvin = v; } },
  radius: { min: 0.05, max: 1.5, get: (l) => l.radius, set: (l, v) => { l.radius = v; } },
};
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

// ---------------------------------------------------------------- 屏幕

export function createTerminalScreen(): Screen {
  const history: string[] = [];
  let historyIdx = 0;
  let activePreset: string | null = null;
  let cues: Cue[] = [];
  let busy = false;

  const term = h("div", { class: "term" });
  const input = h("input", {
    type: "text",
    spellcheck: "false",
    autocomplete: "off",
    placeholder: "输入命令，help 查看全部（Tab 补全 · ↑↓ 历史 · Ctrl+K 清屏）",
  });
  const busyChip = h("span", { class: "chip" }, "空闲");
  const nodeList = h("div", { class: "panel__body" });
  const cueList = h("div", { class: "panel__body" });
  const sessionBody = h("div", { class: "panel__body" });

  // -------------------------------------------------------------- 输出

  function stamp(): string {
    const d = new Date();
    const p2 = (n: number): string => String(n).padStart(2, "0");
    return `[${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}]`;
  }

  function write(kind: Kind, text: string): void {
    const color: Record<Kind, string> = {
      in: "var(--hls-text-primary)",
      out: "var(--hls-text-secondary)",
      err: "#f87171",
      ok: "var(--hls-success)",
      info: "var(--hls-text-muted)",
    };
    for (const row of text.split("\n")) {
      term.append(h("div", { class: "term__line" },
        h("span", { class: "muted" }, `${stamp()} `),
        h("span", { style: `color:${color[kind]}` }, row),
      ));
    }
    term.scrollTop = term.scrollHeight;
  }

  const ok = (t: string): void => write("ok", t);
  const out = (t: string): void => write("out", t);
  const info = (t: string): void => write("info", t);
  const fail = (t: string): void => write("err", t);

  function errText(e: unknown): string {
    if (e instanceof ApiError) return e.message;
    return e instanceof Error ? e.message : String(e);
  }

  // -------------------------------------------------------------- 状态读取

  const params = (): RenderParams => currentParams();
  const presetNames = (): string[] => store.ctx.state?.preset_names ?? [];

  function timeLabel(sec?: number): string {
    if (!sec) return "—";
    const d = new Date(sec * 1000);          // 后端 core/cues.py 用 time.time()（秒）
    const p2 = (n: number): string => String(n).padStart(2, "0");
    return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  }

  // -------------------------------------------------------------- 面板刷新

  function renderNodes(): void {
    clear(nodeList);
    const p = params();
    if (!p.lights.length) {
      nodeList.append(h("div", { class: "card small muted" }, "当前参数中没有光源。"));
      return;
    }
    for (let i = 0; i < p.lights.length; i += 1) {
      const l = p.lights[i];
      const pct = Math.round((l.intensity / 4) * 100);
      nodeList.append(h("div", { class: "card" },
        h("div", { class: "row row--between" },
          h("span", { class: "small", style: "font-weight:600" }, `#${i} ${l.name}`),
          h("span", { class: "tiny muted" }, `${pct}%`),
        ),
        h("div", { class: "bar", style: "margin:5px 0 4px" },
          h("i", { style: `width:${pct}%;background:var(--hls-warm)` })),
        h("div", { class: "tiny muted" },
          `${l.kind === "point" ? "点光" : "平行光"} · ${Math.round(l.kelvin)}K · 半径 ${l.radius.toFixed(2)}` +
          (l.visible ? "" : " · 已停用")),
      ));
    }
  }

  function renderCues(): void {
    clear(cueList);
    if (!cues.length) {
      cueList.append(h("div", { class: "card small muted" }, "还没有会话快照。用 scene save <名称> 保存当前参数。"));
      return;
    }
    for (const c of cues) {
      cueList.append(h("div", { class: "card" },
        h("div", { class: "row row--between" },
          h("span", { class: "small ellipsis", style: "font-weight:600" }, c.name),
          h("span", { class: "tiny muted" }, `${c.params.lights.length} 灯`),
        ),
        h("div", { class: "tiny muted", style: "margin-top:3px" },
          `${timeLabel(c.created)} · fade ${c.fade.toFixed(1)}s`),
      ));
    }
  }

  function renderSession(): void {
    clear(sessionBody);
    const p = params();
    const st: AppState | null = store.ctx.state;
    sessionBody.append(h("div", { class: "card" },
      h("div", { class: "row", style: "gap:6px;margin-bottom:4px" },
        icon("info", 13, "var(--hls-text-muted)"),
        h("span", { style: "font-size:11.5px;font-weight:600" }, "会话"),
      ),
      kv("参数项", `${Object.keys(p).length} 项`),
      kv("光源数量", `${p.lights.length}`),
      kv("当前预设", activePreset ?? "未选择"),
      kv("后端", store.ctx.backend || (st?.model_ready ? "内置本地引擎" : "—")),
      kv("版本", st?.version ? `v${st.version}` : "—"),
      kv("缓存目录", st?.cache_dir ?? "—"),
      kv("当前媒体", store.ctx.mediaName || "未打开"),
    ));
  }

  function syncBusy(): void {
    busyChip.textContent = busy ? "运行中" : "空闲";
    busyChip.style.color = busy ? "var(--hls-text-primary)" : "";
    busyChip.style.borderColor = busy ? "var(--hls-accent-line)" : "";
    input.disabled = busy;
  }

  // -------------------------------------------------------------- 命令

  function requireImage(): string | null {
    const id = store.ctx.imageId;
    if (!id) {
      fail("未打开媒体：请先在「光影工作台」打开一张图片，本命令需要 image_id。");
      return null;
    }
    return id;
  }

  function lightByNameOrIndex(tok: string): number {
    const p = params();
    const n = Number(tok);
    if (Number.isInteger(n) && n >= 0 && n < p.lights.length) return n;
    const idx = p.lights.findIndex((l) => l.name === tok);
    return idx;
  }

  CMDS.help = {
    usage: "help",
    desc: "列出全部可用命令",
    run: () => {
      out("可用命令（本终端直接调用本地引擎接口）：");
      for (const [name, c] of Object.entries(CMDS)) {
        out(`  ${c.usage.padEnd(38, " ")} ${c.desc}`.replace(/\s+$/, ""));
      }
      out("不支持的能力：");
      out(`  ${Object.keys(UNSUPPORTED).join(" / ")}`);
      info("以上命令会明确回「本引擎不支持」，不会给出伪造的成功信息。");
    },
  };

  CMDS.state = {
    usage: "state",
    desc: "读取引擎状态（/api/state）",
    run: async () => {
      const st = await api.state();
      const p = params();
      ok(`引擎 v${st.version} · 模型${st.model_ready ? "就绪" : "未就绪"} · ffmpeg ${st.ffmpeg ? "可用" : "缺失"}`);
      out(`缓存目录：${st.cache_dir}`);
      out(`预设（${st.preset_names.length}）：${st.preset_names.join(" / ")}`);
      out(`当前参数：光源 ${p.lights.length} · 环境强度 ${p.ambient_intensity.toFixed(2)} · 环境色温 ${Math.round(p.ambient_kelvin)}K`);
      out(`光照模式 ${p.lighting_mode} · 阴影 ${p.shadow_mode} · 曝光 ${p.exposure.toFixed(2)} · 预览上限 ${p.preview_size}px`);
      out(`视频上限：推荐 ${st.video_limits.recommend_max_side}px / ${st.video_limits.recommend_max_duration}s`);
    },
  };

  CMDS.render = {
    usage: "render",
    desc: "用当前参数渲染当前图片（/api/render）",
    run: async () => {
      const id = requireImage();
      if (!id) return;
      info("渲染中…");
      const r = await api.render(id, params());
      store.set({
        lastRender: r.png_b64,
        backend: r.backend,
        fromCache: r.from_cache,
        status: `终端渲染完成（${r.elapsed.toFixed(2)}s，来源 ${r.backend}）`,
      });
      ok(`渲染完成：${r.width}×${r.height} · 后端 ${r.backend} · 耗时 ${r.elapsed.toFixed(2)}s · ${r.from_cache ? "缓存命中" : "新计算"}`);
    },
  };

  CMDS.preset = {
    usage: "preset list | preset <名称>",
    desc: "列出 / 套用内置预设",
    run: (args) => {
      const sub = (args[0] ?? "list").toLowerCase();
      const st = store.ctx.state;
      if (sub === "list" || !args[0]) {
        out(`内置预设（${presetNames().length}）：${presetNames().join(" / ")}`);
        info("预设来自 /api/state 的 preset_names；套用后当前参数会被替换。");
        return;
      }
      const hit = st?.presets.find((p) => p.name === sub);
      if (!hit) {
        fail(`没有名为「${args[0]}」的预设。`);
        out(`可用：${presetNames().join(" / ")}`);
        return;
      }
      setParams(structuredClone(hit.params));
      activePreset = hit.name;
      ok(`已套用预设「${hit.name}」：光源 ${hit.params.lights.length} 个 · 环境强度 ${hit.params.ambient_intensity.toFixed(2)} · 曝光 ${hit.params.exposure.toFixed(2)}`);
    },
  };

  CMDS.light = {
    usage: "light list | light set <序号|名称> intensity|kelvin|radius <值>",
    desc: "查看 / 修改光源参数",
    run: (args) => {
      const sub = (args[0] ?? "list").toLowerCase();
      if (sub === "list") {
        const p = params();
        if (!p.lights.length) { info("当前参数中没有光源。"); return; }
        for (let i = 0; i < p.lights.length; i += 1) {
          const l = p.lights[i];
          out(`#${i} ${l.name} · ${l.kind === "point" ? "点光" : "平行光"} · 强度 ${l.intensity.toFixed(2)}（${Math.round((l.intensity / 4) * 100)}%）· ${Math.round(l.kelvin)}K · 半径 ${l.radius.toFixed(2)}${l.visible ? "" : " · 已停用"}`);
        }
        return;
      }
      if (sub !== "set") { fail("用法：" + CMDS.light.usage); return; }
      const [, who, field, rawValue] = args;
      if (!who || !field || rawValue === undefined) { fail("用法：" + CMDS.light.usage); return; }
      const idx = lightByNameOrIndex(who);
      if (idx < 0) { fail(`找不到光源「${who}」：可用序号 0~${params().lights.length - 1} 或完整名称。`); return; }
      const value = Number(rawValue);
      if (!Number.isFinite(value)) { fail(`「${rawValue}」不是有效数字。`); return; }
      const next = structuredClone(params());
      const l = next.lights[idx];
      const f = LIGHT_FIELDS[field];
      if (!f) { fail(`不支持的字段「${field}」：可用 ${Object.keys(LIGHT_FIELDS).join(" / ")}。`); return; }
      const before = f.get(l);
      f.set(l, clamp(value, f.min, f.max));
      setParams(next);
      const after = f.get(l);
      ok(`#${idx} ${l.name} 的 ${field}：${before.toFixed(2)} → ${after.toFixed(2)}${after !== value ? `（已夹取到引擎范围 ${f.min}~${f.max}，输入 ${value}）` : ""}`);
      info("参数已写入全局 store，其他屏幕读取的是同一份基准。");
    },
  };

  CMDS.scene = {
    usage: "scene save <名称> | scene list | scene load <名称>",
    desc: "会话快照（CUE）：保存 / 列出 / 载入",
    run: async (args) => {
      const sub = (args[0] ?? "list").toLowerCase();
      const name = args.slice(1).join(" ").trim();
      if (sub === "list") {
        const r = await api.cues();
        cues = r.cues;
        renderCues();
        if (!cues.length) { info("会话里还没有快照。"); return; }
        out(`会话快照（${cues.length}）：`);
        for (const c of cues) out(`  ${c.name} · ${timeLabel(c.created)} · fade ${c.fade.toFixed(1)}s · ${c.params.lights.length} 灯`);
        return;
      }
      if (sub === "save") {
        if (!name) { fail("用法：scene save <名称>"); return; }
        const r = await api.cueSave(name, params(), 2.0);
        activePreset = null;
        await reloadCues();
        ok(`已保存快照「${r.cue.name}」（id ${r.cue.id.slice(0, 8)}… · fade ${r.cue.fade.toFixed(1)}s · ${r.cue.params.lights.length} 灯）`);
        return;
      }
      if (sub === "load") {
        if (!name) { fail("用法：scene load <名称>"); return; }
        const r = await api.cues();
        cues = r.cues;
        renderCues();
        const hit = cues.find((c) => c.name === name);
        if (!hit) { fail(`没有名为「${name}」的快照。`); out(`可用：${cues.map((c) => c.name).join(" / ") || "（空）"}`); return; }
        const res = await api.cueInterpolate(hit.id, params(), 1);
        setParams(res.params);
        activePreset = null;
        ok(`已载入快照「${hit.name}」（t=1 完全切换）：光源 ${res.params.lights.length} 个 · 环境强度 ${res.params.ambient_intensity.toFixed(2)} · 环境色温 ${Math.round(res.params.ambient_kelvin)}K`);
        return;
      }
      fail("用法：" + CMDS.scene.usage);
    },
  };

  CMDS.snap = {
    usage: "snap save <标记>",
    desc: "快速保存会话快照（scene save 的别名，fade 0）",
    run: async (args) => {
      if ((args[0] ?? "").toLowerCase() !== "save" || !args.slice(1).join(" ").trim()) {
        fail("用法：snap save <标记>");
        return;
      }
      const tag = args.slice(1).join(" ").trim();
      const r = await api.cueSave(tag, params(), 0);
      await reloadCues();
      ok(`快照已保存「${r.cue.name}」· fade ${r.cue.fade.toFixed(1)}s（硬切）· ${r.cue.params.lights.length} 灯`);
    },
  };

  CMDS.export = {
    usage: "export png | export jpeg",
    desc: "导出当前图片的渲染结果并下载",
    run: async (args) => {
      const fmt = (args[0] ?? "png").toLowerCase();
      if (fmt !== "png" && fmt !== "jpeg") { fail("用法：export png | export jpeg"); return; }
      const id = requireImage();
      if (!id) return;
      info(`导出 ${fmt.toUpperCase()} 中…`);
      const r = await api.exportImage(id, params(), fmt);
      downloadB64(r.png_b64, r.filename, fmt === "png" ? "image/png" : "image/jpeg");
      ok(`已导出并触发下载：${r.filename}（${fmt.toUpperCase()}）`);
    },
  };

  CMDS.cache = {
    usage: "cache clear",
    desc: "清理 AI 分析结果缓存（/api/cache/clear）",
    run: async (args) => {
      if ((args[0] ?? "").toLowerCase() !== "clear") { fail("用法：cache clear"); return; }
      const r = await api.clearCache();
      ok(`缓存已清理：移除 ${r.removed} 条。`);
    },
  };

  CMDS.plugins = {
    usage: "plugins",
    desc: "列出已安装插件及其启用状态",
    run: async () => {
      const st = await api.plugins();
      if (!st.plugins.length) { info("没有已安装的插件。"); return; }
      out(`插件（${st.plugins.length}）：`);
      for (const p of st.plugins) {
        const on = st.enabled.includes(p.id) || p.enabled === true;
        out(`  ${on ? "●" : "○"} ${p.id} v${p.version} · ${p.kind} · ${p.name}${on ? " · 已启用" : " · 已停用"}`);
      }
      info(`可用类型：${st.kinds.join(" / ") || "（无）"}`);
    },
  };

  CMDS.clear = {
    usage: "clear",
    desc: "清屏（等同 Ctrl+K）",
    run: () => { clear(term); },
  };

  // -------------------------------------------------------------- 执行

  async function reloadCues(): Promise<void> {
    try {
      const r = await api.cues();
      cues = r.cues;
    } catch {
      cues = [];
    }
    renderCues();
  }

  async function execute(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text) return;
    write("in", `hlst@studio:~$ ${text}`);
    history.push(text);
    historyIdx = history.length;
    const toks = text.split(/\s+/);
    const name = toks[0].toLowerCase();
    const cmd = CMDS[name];
    if (!cmd) {
      const reason = UNSUPPORTED[name]
        ?? Object.entries(UNSUPPORTED).find(([k]) => k.length >= 2 && name.startsWith(k))?.[1];
      if (reason) fail(`本引擎不支持「${name}」：${reason}。引擎只做相对亮度 / Lambert-Phong 光照计算。`);
      else fail(`未知命令「${name}」。`);
      info(`输入 help 查看可用命令。已知不支持：${Object.keys(UNSUPPORTED).join(" / ")}`);
      return;
    }
    busy = true;
    syncBusy();
    try {
      await cmd.run(toks.slice(1));
    } catch (e) {
      fail(`执行失败：${errText(e)}`);
    } finally {
      busy = false;
      syncBusy();
    }
  }

  // -------------------------------------------------------------- 补全

  const SUBS: Record<string, string[]> = {
    light: ["list", "set", "intensity", "kelvin", "radius"],
    scene: ["save", "list", "load"],
    snap: ["save"],
    preset: ["list"],
    export: ["png", "jpeg"],
    cache: ["clear"],
  };

  function commonPrefix(xs: string[]): string {
    let p = xs[0] ?? "";
    for (const x of xs) {
      let i = 0;
      while (i < p.length && i < x.length && p[i] === x[i]) i += 1;
      p = p.slice(0, i);
    }
    return p;
  }

  function complete(): void {
    const raw = input.value;
    const endsWithSpace = /\s$/.test(raw);
    const toks = raw.split(/\s+/).filter(Boolean);
    const head = (toks[0] ?? "").toLowerCase();
    let current: string;
    let cands: string[];
    if (toks.length <= 1 && !endsWithSpace) {
      current = toks[0] ?? "";
      cands = [...Object.keys(CMDS), ...Object.keys(UNSUPPORTED)].filter((c) => c.startsWith(current));
    } else {
      current = endsWithSpace ? "" : toks[toks.length - 1];
      const pool = head === "preset" ? [...(SUBS.preset ?? []), ...presetNames()] : SUBS[head] ?? [];
      cands = pool.filter((c) => c.startsWith(current));
    }
    if (!cands.length) return;
    const base = endsWithSpace ? toks : toks.slice(0, -1);
    if (cands.length === 1) {
      input.value = `${[...base, cands[0]].join(" ")} `;
      return;
    }
    write("info", cands.join("  "));
    const common = commonPrefix(cands);
    if (common.length > current.length) input.value = [...base, common].join(" ");
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      if (busy) return;
      const v = input.value;
      input.value = "";
      void execute(v);
      return;
    }
    if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      clear(term);
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      complete();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!history.length) return;
      historyIdx = Math.max(0, historyIdx - 1);
      input.value = history[historyIdx] ?? "";
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      historyIdx = Math.min(history.length, historyIdx + 1);
      input.value = history[historyIdx] ?? "";
    }
  });

  const shellRow = h("div", { class: "term__in" },
    h("span", {}, "hlst@studio:~$"),
    input,
  );
  let unsub: (() => void) | null = null;

  shellRow.addEventListener("click", () => input.focus());

  return {
    id: "terminal",
    name: "暗房终端",
    icon: "terminal",
    mount(root: HTMLElement): void {
      const inner = h("div", { style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column" });
      const head = h("div", {
        style: "flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--hls-border);background:var(--hls-bg-panel)",
      },
        icon("terminal", 15, "var(--hls-accent)"),
        h("div", {},
          h("div", { style: "font-size:13px;font-weight:600" }, "暗房终端"),
          h("div", { class: "tiny muted" }, "DARKROOM CONSOLE · 直接驱动本地引擎"),
        ),
        h("span", { class: "spacer" }),
        h("span", { class: "chip" }, "Tab 补全 · ↑↓ 历史 · Ctrl+K 清屏"),
        busyChip,
      );
      const body = h("div", { style: "flex:1;min-height:0;display:flex" },
        h("div", { style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:#05070c" },
          term,
          shellRow,
        ),
        h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
          h("div", { class: "panel__head" }, "光源节点 · LIGHT NODES"),
          nodeList,
          h("div", { class: "panel__head" },
            "会话快照 · CUE",
            h("span", { class: "spacer" }),
            button("刷新", () => { void reloadCues(); }, { icon: "rotate", variant: "ghost" }),
          ),
          cueList,
          h("div", { class: "panel__head" }, "会话 · SESSION"),
          sessionBody,
        ),
      );
      inner.append(head, body);
      root.append(inner);
      clear(term);
      write("info", "凌日光影棚 · 暗房终端 —— 命令直接调用本地引擎接口，不支持的硬件能力会如实拒绝。");
      write("info", "输入 help 查看命令；Tab 补全；↑↓ 翻历史；Ctrl+K 清屏。");
      renderNodes();
      renderSession();
      syncBusy();
      void reloadCues();
      // 每次挂载重新订阅：参数被其他屏幕或本终端改动时，右侧面板同步刷新
      unsub?.();
      unsub = store.subscribe(() => {
        renderNodes();
        renderSession();
      });
      input.focus();
    },
    unmount(): void {
      unsub?.();
      unsub = null;
      historyIdx = history.length;
    },
  };
}

function kv(label: string, value: string): HTMLElement {
  return h("div", { class: "kv" }, h("span", {}, label), h("b", {}, value));
}
