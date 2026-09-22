/**
 * 屏幕 A5 · 插件中心（纯数据扩展）
 *
 * 后端插件系统（core/plugins.py）只做两件事：解析 JSON 清单、把清单里的数据
 * 收敛成合法取值范围。它**从不执行任何插件代码**，所以「权限」一栏必须如实
 * 写出这一属性，而不是罗列设计稿里的演示文案。
 *
 * 设计稿的演示数据（插件评分、安装量、体积、更新记录、调用曲线）在本机无法
 * 测得，一律不展示——本屏只渲染 /api/plugins 真正返回的字段。
 */
import { api, PluginInfo, PluginState } from "../api";
import type { Screen } from "../shell";
import { button, clear, h, icon, reportError, toast } from "../ui";

/*
 * 分类映射：后端 PLUGIN_KINDS = preset / theme / light-rig / backend。
 *   preset    → 光照工具（贡献一份 RenderParams 预设）
 *   theme     → 主题外观（贡献一组 #RRGGBB 设计令牌）
 *   light-rig → 灯组（贡献一组光源，可一键套用）
 *   backend   → 后端扩展（声明式标签，当前版本不加载任何代码）
 * 设计稿里的「图像处理 / 视频特效 / 脚本扩展」在后端没有对应 kind，
 * 与其展示三个永远为 0 的假分类，不如按真实 kind 渲染分类。
 */
const KIND_LABEL: Record<string, string> = {
  preset: "光照工具",
  theme: "主题外观",
  "light-rig": "灯组",
  backend: "后端扩展",
};

const KIND_ICON: Record<string, string> = {
  preset: "lightbulb",
  theme: "palette",
  "light-rig": "sliders",
  backend: "terminal",
};

const KIND_HINT: Record<string, string> = {
  preset: "贡献一份渲染预设（RenderParams），安装后出现在预设下拉框中",
  theme: "贡献一组设计令牌，颜色值必须是 #RRGGBB",
  "light-rig": "贡献一组光源，安装后可一键套用",
  backend: "预留的声明式标签，当前版本不会加载任何代码",
};

/** 清单必填字段（与 webui/api.py 的校验保持一致） */
const REQUIRED_FIELDS = ["id", "name", "version", "kind"] as const;

/** 仓库内的示例清单：相对应用根目录取，仅在“从仓库直接运行”时可用 */
const REPO_EXAMPLES = [
  "com.example.amber-theme.json",
  "com.example.velvet-night.json",
  "com.example.warm-rig.json",
];

interface RepoExample {
  file: string;
  name: string;
  hint: string;
  manifest: Record<string, unknown>;
}

/** 本地搜索图标（ui.ts 的图标集中没有放大镜，这里内联一个，避免改动公共模块） */
function searchGlyph(size = 13): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  for (const d of ["M17 11a6 6 0 1 1-12 0 6 6 0 0 1 12 0Z", "m21 21-4.3-4.3"]) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

const kindLabel = (kind: string): string => KIND_LABEL[kind] ?? kind;
const kindIcon = (kind: string): string => KIND_ICON[kind] ?? "puzzle";
const kindHint = (kind: string): string => KIND_HINT[kind] ?? "未知类型：后端只会保存清单，不会加载任何代码";

/** 校验一份清单；缺 id/name/version/kind 时返回中文错误 */
function validateManifest(raw: unknown): { ok: true; manifest: Record<string, unknown> } | { ok: false; error: string } {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, error: "清单必须是一个 JSON 对象" };
  }
  const obj = raw as Record<string, unknown>;
  const missing: string[] = [];
  for (const key of REQUIRED_FIELDS) {
    const value = obj[key];
    if (typeof value !== "string" || value.trim() === "") missing.push(key);
  }
  if (missing.length) {
    return { ok: false, error: `清单缺少必填字段：${missing.join("、")}（需要 id / name / version / kind）` };
  }
  return { ok: true, manifest: obj };
}

export function createPluginsScreen(): Screen {
  let panelRoot: HTMLElement | null = null;
  let categoryBox: HTMLElement | null = null;
  let countsBox: HTMLElement | null = null;
  let installBox: HTMLElement | null = null;
  let gridBox: HTMLElement | null = null;
  let detailBox: HTMLElement | null = null;

  let state: PluginState = { plugins: [], enabled: [], kinds: [] };
  let filter = "all";
  let query = "";
  let selectedId = "";
  let examples: RepoExample[] = [];
  let probed = false;

  const isEnabled = (p: PluginInfo): boolean => p.enabled ?? state.enabled.includes(p.id);

  // ---------------------------------------------------------------- 数据

  async function refresh(): Promise<void> {
    try {
      state = await api.plugins();
    } catch (e) {
      reportError(e);
      state = { plugins: [], enabled: [], kinds: [] };
    }
    if (!state.plugins.some((p) => p.id === selectedId)) selectedId = state.plugins[0]?.id ?? "";
    rerender();
  }

  async function setEnabled(p: PluginInfo, enabled: boolean): Promise<void> {
    try {
      const res = await api.pluginToggle(p.id, enabled);
      state = res.state;
      toast(enabled ? `已启用 ${p.name}` : `已停用 ${p.name}`, "ok");
      rerender();
    } catch (e) {
      reportError(e);
    }
  }

  async function removePlugin(p: PluginInfo): Promise<void> {
    if (!window.confirm(`确定移除插件「${p.name}」吗？移除后其贡献（预设 / 灯组 / 令牌）会立即失效。`)) return;
    try {
      await api.pluginRemove(p.id);
      toast(`已移除 ${p.name}`, "ok");
      await refresh();
    } catch (e) {
      reportError(e);
    }
  }

  async function installManifest(manifest: Record<string, unknown>): Promise<void> {
    try {
      const res = await api.pluginInstall(manifest);
      state = res.state;
      selectedId = res.plugin.id;
      toast(`已安装 ${res.plugin.name}（v${res.plugin.version}）`, "ok");
      rerender();
    } catch (e) {
      reportError(e);
    }
  }

  async function installFromFile(file: File): Promise<void> {
    let raw: unknown;
    try {
      raw = JSON.parse(await file.text());
    } catch {
      toast("清单不是合法 JSON，请检查文件内容（编码应为 UTF-8）", "error");
      return;
    }
    const checked = validateManifest(raw);
    if (!checked.ok) {
      toast(checked.error, "error");
      return;
    }
    const kind = String(checked.manifest.kind);
    if (state.kinds.length && !state.kinds.includes(kind)) {
      toast(`未知的插件类型：${kind}（可选：${state.kinds.join(" / ")}）`, "error");
      return;
    }
    await installManifest(checked.manifest);
  }

  /**
   * 探测仓库内示例清单。
   * 只有从仓库目录运行（plugins/examples 可被静态服务命中）时才拿得到；
   * 命中失败或返回的不是 JSON 时保持隐藏，不伪造“可安装示例”。
   */
  async function probeExamples(): Promise<void> {
    const loaded: RepoExample[] = [];
    for (const file of REPO_EXAMPLES) {
      try {
        const res = await fetch(`../../plugins/examples/${file}`);
        if (!res.ok) continue;
        const checked = validateManifest(JSON.parse(await res.text()) as unknown);
        if (!checked.ok) continue;
        loaded.push({
          file,
          name: String(checked.manifest.name),
          hint: kindHint(String(checked.manifest.kind)),
          manifest: checked.manifest,
        });
      } catch {
        /* 单条示例不可用则跳过（SPA 回落到 index.html 时 JSON.parse 会抛错） */
      }
    }
    if (!panelRoot) return;
    examples = loaded;
    probed = true;
    renderInstall();
  }

  // ---------------------------------------------------------------- 渲染

  function rerender(): void {
    renderCategories();
    renderCounts();
    renderGrid();
    renderDetail();
  }

  function countKind(kind: string): number {
    return state.plugins.filter((p) => p.kind === kind).length;
  }

  function visiblePlugins(): PluginInfo[] {
    const q = query.trim().toLowerCase();
    return state.plugins.filter((p) => {
      if (filter !== "all" && p.kind !== filter) return false;
      if (!q) return true;
      return [p.name, p.author ?? "", p.id, p.description ?? ""]
        .some((text) => text.toLowerCase().includes(q));
    });
  }

  function filterRow(id: string, label: string, count: number): HTMLElement {
    const on = filter === id;
    const row = h("div", { class: on ? "item on" : "item", style: "cursor:pointer" },
      h("span", { class: "item__t" }, label),
      h("span", { class: "spacer" }),
      h("span", { class: "tiny mono muted" }, String(count)),
    );
    row.addEventListener("click", () => {
      filter = id;
      renderCategories();
      renderGrid();
    });
    return row;
  }

  function renderCategories(): void {
    const box = categoryBox;
    if (!box) return;
    clear(box);
    const kinds = state.kinds.length ? state.kinds : Object.keys(KIND_LABEL);
    box.append(filterRow("all", "全部", state.plugins.length));
    for (const kind of kinds) box.append(filterRow(kind, kindLabel(kind), countKind(kind)));
  }

  function renderCounts(): void {
    const box = countsBox;
    if (!box) return;
    clear(box);
    const total = state.plugins.length;
    const on = state.plugins.filter(isEnabled).length;
    box.append(
      h("div", { class: "row row--between small" },
        h("span", { class: "sec" }, "插件数"),
        h("b", {}, `${total} 个`)),
      h("div", { class: "row row--between small" },
        h("span", { class: "sec" }, "已启用 / 已停用"),
        h("b", {}, `${on} / ${total - on}`)),
      h("div", { class: "divider" }),
    );
    for (const kind of (state.kinds.length ? state.kinds : Object.keys(KIND_LABEL))) {
      box.append(h("div", { class: "row row--between small" },
        h("span", { class: "sec" }, kindLabel(kind)),
        h("b", { class: "mono" }, String(countKind(kind)))));
    }
    if (total === 0) {
      box.append(h("div", { class: "small muted", style: "margin-top:4px" }, "尚未安装任何插件。"));
    }
  }

  function act(
    label: string,
    onClick: () => void,
    opts?: { icon?: string; variant?: "primary" | "ghost" | "danger" | "icon"; title?: string },
  ): HTMLButtonElement {
    const b = button(label, onClick, opts);
    // 卡片本身可点（选中），操作按钮不冒泡
    b.addEventListener("click", (e) => e.stopPropagation());
    return b;
  }

  function pluginCard(p: PluginInfo): HTMLElement {
    const on = isEnabled(p);
    const isSelected = p.id === selectedId;
    const card = h("div", {
      class: "card",
      style: `display:flex;flex-direction:column;gap:8px;cursor:pointer;
              background:${isSelected ? "var(--hls-accent-tint)" : "var(--hls-bg-elevated)"};
              border-color:${isSelected ? "var(--hls-accent-line)" : "var(--hls-border)"}`,
    });
    card.addEventListener("click", () => {
      selectedId = p.id;
      rerender();
    });

    const badge = h("div", {
      style: `width:34px;height:34px;flex:0 0 34px;border-radius:9px;display:flex;align-items:center;
              justify-content:center;background:var(--hls-bg-canvas);border:1px solid var(--hls-border);
              color:${on ? "var(--hls-accent-bright)" : "var(--hls-text-muted)"}`,
    }, icon(kindIcon(p.kind), 17));

    card.append(
      h("div", { class: "row", style: "gap:9px" },
        badge,
        h("div", { class: "grow" },
          h("div", { class: "ellipsis", style: "font-weight:600;font-size:12.5px" }, p.name),
          h("div", { class: "tiny muted ellipsis" }, `${kindLabel(p.kind)} · v${p.version}`)),
        h("span", {
          class: "chip",
          style: on ? "color:var(--hls-success)" : "color:var(--hls-text-muted)",
        }, h("i", { class: on ? "dot" : "dot dot--off" }), on ? "已安装" : "已停用"),
      ),
      h("div", { class: "small sec", style: "line-height:1.5;min-height:32px" },
        p.description || "（该插件未提供说明）"),
      h("div", { class: "row row--between" },
        h("span", { class: "tiny muted ellipsis" }, p.author || "未署名"),
        h("span", { class: "tiny muted mono ellipsis" }, p.id)),
      h("div", { class: "row", style: "gap:6px" },
        act(on ? "停用" : "启用", () => { void setEnabled(p, !on); }, { icon: "power", variant: on ? "ghost" : "primary" }),
        act("移除", () => { void removePlugin(p); }, { icon: "trash", variant: "danger" }),
        h("span", { class: "spacer" }),
        act("详情", () => { selectedId = p.id; rerender(); }, { variant: "ghost" }),
      ),
    );
    return card;
  }

  function emptyState(title: string, lines: string[]): HTMLElement {
    return h("div", { class: "card", style: "display:flex;flex-direction:column;gap:7px" },
      h("div", { class: "row", style: "gap:7px;font-weight:600" }, icon("info", 14, "var(--hls-accent)"), title),
      ...lines.map((line) => h("div", { class: "small sec", style: "line-height:1.7" }, line)),
    );
  }

  function renderGrid(): void {
    const box = gridBox;
    if (!box) return;
    clear(box);

    if (state.plugins.length === 0) {
      box.append(emptyState("尚未安装任何插件", [
        "插件是可选的本地扩展，软件不捆绑任何第三方插件；插件目录默认位于用户数据目录下的 plugins/。",
        "① 点击上方「安装插件」，选择一份 .json 清单；② 若从仓库目录运行，可用「从仓库示例安装」载入 plugins/examples 下的示例。",
        "插件清单是纯数据：后端只解析 JSON，不执行任何插件代码。",
      ]));
      return;
    }

    const list = visiblePlugins();
    box.append(h("div", { class: "row", style: "gap:8px" },
      h("span", { style: "font-weight:600" }, "扩展插件"),
      h("span", { class: "chip" }, `共 ${state.plugins.length} 个`),
      h("span", { class: "spacer" }),
      h("span", { class: "tiny muted" }, `当前显示 ${list.length} 个`),
    ));

    if (list.length === 0) {
      box.append(emptyState("没有匹配的插件", ["换个关键词，或把分类切回「全部」。"]));
      return;
    }

    const grid = h("div", {
      style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(238px,1fr));gap:10px",
    });
    for (const p of list) grid.append(pluginCard(p));
    box.append(grid);
  }

  function renderInstall(): void {
    const box = installBox;
    if (!box) return;
    clear(box);

    const fileInput = h("input", { type: "file", accept: ".json,application/json", style: "display:none" });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      if (file) void installFromFile(file);
      fileInput.value = "";
    });

    const row = h("div", { class: "row", style: "gap:8px" },
      button("选择 .json 清单", () => fileInput.click(), { icon: "upload", variant: "primary" }),
      fileInput,
    );

    if (examples.length) {
      row.append(h("span", { class: "tiny muted" }, "或"));
      for (const ex of examples) {
        row.append(button(`示例 · ${ex.name}`, () => { void installManifest(ex.manifest); }, {
          icon: "download",
          variant: "ghost",
          title: `${ex.file} —— ${ex.hint}`,
        }));
      }
    }

    box.append(
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
        h("div", { class: "row", style: "gap:7px" },
          icon("upload", 14, "var(--hls-accent)"),
          h("span", { style: "font-weight:600;font-size:12.5px" }, "安装插件"),
          h("span", { class: "spacer" }),
          h("span", { class: "tiny muted" }, "本地 .json 数据清单"),
        ),
        row,
        h("div", { class: "tiny muted", style: "line-height:1.7" },
          "选择文件后先在浏览器本地解析并校验 id / name / version / kind，通过后再提交给本地服务。清单只作为数据保存，不会被执行。",
          examples.length
            ? ""
            : probed
              ? "（「从仓库示例安装」只在从仓库目录直接运行、且 plugins/examples 能被静态服务命中时出现；当前未探测到示例，因此不显示该入口。）"
              : "正在探测仓库内示例清单…"),
      ),
    );
  }

  function detailRow(label: string, value: string | HTMLElement, mono = false): HTMLElement {
    return h("div", { class: "kv" },
      h("span", {}, label),
      h("b", { class: mono ? "mono ellipsis" : "ellipsis" }, value));
  }

  function toggleRow(
    title: string,
    note: string,
    on: boolean,
    onToggle: (next: boolean) => void,
  ): HTMLElement {
    return h("div", { class: "row row--between", style: "gap:10px" },
      h("div", { class: "grow" },
        h("div", { style: "font-size:12px" }, title),
        h("div", { class: "tiny muted", style: "line-height:1.5" }, note)),
      button(on ? "开" : "关", () => onToggle(!on), { on, title: on ? "点击关闭" : "点击开启" }),
    );
  }

  function renderDetail(): void {
    const box = detailBox;
    if (!box) return;
    clear(box);

    const p = state.plugins.find((x) => x.id === selectedId);
    if (!p) {
      box.append(h("div", { class: "small muted", style: "line-height:1.7" },
        state.plugins.length ? "点击任意插件卡片查看详情。" : "尚未安装任何插件。"));
      return;
    }

    const on = isEnabled(p);
    box.append(
      h("div", { class: "row", style: "gap:9px" },
        h("div", {
          style: `width:34px;height:34px;flex:0 0 34px;border-radius:9px;display:flex;align-items:center;
                  justify-content:center;background:var(--hls-bg-canvas);border:1px solid var(--hls-border);
                  color:var(--hls-accent-bright)`,
        }, icon(kindIcon(p.kind), 17)),
        h("div", { class: "grow" },
          h("div", { style: "font-weight:600;font-size:13px" }, p.name),
          h("div", { class: "tiny muted" }, `${p.author || "未署名"} · v${p.version}`)),
      ),
      h("div", { class: "row", style: "gap:6px" },
        h("span", { class: "chip", style: on ? "color:var(--hls-success)" : "" },
          h("i", { class: on ? "dot" : "dot dot--off" }), on ? "已安装并启用" : "已停用"),
        h("span", { class: "chip" }, kindLabel(p.kind)),
      ),
      h("div", { class: "card" },
        detailRow("类型", `${kindLabel(p.kind)}（${p.kind}）`),
        detailRow("版本", `v${p.version}`, true),
        detailRow("作者", p.author || "未署名"),
        detailRow("标识", p.id, true)),
      h("div", { class: "panel__head", style: "padding:0 0 2px" }, "说明"),
      h("div", { class: "card small sec", style: "line-height:1.7" },
        p.description || "（该插件未提供说明）",
        h("div", { class: "tiny muted", style: "margin-top:6px" }, kindHint(p.kind))),
      h("div", { class: "panel__head", style: "padding:0 0 2px" }, "权限"),
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:6px" },
        h("div", { class: "row", style: "gap:7px;align-items:flex-start" },
          icon("check", 14, "var(--hls-success)"),
          h("div", { style: "font-size:12px;font-weight:600;line-height:1.6" },
            "这是纯数据插件：只解析 JSON 清单，不执行任何插件代码")),
        h("div", { class: "tiny muted", style: "line-height:1.7" },
          "安装只是把清单写入插件目录；未知字段会被丢弃，越界数值会被收敛到引擎取值范围，非法颜色不会被采纳。"),
      ),
      h("div", { class: "panel__head", style: "padding:0 0 2px" }, "运行行为"),
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:12px" },
        toggleRow(
          "已启用",
          "停用后该插件的贡献（预设 / 灯组 / 令牌）不再出现在界面上，清单文件保留。",
          on,
          (next) => { void setEnabled(p, next); },
        ),
        h("div", { class: "divider" }),
        toggleRow(
          "随项目自动加载",
          "后端只保存一个启用标记（state.json），本项与「已启用」是同一状态的两种表述。",
          on,
          (next) => { void setEnabled(p, next); },
        ),
      ),
      button(on ? "停用插件" : "启用插件", () => { void setEnabled(p, !on); }, {
        icon: "power",
        variant: on ? "danger" : "primary",
      }),
      h("div", { class: "small muted", style: "line-height:1.7" },
        kindLabel(p.kind) === p.kind ? "该插件类型不在当前服务声明的类型表中。" : `类型说明：${kindHint(p.kind)}`),
      button("查看插件开发说明", showDevDocs, { icon: "workflow", variant: "ghost" }),
    );
  }

  /** 清单格式说明（内容与 plugins/examples/*.json 的字段一一对应） */
  function showDevDocs(): void {
    const manifestSample = `{
  "id": "com.example.my-plugin",   // 必填，唯一标识
  "name": "我的插件",                // 必填，显示名称
  "version": "1.0.0",               // 必填，版本号
  "kind": "preset",                 // 必填，见下方四种类型
  "description": "可选，一句话说明",
  "author": "可选，作者署名",
  "data": { }                       // 该插件贡献的数据
}`;
    const modal = h("div", { class: "modal", style: "max-width:640px" },
      h("h2", {}, "插件开发说明 · 纯数据清单"),
      h("div", { class: "small sec", style: "line-height:1.8" },
        "一个插件就是一个 .json 文件。后端只解析它、收敛它，绝不执行其中的任何内容——不需要写代码，也没有脚本入口。"),
      h("pre", {
        class: "mono small",
        style: `margin:10px 0;padding:10px;border-radius:var(--r-sm);background:var(--hls-bg-canvas);
                border:1px solid var(--hls-border);color:var(--hls-text-secondary);overflow-x:auto;line-height:1.7`,
      }, manifestSample),
      h("div", { class: "card small", style: "line-height:1.8" },
        h("div", { style: "font-weight:600;margin-bottom:4px" }, "data 随 kind 变化"),
        h("div", {}, "preset —— 一份 RenderParams：ambient_intensity(0~2) / ambient_kelvin(1500~12000) / shadow_mode(hard|soft) / lighting_mode(linear|hq) / preview_size(64~4096) / lighting_size(64~4096) / shadow_strength(0~1) / specular_strength(0~1) / tone_preserve(0~1) / exposure(0.2~2.5) / reference_offset(对象或 null)，另可带 lights 数组。"),
        h("div", { style: "margin-top:6px" }, "theme —— data.tokens 为「令牌名 → #RRGGBB」映射，键名与内置主题一致：bg-root / bg-panel / bg-elevated / bg-canvas / border / border-strong / text-primary / text-secondary / text-muted / accent / accent-bright / accent-tint / accent-line / success / warm / violet。"),
        h("div", { style: "margin-top:6px" }, "light-rig —— data.lights 为光源数组，每项字段：name / kind(directional|point) / dx,dy,dz / px,py,pz / intensity(0~4) / kelvin(1500~12000) / radius(0.05~1.5) / visible。"),
        h("div", { style: "margin-top:6px" }, "backend —— 预留的声明式标签，当前版本不加载任何代码。"),
      ),
      h("div", { class: "small muted", style: "margin-top:10px;line-height:1.8" },
        "仓库内的示例见 plugins/examples/：com.example.amber-theme.json（主题）、com.example.velvet-night.json（预设）、com.example.warm-rig.json（灯组）。"),
      h("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" },
        button("关闭", () => mask.remove(), { variant: "ghost" })),
    );
    const mask = h("div", { class: "modal-mask" }, modal);
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
    document.body.append(mask);
  }

  // ---------------------------------------------------------------- 组装

  function mount(root: HTMLElement): void {
    panelRoot = root;

    const search = h("input", { class: "input", placeholder: "搜索插件、作者、标识…" });
    search.addEventListener("input", () => {
      query = search.value;
      renderGrid();
    });

    categoryBox = h("div", { class: "list" });
    countsBox = h("div", { style: "display:flex;flex-direction:column;gap:3px" });
    installBox = h("div", { style: "display:flex;flex-direction:column;gap:10px" });
    gridBox = h("div", { style: "display:flex;flex-direction:column;gap:10px" });
    detailBox = h("div", { class: "panel__body", style: "padding:0;gap:10px" });

    const left = h("div", { class: "panel", style: "width:260px;flex:0 0 260px" },
      h("div", { class: "panel__head" },
        icon("puzzle", 13, "var(--hls-accent)"), "插件中心",
        h("span", { class: "spacer" }),
        h("span", { class: "chip" }, "纯数据扩展")),
      h("div", { class: "panel__body" },
        h("div", { class: "row", style: "gap:6px" }, searchGlyph(), search),
        h("div", { class: "panel__head", style: "padding:2px 0 0" }, "分类"),
        categoryBox,
        h("div", { class: "divider" }),
        h("div", { class: "panel__head", style: "padding:2px 0 0" }, "已安装扩展"),
        countsBox,
        h("div", { style: "margin-top:auto;display:flex;flex-direction:column;gap:6px" },
          button("查看插件开发说明", showDevDocs, { icon: "workflow", variant: "ghost" }),
          h("div", { class: "tiny muted", style: "line-height:1.6" },
            "插件目录默认位于用户数据目录下的 plugins/，清单为 .json。")),
      ),
    );

    const main = h("div", { class: "main" },
      h("div", { class: "row", style: "padding:12px 14px 10px;gap:8px;border-bottom:1px solid var(--hls-border)" },
        h("span", { style: "font-weight:600" }, "插件中心"),
        h("span", { class: "tiny muted" }, "本地扩展 · 仅解析 JSON 清单，不执行插件代码"),
      ),
      h("div", {
        style: "flex:1;min-height:0;overflow-y:auto;padding:12px 14px 16px;display:flex;flex-direction:column;gap:10px",
      }, installBox, gridBox),
    );

    const right = h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
      h("div", { class: "panel__head" }, icon("info", 13, "var(--hls-accent)"), "插件详情"),
      h("div", { class: "panel__body" }, detailBox),
    );

    root.append(left, main, right);

    renderInstall();
    rerender();
    void refresh();
    void probeExamples();
  }

  function unmount(): void {
    panelRoot = null;
    categoryBox = null;
    countsBox = null;
    installBox = null;
    gridBox = null;
    detailBox = null;
    examples = [];
    filter = "all";
    query = "";
    selectedId = "";
  }

  return { id: "plugins", name: "插件中心", subtitle: "本地扩展 · 仅解析 JSON 清单，不执行插件代码", icon: "puzzle", mount, unmount };
}
