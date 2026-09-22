/**
 * 屏幕 A3 · 专业模式：节点图工作流编辑器。
 *
 * 职责：
 *   - 从 /api/workflow/types 拉取节点类型，按 category 归组渲染节点库；
 *   - 画布上用绝对定位的节点 div + 一层 SVG 画连线，节点可拖拽、可连线；
 *   - 右侧属性面板编辑选中节点的参数（light / global / auto_light）；
 *   - 求值走 /api/workflow/evaluate：成功应用参数并渲染预览，失败列出中文 problems；
 *   -「AI 打光」输入栏走 /api/autolight，返回的节点自动串成 input → … → output；
 *   - 工作流可从 / 向 JSON 导入导出。
 *
 * 诚实性：本屏幕只显示真实的节点结构与参数值，不生成任何测量类数字。
 */
import {
  api, AutoLightNode, RenderParams, Workflow, WorkflowEdge, WorkflowNode, WorkflowNodeType,
  downloadBlobUrl, pngUrl,
} from "../api";
import { button, clear, h, icon, reportError, segmented, slider, store, toast, Trackball } from "../ui";
import { Screen, setParams } from "../shell";

// ---------------------------------------------------------------- 常量

const NODE_W = 178;
const NODE_H = 60;
/** 单端口节点的端口纵向中心 */
const PORT_Y = 30;
/** 多端口节点（参考图迁移有两个输入）的端口纵向起点与间距 */
const PORT_TOP = 15;
const PORT_GAP = 22;

/** 分类配色（用设计令牌，运行时随主题变化） */
const CAT_COLOR: Record<string, string> = {
  "输入": "var(--hls-accent)",
  "AI": "var(--hls-violet)",
  "灯光": "var(--hls-warm)",
  "交互": "var(--hls-success)",
  "参考": "var(--hls-accent-bright)",
  "输出": "var(--hls-text-secondary)",
};

/** 后端 /api/workflow/types 实际返回 `type` 键（api.ts 注释为 id），两者都认 */
interface NodeTypeDef extends WorkflowNodeType { type?: string }

const nodeTypeId = (t: NodeTypeDef): string => {
  const alt = t.type;
  return alt && alt.length ? alt : t.id;
};

const asNumber = (v: unknown, fallback: number): number =>
  typeof v === "number" && Number.isFinite(v) ? v : fallback;

const asString = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);

/** JSON 边界的唯一收窄点：非对象一律当空对象，之后所有字段读取都由类型系统保证 */
function asRecord(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

/** 解析导入的 JSON 为工作流；结构不对返回 null */
function parseWorkflow(raw: unknown): Workflow | null {
  const top = asRecord(raw);
  if (!Array.isArray(top.nodes) || !Array.isArray(top.edges)) return null;

  const nodes: WorkflowNode[] = [];
  for (const item of top.nodes) {
    const r = asRecord(item);
    const id = asString(r.id);
    const type = asString(r.type);
    if (!id || !type) continue;
    nodes.push({
      id,
      type,
      name: asString(r.name),
      params: { ...asRecord(r.params) },
      x: asNumber(r.x, 0),
      y: asNumber(r.y, 0),
    });
  }
  const edges: WorkflowEdge[] = [];
  for (const item of top.edges) {
    const r = asRecord(item);
    const src = asString(r.src);
    const dst = asString(r.dst);
    if (!src || !dst) continue;
    edges.push({
      src,
      dst,
      src_port: asString(r.src_port, "out") || "out",
      dst_port: asString(r.dst_port, "in") || "in",
    });
  }
  return { nodes, edges, name: asString(top.name, "导入的工作流") || "导入的工作流" };
}

/** SVG 元素小工具 */
function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K, attrs: Record<string, string | number>,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** 新增节点时的默认参数（范围与 core/workflow.py 的收敛范围一致） */
function defaultParams(type: string): Record<string, unknown> {
  switch (type) {
    case "light":
      return {
        name: "新光源", kind: "directional",
        dx: -0.5, dy: -0.5, dz: 0.7, intensity: 1.0, kelvin: 5600, radius: 0.5,
      };
    case "global":
      return {
        ambient_intensity: 0.4, ambient_kelvin: 6000, exposure: 1.0,
        shadow_mode: "soft", lighting_mode: "linear",
      };
    case "auto_light":
      return { prompt: "" };
    default:
      return {};
  }
}

// ---------------------------------------------------------------- 屏幕

export function createProModeScreen(): Screen {
  let types: NodeTypeDef[] = [];
  let workflow: Workflow = { nodes: [], edges: [], name: "专业模式工作流" };
  let selected: string | null = null;
  let selectedEdge = -1;
  let pendingSrc: { id: string; port: string } | null = null;
  let scale = 1;
  let panX = 24;
  let panY = 24;
  let uid = 1;
  let busy = false;
  let mounted = false;
  let evalPng: string | null = null;
  let evalParams: RenderParams | null = null;
  let note = "";
  let paletteQuery = "";

  // DOM 引用（mount 时赋值）
  let canvasEl!: HTMLDivElement;
  let worldEl!: HTMLDivElement;
  let edgeLayer!: SVGGElement;
  let tempPath!: SVGPathElement;
  let inspectorEl!: HTMLElement;
  let paletteEl!: HTMLElement;
  let statusEl!: HTMLElement;
  let zoomLabel!: HTMLElement;
  let evaluateBtn!: HTMLButtonElement;
  let onKey: ((e: KeyboardEvent) => void) | null = null;

  interface CardRef { card: HTMLDivElement; summary: HTMLElement; nameEl: HTMLElement }
  const cards = new Map<string, CardRef>();

  // 轨迹球整个屏幕只建一次，避免每次重建属性面板都注册一对 window 监听
  let ball: Trackball | null = null;
  let ballHandler: (dx: number, dy: number, dz: number) => void = () => {};

  // ------------------------------------------------------- 小工具

  const nodeById = (id: string | null): WorkflowNode | undefined =>
    id === null ? undefined : workflow.nodes.find((n) => n.id === id);

  const typeOf = (t: string): NodeTypeDef | undefined => types.find((x) => nodeTypeId(x) === t);

  const catColor = (t: string): string => CAT_COLOR[typeOf(t)?.category ?? ""] ?? "var(--hls-text-secondary)";

  /** api.ts 的 WorkflowNode.x/y 声明为可选，布局与拖拽一律按 0 兜底 */
  const nx = (n: WorkflowNode): number => asNumber(n.x, 0);
  const ny = (n: WorkflowNode): number => asNumber(n.y, 0);

  const portsOf = (node: WorkflowNode, dir: "in" | "out"): { id: string; label: string }[] => {
    const spec = typeOf(node.type);
    const list = dir === "in" ? spec?.inputs : spec?.outputs;
    return list && list.length ? list : [{ id: dir === "in" ? "in" : "out", label: dir === "in" ? "输入" : "输出" }];
  };

  const portOffset = (idx: number, total: number): number =>
    total <= 1 ? PORT_Y : PORT_TOP + idx * PORT_GAP;

  const portIndexOf = (node: WorkflowNode, dir: "in" | "out", portId: string | undefined): number => {
    const list = portsOf(node, dir);
    const i = list.findIndex((p) => p.id === portId);
    return i >= 0 ? i : 0;
  };

  const newId = (prefix: string): string => {
    let id = `${prefix}${uid}`;
    while (workflow.nodes.some((n) => n.id === id)) {
      uid += 1;
      id = `${prefix}${uid}`;
    }
    uid += 1;
    return id;
  };

  function summaryOf(node: WorkflowNode): string {
    const p = node.params ?? {};
    switch (node.type) {
      case "input":
        return store.ctx.mediaName ? `当前媒体：${store.ctx.mediaName}` : "等待工作台载入媒体";
      case "depth":
        return "本地深度 / 法线估计";
      case "auto_light": {
        const prompt = asString(p.prompt).trim();
        return prompt ? `「${prompt.slice(0, 14)}${prompt.length > 14 ? "…" : ""}」` : "未填写氛围描述";
      }
      case "light": {
        const kind = asString(p.kind, "directional") === "point" ? "点光" : "平行光";
        return `${kind} · 强度 ${asNumber(p.intensity, 0).toFixed(2)} · ${Math.round(asNumber(p.kelvin, 5600))}K`;
      }
      case "global":
        return `环境 ${asNumber(p.ambient_intensity, 0).toFixed(2)} · 曝光 ${asNumber(p.exposure, 1).toFixed(2)}`;
      case "pick":
        return "画布点击拾取锚定";
      case "reference":
        return "参考图保守迁移";
      case "output":
        return "合成输出 · 交由导出";
      default:
        return "";
    }
  }

  // ------------------------------------------------------- 画布

  function applyView(): void {
    worldEl.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
  }

  function portPos(node: WorkflowNode, dir: "in" | "out", portId: string | undefined): { x: number; y: number } {
    const list = portsOf(node, dir);
    const idx = portIndexOf(node, dir, portId);
    const yo = portOffset(idx, list.length);
    return dir === "out"
      ? { x: nx(node) + NODE_W, y: ny(node) + yo }
      : { x: nx(node), y: ny(node) + yo };
  }

  function edgePath(x1: number, y1: number, x2: number, y2: number): string {
    const dx = Math.min(120, Math.max(30, Math.abs(x2 - x1) * 0.55));
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }

  function renderEdges(): void {
    edgeLayer.replaceChildren();
    workflow.edges.forEach((edge, i) => {
      const src = nodeById(edge.src);
      const dst = nodeById(edge.dst);
      if (!src || !dst) return;
      const a = portPos(src, "out", edge.src_port);
      const b = portPos(dst, "in", edge.dst_port);
      const d = edgePath(a.x, a.y, b.x, b.y);
      const on = i === selectedEdge;

      const hit = svgEl("path", { d, fill: "none", stroke: "transparent", "stroke-width": 12 });
      hit.style.pointerEvents = "stroke";
      hit.style.cursor = "pointer";
      hit.addEventListener("click", (e) => {
        e.stopPropagation();
        selectedEdge = i;
        selected = null;
        renderEdges();
        renderInspector();
        renderStatus();
      });

      const line = svgEl("path", {
        d, fill: "none",
        stroke: on ? "var(--hls-warm)" : "var(--hls-border-strong)",
        "stroke-width": on ? 2.4 : 1.8,
      });
      line.style.pointerEvents = "none";
      edgeLayer.append(hit, line);
    });
    edgeLayer.append(tempPath);
  }

  /** 连线待定时跟随光标的虚线 */
  function setTempPath(d: string): void {
    tempPath.setAttribute("d", d);
  }

  function renderNodes(): void {
    cards.clear();
    for (const card of Array.from(worldEl.querySelectorAll<HTMLElement>("[data-node-id]"))) card.remove();
    for (const node of workflow.nodes) buildCard(node);
    renderEdges();
    renderStatus();
  }

  function buildCard(node: WorkflowNode): void {
    const color = catColor(node.type);
    const spec = typeOf(node.type);
    const dot = h("i", { style: `width:7px;height:7px;border-radius:50%;flex:0 0 auto;background:${color}` });
    const nameEl = h("span", { class: "ellipsis", style: "flex:1;min-width:0" },
      node.name || spec?.name || node.type);
    const head = h("div", {
      style: `display:flex;align-items:center;gap:6px;padding:0 8px;height:26px;
              background:var(--hls-bg-panel);border-bottom:1px solid var(--hls-border);
              border-radius:7px 7px 0 0;font-size:11.5px;font-weight:600;overflow:hidden`,
    }, dot, nameEl);
    const summary = h("div", {
      class: "ellipsis",
      style: "padding:7px 8px;font-size:10.5px;color:var(--hls-text-secondary);line-height:1.4",
    }, summaryOf(node));

    const card = h("div", {
      "data-node-id": node.id,
      style: `position:absolute;left:${nx(node)}px;top:${ny(node)}px;width:${NODE_W}px;height:${NODE_H}px;
              border-radius:8px;background:var(--hls-bg-elevated);border:1px solid var(--hls-border);
              cursor:grab;user-select:none;overflow:visible`,
    }, head, summary);

    // 输入端口
    const ins = portsOf(node, "in");
    ins.forEach((p, i) => {
      const y = portOffset(i, ins.length);
      const port = h("div", {
        title: `输入：${p.label}`,
        style: `position:absolute;left:-6px;top:${y - 5}px;width:10px;height:10px;border-radius:50%;
                background:var(--hls-bg-root);border:2px solid ${color};cursor:crosshair`,
      });
      port.addEventListener("pointerdown", (e) => e.stopPropagation());
      port.addEventListener("click", (e) => {
        e.stopPropagation();
        const src = pendingSrc;
        if (!src) {
          note = "请先点击某个节点的输出端口，再点击这里的输入端口";
          renderStatus();
          return;
        }
        if (src.id === node.id) {
          note = "不能把节点连接到自身";
          renderStatus();
          return;
        }
        const dup = workflow.edges.some((ed) => ed.src === src.id && ed.dst === node.id && ed.dst_port === p.id);
        if (!dup) workflow.edges.push({ src: src.id, dst: node.id, src_port: src.port, dst_port: p.id });
        pendingSrc = null;
        note = "";
        renderNodes();
      });
      card.append(port);
    });

    // 输出端口
    const outs = portsOf(node, "out");
    outs.forEach((p, i) => {
      const y = portOffset(i, outs.length);
      const active = pendingSrc?.id === node.id && pendingSrc.port === p.id;
      const port = h("div", {
        title: `输出：${p.label}`,
        style: `position:absolute;right:-6px;top:${y - 5}px;width:10px;height:10px;border-radius:50%;
                background:${active ? color : "var(--hls-bg-root)"};border:2px solid ${color};cursor:crosshair`,
      });
      port.addEventListener("pointerdown", (e) => e.stopPropagation());
      port.addEventListener("click", (e) => {
        e.stopPropagation();
        pendingSrc = active ? null : { id: node.id, port: p.id };
        note = pendingSrc ? `已选中「${node.name || node.type}」的输出，请点击目标节点的输入端口` : "";
        renderNodes();
      });
      card.append(port);
    });

    // 选择 + 拖拽（拖拽期间只移动本卡片并重画连线，绝不重建 DOM）
    card.addEventListener("pointerdown", (e) => {
      selectOnCanvas(node.id);
      const startX = e.clientX;
      const startY = e.clientY;
      const ox = nx(node);
      const oy = ny(node);
      card.style.cursor = "grabbing";
      const onMove = (ev: PointerEvent): void => {
        node.x = Math.round(ox + (ev.clientX - startX) / scale);
        node.y = Math.round(oy + (ev.clientY - startY) / scale);
        card.style.left = `${node.x}px`;
        card.style.top = `${node.y}px`;
        renderEdges();
      };
      const onUp = (): void => {
        card.style.cursor = "grab";
        card.removeEventListener("pointermove", onMove);
        card.removeEventListener("pointerup", onUp);
      };
      card.addEventListener("pointermove", onMove);
      card.addEventListener("pointerup", onUp);
      // 捕获不是拖拽的必要条件：拿不到（例如指针已释放）也照样拖动
      try {
        card.setPointerCapture(e.pointerId);
      } catch { /* 忽略：监听已挂好 */ }
    });

    card.style.borderColor = node.id === selected ? color : "var(--hls-border)";
    card.style.boxShadow = node.id === selected
      ? `0 0 0 1px ${color}, 0 4px 16px #0008` : "0 3px 12px #0007";
    worldEl.append(card);
    cards.set(node.id, { card, summary, nameEl });
  }

  /** 改参数 / 改名后只刷新这张卡片的文案，不重建 DOM */
  function refreshCard(node: WorkflowNode): void {
    const entry = cards.get(node.id);
    if (!entry) return;
    entry.summary.textContent = summaryOf(node);
    entry.nameEl.textContent = node.name || typeOf(node.type)?.name || node.type;
  }

  function selectOnCanvas(id: string | null): void {
    selected = id;
    selectedEdge = -1;
    for (const node of workflow.nodes) {
      const entry = cards.get(node.id);
      if (!entry) continue;
      const sel = node.id === id;
      const color = catColor(node.type);
      entry.card.style.borderColor = sel ? color : "var(--hls-border)";
      entry.card.style.boxShadow = sel ? `0 0 0 1px ${color}, 0 4px 16px #0008` : "0 3px 12px #0007";
    }
    renderInspector();
    renderStatus();
  }

  function fitView(): void {
    if (!workflow.nodes.length) return;
    const xs = workflow.nodes.map((n) => nx(n));
    const ys = workflow.nodes.map((n) => ny(n));
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const maxX = Math.max(...xs) + NODE_W;
    const maxY = Math.max(...ys) + NODE_H;
    const r = canvasEl.getBoundingClientRect();
    const pad = 36;
    const s = Math.min(
      (r.width - pad * 2) / Math.max(maxX - minX, 1),
      (r.height - pad * 2) / Math.max(maxY - minY, 1),
      1.3,
    );
    scale = Math.max(0.3, Math.min(1.3, s));
    panX = pad - minX * scale + Math.max(0, (r.width - pad * 2 - (maxX - minX) * scale) / 2);
    panY = pad - minY * scale + Math.max(0, (r.height - pad * 2 - (maxY - minY) * scale) / 2);
    applyView();
  }

  function addNode(type: string, atX?: number, atY?: number): WorkflowNode {
    const r = canvasEl.getBoundingClientRect();
    const x = atX ?? Math.round((r.width / 2 - panX) / scale - NODE_W / 2) + (workflow.nodes.length % 4) * 18;
    const y = atY ?? Math.round((r.height / 2 - panY) / scale - NODE_H / 2) + (workflow.nodes.length % 3) * 16;
    const node: WorkflowNode = {
      id: newId("n_"),
      type,
      name: typeOf(type)?.name ?? type,
      params: defaultParams(type),
      x,
      y,
    };
    workflow.nodes.push(node);
    buildCard(node);
    renderEdges();
    selectOnCanvas(node.id);
    return node;
  }

  function deleteSelected(): void {
    if (selectedEdge >= 0) {
      workflow.edges.splice(selectedEdge, 1);
      selectedEdge = -1;
      renderEdges();
      renderInspector();
      renderStatus();
      return;
    }
    const id = selected;
    if (id === null) return;
    workflow.nodes = workflow.nodes.filter((n) => n.id !== id);
    workflow.edges = workflow.edges.filter((e) => e.src !== id && e.dst !== id);
    if (pendingSrc?.id === id) pendingSrc = null;
    selected = null;
    renderNodes();
    renderInspector();
  }

  // ------------------------------------------------------- 节点库

  function renderPalette(): void {
    clear(paletteEl);
    const q = paletteQuery.trim().toLowerCase();
    const groups = new Map<string, NodeTypeDef[]>();
    for (const t of types) {
      const name = asString(t.name);
      if (q && !name.toLowerCase().includes(q) && !nodeTypeId(t).toLowerCase().includes(q)) continue;
      const list = groups.get(t.category);
      if (list) list.push(t);
      else groups.set(t.category, [t]);
    }
    if (!groups.size) {
      paletteEl.append(h("div", { class: "small muted", style: "padding:6px 2px" }, "没有匹配的节点"));
      return;
    }
    for (const [cat, list] of groups) {
      const color = CAT_COLOR[cat] ?? "var(--hls-text-secondary)";
      paletteEl.append(h("div", {
        class: "tiny",
        style: "letter-spacing:.5px;color:var(--hls-text-muted);margin:8px 0 2px;display:flex;align-items:center;gap:6px",
      }, h("i", { style: `width:6px;height:6px;border-radius:50%;background:${color}` }), cat));
      for (const t of list) {
        const b = h("button", { class: "item", type: "button", title: `加入画布：${t.name}` },
          h("span", { class: "row", style: `color:${color}` }, icon("plus", 12)),
          h("span", { class: "grow ellipsis" }, t.name),
          h("span", { class: "tiny muted" }, `${t.inputs.length}→${t.outputs.length}`),
        );
        b.addEventListener("click", () => {
          const node = addNode(nodeTypeId(t));
          note = `已加入节点：${node.name}`;
          renderStatus();
        });
        paletteEl.append(b);
      }
    }
  }

  // ------------------------------------------------------- 属性面板

  function setParam(node: WorkflowNode, key: string, value: unknown): void {
    node.params = { ...(node.params ?? {}), [key]: value };
    refreshCard(node);
    renderEdges();
  }

  function renderInspector(): void {
    clear(inspectorEl);

    if (selectedEdge >= 0) {
      const edge = workflow.edges[selectedEdge];
      inspectorEl.append(h("div", { class: "small sec" }, "已选中连线"));
      if (edge) {
        inspectorEl.append(h("div", { class: "card" },
          h("div", { class: "kv" }, h("span", {}, "起点"), h("b", { class: "mono" }, edge.src)),
          h("div", { class: "kv" }, h("span", {}, "终点"), h("b", { class: "mono" }, edge.dst)),
        ));
      }
      inspectorEl.append(button("删除连线", () => deleteSelected(), { icon: "trash", variant: "danger" }));
      return;
    }

    const node = nodeById(selected);
    if (!node) {
      inspectorEl.append(
        h("div", { class: "small muted" }, "在画布上点击一个节点查看并编辑它的参数；点击连线可选中连线。"),
        h("div", { class: "card small muted" }, "连线方式：先点节点的输出端口，再点目标节点的输入端口。"),
      );
      return;
    }

    const spec = typeOf(node.type);
    const color = catColor(node.type);
    const p = node.params ?? {};

    inspectorEl.append(
      h("div", { class: "row", style: "gap:7px" },
        h("i", { style: `width:8px;height:8px;border-radius:50%;background:${color}` }),
        h("b", {}, node.name || spec?.name || node.type)),
      h("div", { class: "card" },
        h("div", { class: "kv" }, h("span", {}, "类型"), h("b", { class: "mono" }, node.type)),
        h("div", { class: "kv" }, h("span", {}, "分类"), h("b", {}, spec?.category ?? "未知")),
        h("div", { class: "kv" }, h("span", {}, "节点 id"), h("b", { class: "mono" }, node.id))),
    );

    const nameInput = h("input", { class: "input", value: node.name ?? "" });
    nameInput.addEventListener("input", () => {
      node.name = nameInput.value;
      refreshCard(node);
    });
    inspectorEl.append(h("div", { class: "field" }, h("label", {}, "名称"), nameInput));

    if (node.type === "light") {
      const kind = asString(p.kind, "directional") === "point" ? "point" : "directional";
      inspectorEl.append(h("div", { class: "field" }, h("label", {}, "光源类型"),
        segmented(
          [{ id: "directional", label: "平行光" }, { id: "point", label: "点光" }],
          kind,
          (id) => { setParam(node, "kind", id); renderInspector(); },
        )));

      if (kind === "directional") {
        const readout = [
          h("b", { class: "mono" }, asNumber(p.dx, 0).toFixed(2)),
          h("b", { class: "mono" }, asNumber(p.dy, 0).toFixed(2)),
          h("b", { class: "mono" }, asNumber(p.dz, 0).toFixed(2)),
        ];
        const ballBox = h("div", {});
        inspectorEl.append(
          h("div", { class: "field" }, h("label", {}, "方向 · 3D 轨迹球"), ballBox),
          h("div", {
            class: "row",
            style: "justify-content:space-around;font-size:10.5px;color:var(--hls-text-muted)",
          },
            h("span", {}, "dx ", readout[0]),
            h("span", {}, "dy ", readout[1]),
            h("span", {}, "dz ", readout[2])),
          h("div", { class: "grid3" },
            slider({
              label: "dx", min: -1, max: 1, step: 0.01, value: asNumber(p.dx, -0.5),
              onInput: (v) => { setParam(node, "dx", v); readout[0].textContent = v.toFixed(2); syncBall(node); },
            }),
            slider({
              label: "dy", min: -1, max: 1, step: 0.01, value: asNumber(p.dy, -0.5),
              onInput: (v) => { setParam(node, "dy", v); readout[1].textContent = v.toFixed(2); syncBall(node); },
            }),
            slider({
              label: "dz", min: -1, max: 1, step: 0.01, value: asNumber(p.dz, 0.7),
              onInput: (v) => { setParam(node, "dz", v); readout[2].textContent = v.toFixed(2); syncBall(node); },
            })),
        );
        if (ball) {
          ballBox.append(ball.el);
          ball.set(asNumber(p.dx, -0.5), asNumber(p.dy, -0.5), asNumber(p.dz, 0.7));
          ballHandler = (dx, dy, dz) => {
            setParam(node, "dx", Math.round(dx * 1000) / 1000);
            setParam(node, "dy", Math.round(dy * 1000) / 1000);
            setParam(node, "dz", Math.round(dz * 1000) / 1000);
            readout[0].textContent = dx.toFixed(2);
            readout[1].textContent = dy.toFixed(2);
            readout[2].textContent = dz.toFixed(2);
          };
        }
      } else {
        inspectorEl.append(
          h("div", { class: "grid3" },
            slider({ label: "px", min: 0, max: 1, step: 0.01, value: asNumber(p.px, 0.5), onInput: (v) => setParam(node, "px", v) }),
            slider({ label: "py", min: 0, max: 1, step: 0.01, value: asNumber(p.py, 0.5), onInput: (v) => setParam(node, "py", v) }),
            slider({ label: "pz", min: 0, max: 3, step: 0.01, value: asNumber(p.pz, 1), onInput: (v) => setParam(node, "pz", v) })),
          h("div", { class: "small muted" }, "点光位置用归一化画布坐标 px/py 与深度 pz 表示。"),
        );
      }

      inspectorEl.append(
        slider({
          label: "强度", min: 0, max: 4, step: 0.01, value: asNumber(p.intensity, 1),
          onInput: (v) => setParam(node, "intensity", v),
        }),
        slider({
          label: "色温 (K)", min: 1500, max: 12000, step: 50, value: asNumber(p.kelvin, 5600),
          format: (v) => `${Math.round(v)}K`,
          onInput: (v) => setParam(node, "kelvin", v),
        }),
        slider({
          label: "半径", min: 0.05, max: 1.5, step: 0.01, value: asNumber(p.radius, 0.5),
          onInput: (v) => setParam(node, "radius", v),
        }),
      );
    } else if (node.type === "global") {
      const shadow = asString(p.shadow_mode, "soft") === "hard" ? "hard" : "soft";
      const mode = asString(p.lighting_mode, "linear") === "hq" ? "hq" : "linear";
      inspectorEl.append(
        slider({
          label: "环境光强度", min: 0, max: 2, step: 0.01, value: asNumber(p.ambient_intensity, 0.4),
          onInput: (v) => setParam(node, "ambient_intensity", v),
        }),
        slider({
          label: "环境光色温 (K)", min: 1500, max: 12000, step: 50, value: asNumber(p.ambient_kelvin, 6000),
          format: (v) => `${Math.round(v)}K`,
          onInput: (v) => setParam(node, "ambient_kelvin", v),
        }),
        slider({
          label: "曝光", min: 0.2, max: 2.5, step: 0.01, value: asNumber(p.exposure, 1),
          onInput: (v) => setParam(node, "exposure", v),
        }),
        h("div", { class: "field" }, h("label", {}, "阴影模式"),
          segmented([{ id: "soft", label: "柔光" }, { id: "hard", label: "硬边" }], shadow,
            (id) => { setParam(node, "shadow_mode", id); renderInspector(); })),
        h("div", { class: "field" }, h("label", {}, "光照模式"),
          segmented([{ id: "linear", label: "快速预览" }, { id: "hq", label: "高质量" }], mode,
            (id) => { setParam(node, "lighting_mode", id); renderInspector(); })),
      );
    } else if (node.type === "auto_light") {
      const ta = h("textarea", {
        class: "textarea", placeholder: "描述光照氛围，例如：黄昏左侧逆光，带青色霓虹轮廓",
      });
      ta.value = asString(p.prompt);
      ta.addEventListener("input", () => setParam(node, "prompt", ta.value));
      inspectorEl.append(
        h("div", { class: "field" }, h("label", {}, "氛围描述（求值时由本地解析器生成光源）"), ta),
        h("div", { class: "small muted" }, "求值时只使用确定性的离线解析，不会发起网络请求。"),
      );
    } else {
      inspectorEl.append(h("div", { class: "card small muted" },
        "该节点是结构节点，不修改渲染参数；它只参与图的连通与顺序。"));
    }

    inspectorEl.append(
      h("div", { class: "divider" }),
      h("div", { class: "row", style: "gap:6px" },
        button("删除节点", () => deleteSelected(), { icon: "trash", variant: "danger" }),
        button("定位视图", () => {
          const r = canvasEl.getBoundingClientRect();
          panX = r.width / 2 - (nx(node) + NODE_W / 2) * scale;
          panY = r.height / 2 - (ny(node) + NODE_H / 2) * scale;
          applyView();
        }, { icon: "crosshair", variant: "ghost" })),
      buildResultCard(),
    );
  }

  /** 用当前节点的方向值刷新轨迹球（滑块改动时调用） */
  function syncBall(node: WorkflowNode): void {
    if (!ball) return;
    const p = node.params ?? {};
    ball.set(asNumber(p.dx, 0), asNumber(p.dy, 0), asNumber(p.dz, 1));
  }

  function buildResultCard(): HTMLElement {
    const box = h("div", { class: "card", style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "row row--between" },
        h("b", { class: "small" }, "求值结果"),
        evalParams ? h("span", { class: "tiny muted" }, `${evalParams.lights.length} 个光源`) : null));
    if (!evalParams) {
      box.append(h("div", { class: "small muted" }, "尚未求值。点击顶部「求值」把节点图编译成渲染参数。"));
      return box;
    }
    if (evalPng) {
      box.append(h("img", {
        src: pngUrl(evalPng), alt: "求值预览",
        style: "width:100%;border-radius:6px;display:block;border:1px solid var(--hls-border)",
      }));
    } else {
      box.append(h("div", { class: "small muted" }, "参数已应用。当前未打开图片，无法渲染预览。"));
    }
    box.append(
      h("div", { class: "kv" }, h("span", {}, "环境光"), h("b", {}, evalParams.ambient_intensity.toFixed(2))),
      h("div", { class: "kv" }, h("span", {}, "曝光"), h("b", {}, evalParams.exposure.toFixed(2))),
      h("div", { class: "kv" }, h("span", {}, "光照模式"),
        h("b", {}, evalParams.lighting_mode === "hq" ? "高质量" : "快速预览")),
    );
    return box;
  }

  function renderStatus(): void {
    const selName = nodeById(selected)?.name;
    statusEl.textContent = [
      `${workflow.nodes.length} 节点 · ${workflow.edges.length} 连线`,
      selName ? `已选：${selName}` : (selectedEdge >= 0 ? `已选：连线 ${selectedEdge + 1}` : "未选中"),
      note,
    ].filter(Boolean).join("　|　");
  }

  // ------------------------------------------------------- 求值

  async function evaluate(): Promise<void> {
    if (busy) return;
    busy = true;
    evaluateBtn.disabled = true;
    const box = h("div", { class: "card", style: "border-color:var(--hls-border-strong);font-size:11.5px" }, "求值中…");
    inspectorEl.prepend(box);
    try {
      const res = await api.workflowEvaluate(workflow);
      if (!mounted) return;
      if (!res.ok) {
        box.style.borderColor = "#f87171";
        box.style.color = "#fca5a5";
        clear(box);
        box.append(
          h("div", { style: "font-weight:600;margin-bottom:6px" }, "工作流无法求值："),
          ...res.problems.map((item) => h("div", { style: "display:flex;gap:6px" }, h("span", {}, "·"), h("span", {}, item))),
        );
        note = `校验未通过（${res.problems.length} 项问题）`;
        renderStatus();
        return;
      }
      box.remove();
      if (!res.params) return;
      setParams(res.params);
      evalParams = res.params;
      evalPng = null;
      const imageId = store.ctx.imageId;
      if (imageId) {
        try {
          const r = await api.render(imageId, res.params);
          evalPng = r.png_b64;
          store.set({ lastRender: r.png_b64, backend: r.backend, fromCache: r.from_cache });
        } catch (e) {
          reportError(e);
        }
      }
      note = "求值成功，渲染参数已应用";
      renderInspector();
    } catch (e) {
      box.remove();
      reportError(e);
    } finally {
      busy = false;
      evaluateBtn.disabled = false;
      if (mounted) renderStatus();
    }
  }

  // ------------------------------------------------------- AI 打光

  function ensureNode(type: string, id: string, name: string, x: number, y: number): WorkflowNode {
    const found = workflow.nodes.find((n) => n.id === id);
    if (found) return found;
    const node: WorkflowNode = { id, type, name, params: defaultParams(type), x, y };
    workflow.nodes.push(node);
    return node;
  }

  async function autoLight(text: string): Promise<void> {
    const prompt = text.trim();
    if (!prompt) {
      toast("请先输入光照氛围描述", "error");
      return;
    }
    try {
      const res = await api.autolight(prompt);
      if (!mounted) return;
      const added: AutoLightNode[] = res.nodes ?? [];
      if (!added.length) {
        toast("未从描述中解析出光照节点", "error");
        return;
      }
      const r = canvasEl.getBoundingClientRect();
      const baseY = Math.round((r.height / 2 - panY) / scale) - PORT_TOP;
      const baseX = Math.round((r.width / 2 - panX) / scale) - (added.length * (NODE_W + 40)) / 2;
      const inNode = ensureNode("input", "input", "图像/视频输入", baseX - NODE_W - 60, baseY + 18);
      const outNode = ensureNode("output", "output", "输出", baseX + added.length * (NODE_W + 40) + 20, baseY + 18);

      const prevTail = workflow.edges.find((e) => e.dst === outNode.id)?.src ?? null;
      workflow.edges = workflow.edges.filter((e) => e.dst !== outNode.id);

      const created: WorkflowNode[] = added.map((n, i) => {
        const type = typeOf(n.type) ? n.type : "light";
        return {
          id: newId("ai_"),
          type,
          name: n.name || typeOf(type)?.name || "AI 光照",
          params: { ...(n.params ?? {}) },
          x: baseX + i * (NODE_W + 40),
          y: baseY,
        };
      });
      workflow.nodes.push(...created);

      const head = prevTail !== null && prevTail !== inNode.id ? nodeById(prevTail) : undefined;
      let cursor = head ?? inNode;
      for (const node of created) {
        workflow.edges.push({ src: cursor.id, dst: node.id, src_port: "out", dst_port: "in" });
        cursor = node;
      }
      workflow.edges.push({ src: cursor.id, dst: outNode.id, src_port: "out", dst_port: "in" });

      selected = created[0].id;
      selectedEdge = -1;
      renderNodes();
      renderInspector();
      fitView();
      note = `AI 打光：新增 ${created.length} 个节点`
        + (res.source === "cloud" ? "（云端）" : "（离线解析）")
        + (res.note ? ` · ${res.note}` : "");
      renderStatus();
      toast(`已生成 ${created.length} 个光照节点`, "ok");
    } catch (e) {
      reportError(e);
    }
  }

  // ------------------------------------------------------- 组装

  function buildToolbar(): HTMLElement {
    const zoomLabelEl = h("b", { class: "tiny mono", style: "min-width:42px;text-align:center" }, "100%");
    zoomLabel = zoomLabelEl;
    const jsonInput = h("input", { type: "file", accept: ".json,application/json", style: "display:none" });
    jsonInput.addEventListener("change", async () => {
      const file = jsonInput.files?.[0];
      if (!file) return;
      try {
        const parsed = parseWorkflow(JSON.parse(await file.text()));
        if (!parsed) throw new Error("JSON 结构不是合法的工作流（需要 nodes / edges 数组）");
        if (!mounted) return;
        workflow = parsed;
        selected = null;
        selectedEdge = -1;
        pendingSrc = null;
        evalParams = null;
        evalPng = null;
        renderNodes();
        renderInspector();
        fitView();
        toast(`已导入工作流：${workflow.name ?? "未命名"}`, "ok");
      } catch (e) {
        reportError(e);
      }
      jsonInput.value = "";
    });

    evaluateBtn = button("求值", () => { void evaluate(); }, { icon: "play", variant: "primary" });

    return h("div", {
      style: `height:44px;flex:0 0 44px;display:flex;align-items:center;gap:8px;padding:0 12px;
              background:var(--hls-bg-panel);border-bottom:1px solid var(--hls-border)`,
    },
      h("div", { class: "row", style: "gap:7px" },
        icon("workflow", 14, "var(--hls-accent)"), h("b", { style: "font-size:12.5px" }, "专业模式 · 节点图")),
      h("div", { style: "width:1px;height:18px;background:var(--hls-border)" }),
      h("div", { class: "row", style: "gap:4px" },
        button("", () => { scale = Math.max(0.3, scale / 1.15); applyView(); },
          { icon: "minus", variant: "icon", title: "缩小" }),
        zoomLabelEl,
        button("", () => { scale = Math.min(1.8, scale * 1.15); applyView(); },
          { icon: "plus", variant: "icon", title: "放大" }),
        button("适应视图", () => fitView(), { icon: "crosshair", variant: "ghost" })),
      h("div", { class: "spacer" }),
      h("span", { class: "tiny muted" }, "连线：输出端口 → 输入端口"),
      button("导入 JSON", () => jsonInput.click(), { icon: "upload", variant: "ghost" }),
      button("导出 JSON", () => {
        const blob = new Blob([JSON.stringify(workflow, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        downloadBlobUrl(url, `${workflow.name || "workflow"}.json`);
        setTimeout(() => URL.revokeObjectURL(url), 4000);
      }, { icon: "download", variant: "ghost" }),
      button("删除", () => deleteSelected(), { icon: "trash", variant: "danger", title: "删除选中的节点或连线" }),
      evaluateBtn,
      jsonInput,
    );
  }

  function buildCanvas(): HTMLElement {
    canvasEl = h("div", {
      style: `flex:1;min-width:0;position:relative;overflow:hidden;background:var(--hls-bg-canvas);
              background-image:
                linear-gradient(var(--hls-border) 1px, transparent 1px),
                linear-gradient(90deg, var(--hls-border) 1px, transparent 1px);
              background-size:32px 32px`,
    });
    worldEl = h("div", { style: "position:absolute;left:0;top:0;width:0;height:0;transform-origin:0 0" });

    const svg = svgEl("svg", { width: 1, height: 1 });
    svg.setAttribute("style", "position:absolute;left:0;top:0;overflow:visible;pointer-events:none");
    edgeLayer = svgEl("g", {});
    svg.append(edgeLayer);
    tempPath = svgEl("path", {
      d: "", fill: "none", stroke: "var(--hls-accent)", "stroke-width": 1.6, "stroke-dasharray": "5 4",
    });
    tempPath.style.pointerEvents = "none";
    worldEl.append(svg);
    canvasEl.append(worldEl);

    // 滚轮缩放（以光标为锚点）
    canvasEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = canvasEl.getBoundingClientRect();
      const mx = e.clientX - r.left;
      const my = e.clientY - r.top;
      const next = Math.max(0.3, Math.min(1.8, scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
      panX = mx - ((mx - panX) / scale) * next;
      panY = my - ((my - panY) / scale) * next;
      scale = next;
      applyView();
    }, { passive: false });

    // 空白处拖拽平移 / 单击取消选择
    canvasEl.addEventListener("pointerdown", (e) => {
      const target = e.target;
      if (target instanceof HTMLElement && target.closest("[data-node-id]")) return;
      const startX = e.clientX;
      const startY = e.clientY;
      const ox = panX;
      const oy = panY;
      let moved = false;
      canvasEl.style.cursor = "grabbing";
      const onMove = (ev: PointerEvent): void => {
        moved = true;
        panX = ox + (ev.clientX - startX);
        panY = oy + (ev.clientY - startY);
        applyView();
      };
      const onUp = (): void => {
        canvasEl.style.cursor = "default";
        canvasEl.removeEventListener("pointermove", onMove);
        canvasEl.removeEventListener("pointerup", onUp);
        if (!moved) {
          pendingSrc = null;
          setTempPath("");
          selectOnCanvas(null);
        }
      };
      canvasEl.addEventListener("pointermove", onMove);
      canvasEl.addEventListener("pointerup", onUp);
      try {
        canvasEl.setPointerCapture(e.pointerId);
      } catch { /* 忽略：监听已挂好 */ }
    });

    // 连线待定：虚线跟随光标
    canvasEl.addEventListener("pointermove", (e) => {
      const src = nodeById(pendingSrc?.id ?? null);
      if (!src || !pendingSrc) return;
      const r = canvasEl.getBoundingClientRect();
      const x = (e.clientX - r.left - panX) / scale;
      const y = (e.clientY - r.top - panY) / scale;
      const a = portPos(src, "out", pendingSrc.port);
      setTempPath(edgePath(a.x, a.y, x, y));
    });

    // 双击空白处快速添加光源节点
    canvasEl.addEventListener("dblclick", (e) => {
      const target = e.target;
      if (target instanceof HTMLElement && target.closest("[data-node-id]")) return;
      const r = canvasEl.getBoundingClientRect();
      addNode("light",
        Math.round((e.clientX - r.left - panX) / scale - NODE_W / 2),
        Math.round((e.clientY - r.top - panY) / scale - NODE_H / 2));
    });

    return canvasEl;
  }

  function buildLeftPanel(): HTMLElement {
    const search = h("input", { class: "input", placeholder: "搜索节点…", type: "search" });
    search.addEventListener("input", () => {
      paletteQuery = search.value;
      renderPalette();
    });
    paletteEl = h("div", { style: "display:flex;flex-direction:column;gap:2px" });
    return h("div", { class: "panel", style: "width:248px;flex:0 0 248px" },
      h("div", { class: "panel__head" }, icon("layers", 13), "节点库"),
      h("div", { class: "panel__body", style: "gap:8px" }, search, paletteEl),
      h("div", { style: "padding:0 14px 14px;margin-top:auto" },
        h("div", { class: "card", style: "font-size:10.5px;line-height:1.5" },
          h("b", { style: "display:block;color:var(--hls-text-secondary);margin-bottom:3px" }, "插件管理"),
          h("span", { class: "muted" }, "自定义节点与光源插件的入口在左侧导航「插件中心」。"))),
    );
  }

  function buildRightPanel(): HTMLElement {
    inspectorEl = h("div", { class: "panel__body" });
    return h("div", { class: "panel panel--right", style: "width:320px;flex:0 0 320px" },
      h("div", { class: "panel__head" }, icon("sliders", 13), "节点属性"),
      inspectorEl,
    );
  }

  function buildAiBar(): HTMLElement {
    const input = h("input", {
      class: "input",
      placeholder: "描述光照氛围，例如：「黄昏时分，暖色光从左后方打来，轮廓带一点青色霓虹」…",
      style: "flex:1",
    });
    const run = button("生成光照节点", () => {
      const text = input.value;
      void autoLight(text).then(() => { input.value = ""; });
    }, { icon: "sparkles", variant: "primary" });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") run.click();
    });
    return h("div", {
      style: `height:66px;flex:0 0 66px;display:flex;align-items:center;gap:10px;padding:0 14px;
              background:var(--hls-bg-panel);border-top:1px solid var(--hls-border)`,
    },
      h("div", { class: "row", style: "gap:6px;color:var(--hls-accent)" },
        icon("sparkles", 15), h("b", { style: "font-size:12px" }, "AI 打光")),
      input,
      run,
    );
  }

  function buildStatusLine(): HTMLElement {
    statusEl = h("div", { class: "ellipsis", style: "flex:1;min-width:0" }, "正在加载节点类型…");
    return h("div", {
      style: `height:26px;flex:0 0 26px;display:flex;align-items:center;gap:10px;padding:0 14px;
              background:var(--hls-bg-panel);border-top:1px solid var(--hls-border);
              color:var(--hls-text-muted);font-size:10.5px`,
    }, statusEl, h("span", { class: "tiny muted" }, "双击画布空白处可快速添加光源节点"));
  }

  // ------------------------------------------------------- 生命周期

  return {
    id: "promode",
    name: "专业模式",
    icon: "workflow",
    mount(root: HTMLElement): void {
      mounted = true;
      types = [];
      workflow = { nodes: [], edges: [], name: "专业模式工作流" };
      selected = null;
      selectedEdge = -1;
      pendingSrc = null;
      evalPng = null;
      evalParams = null;
      note = "";
      paletteQuery = "";
      scale = 1;
      panX = 24;
      panY = 24;
      uid = 1;
      ballHandler = () => {};
      ball = new Trackball(0, 0, 1, (dx, dy, dz) => ballHandler(dx, dy, dz));

      const wrap = h("div", {
        style: "flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--hls-bg-root)",
      });
      wrap.append(
        buildToolbar(),
        h("div", { style: "flex:1;min-height:0;display:flex" },
          buildLeftPanel(), buildCanvas(), buildRightPanel()),
        buildAiBar(),
        buildStatusLine(),
      );
      root.append(wrap);

      onKey = (e: KeyboardEvent): void => {
        const t = e.target;
        if (t instanceof HTMLElement && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
        if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          deleteSelected();
        } else if (e.key === "Escape") {
          pendingSrc = null;
          setTempPath("");
          renderNodes();
        }
      };
      document.addEventListener("keydown", onKey);

      store.set({ status: "专业模式 · 正在加载节点类型" });
      void (async () => {
        try {
          const res = await api.workflowTypes();
          if (!mounted) return;
          types = res.node_types ?? [];
          workflow = parseWorkflow(res.default) ?? { nodes: [], edges: [], name: "专业模式工作流" };
          renderNodes();
          renderPalette();
          renderInspector();
          fitView();
          store.set({ status: `专业模式 · ${types.length} 种节点类型` });
        } catch (e) {
          if (!mounted) return;
          reportError(e);
          if (statusEl) statusEl.textContent = "节点类型加载失败，请确认本地服务仍在运行。";
        }
      })();
    },

    unmount(): void {
      mounted = false;
      if (onKey) document.removeEventListener("keydown", onKey);
      onKey = null;
      ball = null;
      cards.clear();
    },
  };
}
