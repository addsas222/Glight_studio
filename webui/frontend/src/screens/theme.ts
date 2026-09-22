/**
 * 屏幕 A6 · 主题引擎
 *
 * 主题的全部内容就是 16 个 #RRGGBB 设计令牌（core/theme.py），应用主题时把它们
 * 写到 <html> 的 --hls-* 变量上，界面即刻换肤——不需要重新构建。
 *
 * 设计稿里的字号缩放 / 覆盖范围 / 组件数在本机不可推导，一律不展示；
 * 「对比度检查」里的比值全部由当前令牌实时计算（WCAG 2.1 相对亮度公式），
 * 不抄设计稿的数字。
 */
import { api, ThemeInfo, downloadBlobUrl } from "../api";
import type { Screen } from "../shell";
import { applyTheme, button, clear, h, icon, reportError, store, toast } from "../ui";

/** 令牌中文名（顺序与 core/theme.py 的 TOKEN_NAMES 一致） */
const TOKEN_ORDER = [
  "bg-root", "bg-panel", "bg-elevated", "bg-canvas",
  "border", "border-strong",
  "text-primary", "text-secondary", "text-muted",
  "accent", "accent-bright", "accent-tint", "accent-line",
  "success", "warm", "violet",
];

const TOKEN_LABEL: Record<string, string> = {
  "bg-root": "背景",
  "bg-panel": "面板",
  "bg-elevated": "浮层",
  "bg-canvas": "画布",
  "border": "边框",
  "border-strong": "强边框",
  "text-primary": "主文本",
  "text-secondary": "次文本",
  "text-muted": "弱文本",
  "accent": "强调色",
  "accent-bright": "强调亮色",
  "accent-tint": "强调底色",
  "accent-line": "强调描边",
  "success": "成功色",
  "warm": "暖色",
  "violet": "紫罗兰",
};

/** 圆角 / 描边的可选项（密度偏好，不影响主题配色） */
const RADIUS_CHOICES = [6, 8, 12, 16];
const STROKE_CHOICES = [1, 2];

/** styles.css 中 .btn--primary 的文字色是固定的深色（强调色上的按钮文字） */
const BUTTON_TEXT = "#08111e";

interface ContrastPair {
  label: string;
  /** 前景令牌名，或 "#RRGGBB" 字面量 */
  fg: string;
  bg: string;
  /** 大字/UI 元素只需 3:1，正文需要 4.5:1 */
  large: boolean;
}

const CONTRAST_PAIRS: ContrastPair[] = [
  { label: "主文本 / 背景", fg: "text-primary", bg: "bg-root", large: false },
  { label: "次要文字 / 面板", fg: "text-secondary", bg: "bg-panel", large: false },
  { label: "按钮文字 / 强调色", fg: BUTTON_TEXT, bg: "accent", large: true },
];

// ---------------------------------------------------------------- 颜色计算

/** sRGB 分量（0~1）→ 线性光 */
function toLinear(channel: number): number {
  return channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4);
}

/** 解析 #rgb / #rrggbb / rgb() / rgba()，失败返回 null */
function parseColor(css: string): [number, number, number] | null {
  const text = css.trim().toLowerCase();
  const hex = text.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/);
  if (hex) {
    const body = hex[1];
    const full = body.length === 3 ? body.split("").map((c) => c + c).join("") : body;
    return [
      parseInt(full.slice(0, 2), 16),
      parseInt(full.slice(2, 4), 16),
      parseInt(full.slice(4, 6), 16),
    ];
  }
  const rgb = text.match(/^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/);
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  return null;
}

/** WCAG 2.1 相对亮度 */
function luminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((c) => toLinear(c / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** 对比度 (L1+0.05)/(L2+0.05)；任一颜色无法解析返回 null */
function contrastRatio(fg: string, bg: string): number | null {
  const a = parseColor(fg);
  const b = parseColor(bg);
  if (!a || !b) return null;
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/** WCAG 等级文案；need 为该文本类型的 AA 门槛 */
function wcagLevel(ratio: number, need: number): string {
  if (ratio >= 7) return "AAA";
  if (ratio >= 4.5) return "AA";
  return ratio >= need ? "AA（大字/UI）" : "未达标";
}

/**
 * 显示用的比值：向零截断到 1 位小数。
 * 截断（而非四舍五入）保证「显示的数值 ≥ 门槛」时真实值一定也 ≥ 门槛，
 * 不会出现「4.5:1 未达标」这种自相矛盾的读数。
 */
function formatRatio(ratio: number): string {
  return (Math.floor(ratio * 10) / 10).toFixed(1);
}

// ---------------------------------------------------------------- 屏幕

export function createThemeScreen(): Screen {
  let mounted = false;
  let leftBox: HTMLElement | null = null;
  let countChip: HTMLElement | null = null;
  let centerBox: HTMLElement | null = null;
  let tokenBox: HTMLElement | null = null;
  let currentBox: HTMLElement | null = null;

  let themes: ThemeInfo[] = [];
  let defaultId = "blue";
  let activeId = "blue";
  let activeName = "";
  let tokens: Record<string, string> = {};
  let followSystem = false;
  let unsubscribe: (() => void) | null = null;
  let systemQuery: MediaQueryList | null = null;

  /** 当前生效的颜色：优先读 <html> 上真正生效的变量，回退到接口返回的令牌 */
  function currentColor(name: string): string {
    if (name.startsWith("#")) return name;
    const live = getComputedStyle(document.documentElement).getPropertyValue(`--hls-${name}`).trim();
    return live || tokens[name] || "";
  }

  // ---------------------------------------------------------------- 数据

  async function loadTokens(id: string): Promise<void> {
    try {
      const t = await api.theme(id);
      tokens = t.tokens;
      activeName = t.name;
    } catch (e) {
      reportError(e);
      tokens = {};
      activeName = "";
    }
    if (mounted) rerender();
  }

  async function apply(id: string): Promise<void> {
    const info = themes.find((t) => t.id === id);
    // 先更新本地选中项：applyTheme 会写入 store，避免订阅回调重复请求
    activeId = id;
    try {
      await applyTheme(id);
      await loadTokens(id);
      toast(`已应用主题 ${info?.name ?? id}`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  function toggleFollowSystem(): void {
    followSystem = !followSystem;
    if (followSystem) {
      if (systemQuery?.matches) {
        void apply(defaultId);
      } else {
        toast("系统当前为浅色外观：主题引擎目前只提供深色皮肤，已保持当前主题。", "info");
      }
    }
    renderLeft();
  }

  function onSystemAppearance(): void {
    if (!mounted || !followSystem || !systemQuery) return;
    if (systemQuery.matches) {
      void apply(defaultId);
    } else {
      toast("系统已切换到浅色外观；本版本只提供深色皮肤，保持当前主题。", "info");
    }
  }

  function exportTheme(): void {
    const payload = { id: activeId, name: activeName, tokens };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    downloadBlobUrl(url, `horizon-theme-${activeId}.json`);
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast("已导出主题令牌 JSON", "ok");
  }

  // ---------------------------------------------------------------- 渲染

  function rerender(): void {
    renderLeft();
    renderCenter();
    renderTokens();
    renderCurrent();
  }

  function renderLeft(): void {
    const box = leftBox;
    if (!box) return;
    clear(box);
    if (countChip) countChip.textContent = `${themes.length} 个主题`;

    for (const t of themes) {
      const active = t.id === activeId;
      const row = h("div", { class: active ? "item on" : "item", style: "cursor:pointer;gap:9px" },
        h("i", {
          style: `width:22px;height:22px;flex:0 0 22px;border-radius:7px;display:inline-block;
                  background:linear-gradient(135deg, ${t.accent}, var(--hls-bg-canvas));
                  border:1px solid var(--hls-border-strong)`,
        }),
        h("div", { class: "grow" },
          h("div", { class: "item__t" }, t.name),
          h("div", { class: "item__d" }, `${t.dark ? "深色" : "浅色"} · ${t.accent.toUpperCase()}`)),
        active ? icon("check", 14, "var(--hls-accent-bright)") : null,
      );
      row.addEventListener("click", () => { if (!active) void apply(t.id); });
      box.append(row);
    }

    if (!themes.length) {
      box.append(h("div", { class: "small muted" }, "未取到主题列表，请检查本地服务。"));
    }

    box.append(
      h("div", { class: "divider", style: "margin:8px 0" }),
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
        h("div", { class: "row row--between", style: "gap:10px" },
          h("div", { class: "grow" },
            h("div", { style: "font-size:12px" }, "跟随系统外观"),
            h("div", { class: "tiny muted", style: "line-height:1.5" },
              "系统为深色时自动应用默认主题；浅色系统下保持当前主题")),
          button(followSystem ? "开" : "关", toggleFollowSystem, { on: followSystem })),
        h("div", { class: "divider" }),
        button("新建主题", () => {
          toast("自定义主题由插件中心的「主题外观」类插件提供（安装后由其贡献令牌）；当前版本主题引擎列出的是内置主题。", "info");
        }, { icon: "plus", variant: "ghost", title: "自定义主题通过插件提供" }),
      ),
    );
  }

  /** 界面预览：一个假的应用窗口，用 var(--hls-*) 上色，因此换主题即时可见 */
  function mockWindow(): HTMLElement {
    const windowDot = (color: string): HTMLElement =>
      h("i", { style: `width:8px;height:8px;border-radius:50%;display:inline-block;background:${color}` });

    const railItem = (glyph: string, label: string, active: boolean): HTMLElement =>
      h("div", {
        style: `display:flex;flex-direction:column;align-items:center;gap:3px;font-size:8px;
                color:${active ? "var(--hls-text-primary)" : "var(--hls-text-muted)"}`,
      },
        h("div", {
          style: `width:26px;height:24px;border-radius:7px;display:flex;align-items:center;justify-content:center;
                  background:${active ? "var(--hls-accent-tint)" : "transparent"};
                  border:1px solid ${active ? "var(--hls-accent-line)" : "var(--hls-border)"}`,
        }, icon(glyph, 12, active ? "var(--hls-accent-bright)" : "currentColor")),
        h("span", {}, label),
      );

    const mockSlider = (label: string, width: number): HTMLElement =>
      h("div", { style: "display:flex;flex-direction:column;gap:3px" },
        h("span", { class: "tiny muted" }, label),
        h("div", { class: "bar" }, h("i", { style: `width:${width}%` })));

    return h("div", {
      class: "card",
      style: `padding:0;overflow:hidden;border-radius:var(--hls-radius,8px);
              border:var(--hls-border-width,1px) solid var(--hls-border)`,
    },
      // 窗口标题栏
      h("div", {
        style: `display:flex;align-items:center;gap:8px;padding:7px 10px;
                background:var(--hls-bg-root);border-bottom:1px solid var(--hls-border)`,
      },
        windowDot("#f87171"), windowDot("#fbbf24"), windowDot("#34d399"),
        h("span", { style: "font-size:11px;font-weight:600;margin-left:4px" }, "凌日光影棚"),
        h("span", { class: "chip tiny" }, "预览示意"),
        h("span", { class: "spacer" }),
        h("span", { class: "tiny", style: "color:var(--hls-success)" }, "本地 AI · 已连接"),
      ),
      // 窗口主体
      h("div", { style: "display:flex;gap:0;background:var(--hls-bg-panel)" },
        h("div", {
          style: `width:52px;flex:0 0 52px;display:flex;flex-direction:column;align-items:center;gap:8px;
                  padding:9px 0;background:var(--hls-bg-root);border-right:1px solid var(--hls-border)`,
        },
          railItem("folder", "素材", false),
          railItem("lightbulb", "光照", true),
          railItem("sparkles", "AI 打光", false),
          railItem("layers", "图层", false),
        ),
        h("div", {
          style: `flex:1;min-height:186px;margin:10px;border-radius:var(--hls-radius,8px);
                  border:1px solid var(--hls-border);position:relative;overflow:hidden;
                  background:radial-gradient(circle at 34% 30%, var(--hls-accent-tint) 0%, var(--hls-bg-canvas) 68%)`,
        },
          h("i", {
            style: `position:absolute;left:34%;top:30%;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;
                    background:var(--hls-accent-bright);box-shadow:0 0 22px 8px var(--hls-accent)`,
          }),
          h("i", {
            style: `position:absolute;left:62%;top:58%;width:8px;height:8px;margin:-4px 0 0 -4px;border-radius:50%;
                    background:var(--hls-warm);box-shadow:0 0 16px 5px var(--hls-warm)`,
          }),
          h("span", { class: "chip", style: "position:absolute;left:9px;top:9px" }, "主光 · 示意"),
          h("span", { class: "chip", style: "position:absolute;right:9px;bottom:9px" }, "背景"),
        ),
        h("div", {
          style: `width:132px;flex:0 0 132px;padding:10px;display:flex;flex-direction:column;gap:8px;
                  background:var(--hls-bg-root);border-left:1px solid var(--hls-border)`,
        },
          h("div", { style: "font-size:11px;font-weight:600" }, "光照检查器"),
          h("div", { class: "tiny muted" }, "（示意，非真实测量值）"),
          mockSlider("强度", 68),
          mockSlider("色温", 52),
          mockSlider("半径", 42),
          h("div", { class: "row", style: "gap:6px" },
            h("i", { class: "dot" }),
            h("span", { class: "tiny sec" }, "环境光已启用")),
        ),
      ),
    );
  }

  interface ContrastRow extends ContrastPair {
    ratio: number | null;
    need: number;
  }

  function contrastRows(): ContrastRow[] {
    return CONTRAST_PAIRS.map((pair) => ({
      ...pair,
      ratio: contrastRatio(currentColor(pair.fg), currentColor(pair.bg)),
      need: pair.large ? 3 : 4.5,
    }));
  }

  function renderCenter(): void {
    const box = centerBox;
    if (!box) return;
    clear(box);

    const rows = contrastRows();
    const failed = rows.filter((r) => r.ratio !== null && r.ratio < r.need);
    const unknown = rows.filter((r) => r.ratio === null);

    box.append(
      h("div", { class: "row", style: "gap:8px" },
        h("span", { style: "font-weight:600" }, activeName || activeId),
        h("span", { class: "chip", style: "color:var(--hls-success)" },
          h("i", { class: "dot" }), "已应用"),
        h("span", { class: "spacer" }),
        h("span", { class: "tiny muted" }, "界面预览，直接使用当前令牌着色"),
      ),
      mockWindow(),
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:7px" },
        h("div", { class: "row", style: "gap:7px" },
          icon("gauge", 14, "var(--hls-accent)"),
          h("span", { style: "font-weight:600;font-size:12.5px" }, "对比度检查"),
          h("span", { class: "spacer" }),
          h("span", { class: "tiny muted" }, "WCAG 2.1 相对亮度，实时计算")),
        ...rows.map((r) => h("div", { class: "kv" },
          h("span", {}, r.label),
          h("span", { class: "row", style: "gap:8px" },
            h("b", { class: "mono" }, r.ratio === null ? "无法计算" : `${formatRatio(r.ratio)}:1`),
            h("span", {
              class: "chip tiny",
              style: r.ratio === null
                ? "color:var(--hls-text-muted)"
                : (r.ratio < r.need ? "color:#fca5a5" : "color:var(--hls-success)"),
            }, r.ratio === null ? "缺少令牌" : wcagLevel(r.ratio, r.need))))),
        h("div", { class: "divider" }),
        h("div", {
          class: "small",
          style: `line-height:1.7;color:${failed.length || unknown.length ? "var(--hls-warm)" : "var(--hls-success)"}`,
        },
          unknown.length
            ? `以下组合无法计算（未取到对应令牌）：${unknown.map((r) => r.label).join("、")}`
            : failed.length === 0
              ? "全部满足 WCAG 2.1 AA（正文需 4.5:1，大字 / UI 元素需 3:1）"
              : `以下组合未达 AA：${failed.map((r) => `${r.label}（${formatRatio(r.ratio ?? 0)}:1，需 ${r.need}:1）`).join("；")}`),
      ),
    );
  }

  function renderTokens(): void {
    const box = tokenBox;
    if (!box) return;
    clear(box);

    const names = [...TOKEN_ORDER.filter((n) => n in tokens), ...Object.keys(tokens).filter((n) => !TOKEN_ORDER.includes(n))];
    if (!names.length) {
      box.append(h("div", { class: "small muted" }, "未取到令牌，请重新应用主题。"));
      return;
    }

    const colors = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:5px" });
    for (const name of names) {
      const value = tokens[name];
      colors.append(h("div", { class: "row", style: "gap:7px" },
        h("i", {
          style: `width:13px;height:13px;flex:0 0 13px;border-radius:4px;display:inline-block;
                  background:${value};border:1px solid var(--hls-border-strong)`,
        }),
        h("span", { class: "small grow ellipsis" }, TOKEN_LABEL[name] ?? name),
        h("span", { class: "tiny mono muted" }, value.toUpperCase())));
    }

    // 密度偏好只写 CSS 变量：--hls-border 是主题的“边框颜色”，
    // 用数值覆盖它会把边框色写坏，所以描边宽度用独立的 --hls-border-width。
    const rootStyle = document.documentElement.style;
    const radius = Number(rootStyle.getPropertyValue("--hls-radius").replace("px", "")) || 8;
    const stroke = Number(rootStyle.getPropertyValue("--hls-border-width").replace("px", "")) || 1;

    const radiusSelect = h("select", { class: "select" },
      ...RADIUS_CHOICES.map((px) => h("option", { value: String(px), selected: px === radius }, `${px} px`)));
    radiusSelect.addEventListener("change", () => {
      document.documentElement.style.setProperty("--hls-radius", `${radiusSelect.value}px`);
    });

    const strokeSelect = h("select", { class: "select" },
      ...STROKE_CHOICES.map((px) => h("option", { value: String(px), selected: px === stroke }, `${px} px`)));
    strokeSelect.addEventListener("change", () => {
      document.documentElement.style.setProperty("--hls-border-width", `${strokeSelect.value}px`);
    });

    box.append(
      h("div", { class: "panel__head", style: "padding:0 0 2px" }, `颜色 · ${names.length} 个令牌`),
      colors,
      h("div", { class: "panel__head", style: "padding:6px 0 2px" }, "排版"),
      h("div", { class: "card" },
        h("div", { class: "kv" }, h("span", {}, "正文字体"), h("b", {}, "Inter · Noto Sans SC")),
        h("div", { class: "tiny muted", style: "margin-top:4px" },
          "只读：字体随构建产物固定，主题只换颜色，不换字体。")),
      h("div", { class: "panel__head", style: "padding:6px 0 2px" }, "形状与密度"),
      h("div", { class: "card", style: "display:flex;flex-direction:column;gap:9px" },
        h("div", { class: "grid2" },
          h("div", { class: "field" }, h("label", {}, "圆角"), radiusSelect),
          h("div", { class: "field" }, h("label", {}, "描边"), strokeSelect)),
        h("div", { class: "tiny muted", style: "line-height:1.6" },
          "这是界面密度偏好，写在 --hls-radius / --hls-border-width 上（边框颜色 --hls-border 仍由主题决定），仅影响本机界面观感。")),
    );
  }

  function renderCurrent(): void {
    const box = currentBox;
    if (!box) return;
    clear(box);

    box.append(
      h("div", { class: "panel__head", style: "padding:0 0 2px" }, "当前主题"),
      h("div", { class: "card" },
        h("div", { class: "kv" }, h("span", {}, "名称"), h("b", {}, activeName || "—")),
        h("div", { class: "kv" }, h("span", {}, "标识"), h("b", { class: "mono" }, activeId)),
        h("div", { class: "kv" }, h("span", {}, "主题数"), h("b", {}, `${themes.length} 个内置主题`)),
        h("div", { class: "kv" }, h("span", {}, "令牌数"), h("b", {}, `${Object.keys(tokens).length} 个`))),
      button("应用主题", () => { void apply(activeId); }, { icon: "check", variant: "primary" }),
      button("导出主题 JSON", exportTheme, { icon: "download" }),
      h("div", { class: "tiny muted", style: "line-height:1.6" },
        "导出内容为当前主题的 id / name / tokens，可直接作为「主题外观」插件的 data.tokens 使用。"),
    );
  }

  // ---------------------------------------------------------------- 组装

  function mount(root: HTMLElement): void {
    mounted = true;

    leftBox = h("div", { class: "panel__body", style: "padding:0;gap:4px" });
    centerBox = h("div", {
      style: "flex:1;min-height:0;overflow-y:auto;padding:12px 14px 16px;display:flex;flex-direction:column;gap:10px",
    });
    tokenBox = h("div", { class: "panel__body", style: "padding:0;gap:10px" });
    currentBox = h("div", { class: "panel__body", style: "padding:0;gap:8px" });

    countChip = h("span", { class: "chip" }, "—");

    const left = h("div", { class: "panel", style: "width:268px;flex:0 0 268px" },
      h("div", { class: "panel__head" },
        icon("palette", 13, "var(--hls-accent)"), "主题引擎",
        h("span", { class: "spacer" }),
        countChip),
      h("div", { class: "panel__body" },
        h("div", { class: "panel__head", style: "padding:2px 0 0" }, "已安装主题"),
        leftBox),
    );

    const main = h("div", { class: "main" },
      h("div", { class: "row", style: "padding:12px 14px 10px;gap:8px;border-bottom:1px solid var(--hls-border)" },
        h("span", { style: "font-weight:600" }, "外观系统"),
        h("span", { class: "tiny muted" }, "16 个设计令牌 · 运行时写入 CSS 变量，无需重新构建"),
      ),
      centerBox,
    );

    const right = h("div", { class: "panel panel--right", style: "width:330px;flex:0 0 330px" },
      h("div", { class: "panel__head" }, icon("sliders", 13, "var(--hls-accent)"), "设计令牌"),
      h("div", { class: "panel__body" }, tokenBox, h("div", { class: "divider" }), currentBox),
    );

    root.append(left, main, right);

    rerender();

    systemQuery = window.matchMedia("(prefers-color-scheme: dark)");
    systemQuery.addEventListener("change", onSystemAppearance);

    unsubscribe = store.subscribe(() => {
      if (!mounted) return;
      if (store.ctx.themeId && store.ctx.themeId !== activeId) {
        activeId = store.ctx.themeId;
        void loadTokens(activeId);
      }
    });

    void (async () => {
      try {
        const list = await api.themes();
        themes = list.themes;
        defaultId = list.default;
      } catch (e) {
        reportError(e);
      }
      activeId = store.ctx.themeId || defaultId;
      await loadTokens(activeId);
    })();
  }

  function unmount(): void {
    mounted = false;
    unsubscribe?.();
    unsubscribe = null;
    systemQuery?.removeEventListener("change", onSystemAppearance);
    systemQuery = null;
    leftBox = null;
    centerBox = null;
    tokenBox = null;
    currentBox = null;
    followSystem = false;
  }

  return { id: "theme", name: "主题引擎", subtitle: "外观系统 · 16 个设计令牌 · 运行时写入 CSS 变量，无需重新构建", icon: "palette", mount, unmount };
}
