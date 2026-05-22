// tt-prism editor — vanilla JS/SVG client.
//
// State: `state.diagram` is the authoritative mutable copy of the diagram on disk.
// Rendering is a full re-render of the SVG on each mutation (cheap at typical sizes).

const PALETTE = ["#ef9a9a","#90caf9","#a5d6a7","#ffcc80","#ce93d8","#80cbc4","#fff59d","#bcaaa4"];

const state = {
  diagram: null,
  path: null,
  pxPerClock: 0.6,
  laneHeight: 72,
  laneGutter: 8,
  topMargin: 56,
  leftMargin: 140,
  rightMargin: 24,
  bottomMargin: 24,
  selection: new Set(),
  dirty: false,
  depSource: null,
  depMode: false,
  tagFilter: "",
  clipboard: null,
};

// -------- layout helpers (mirror of Python Layout) --------
function xOfClock(c) { return state.leftMargin + c * state.pxPerClock; }
function yLaneTop(order) { return state.topMargin + order * (state.laneHeight + state.laneGutter); }
function yLaneCenter(order) { return yLaneTop(order) + state.laneHeight / 2; }
function snapClock(c, g) { const gg = Math.max(1, g); return Math.round(c / gg) * gg; }

// Cubic Bezier path from (x1,y1) to (x2,y2) — mirrors tt_prism.renderer.svg._bezier_path.
// Tangent strategy: horizontal at both ends for forward with gap, vertical for
// overlap (x1 == x2), big lift above top lane for back-edges.
function bezierPath(x1, y1, x2, y2, forward, orderSrc, orderTgt) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  let c1x, c1y, c2x, c2y;
  if (!forward) {
    const topOrder = Math.min(orderSrc, orderTgt);
    const topY = yLaneTop(topOrder) - 28;
    const lift = Math.max(30, Math.abs(x1 - x2) * 0.25 + 24);
    c1x = x1 + lift; c1y = topY;
    c2x = x2 - lift; c2y = topY;
  } else if (Math.abs(dx) < 1e-3) {
    const t = Math.max(10, Math.abs(dy) * 0.35);
    const sign = dy >= 0 ? 1 : -1;
    c1x = x1; c1y = y1 + t * sign;
    c2x = x2; c2y = y2 - t * sign;
  } else {
    const t = dx >= 0 ? Math.max(18, dx * 0.45) : Math.max(14, -dx * 0.2 + 14);
    c1x = x1 + t; c1y = y1;
    c2x = x2 - t; c2y = y2;
  }
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} `
       + `C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, `
       + `${c2x.toFixed(1)} ${c2y.toFixed(1)}, `
       + `${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

function computeDepOffsets() {
  const deps = state.diagram.dependencies;
  const barH = Math.max(16, state.laneHeight - 16);
  const outLists = new Map(), inLists = new Map();
  deps.forEach((d, idx) => {
    const a = itemById(d.from), b = itemById(d.to);
    if (!a || !b) return;
    const la = laneById(a.lane_id), lb = laneById(b.lane_id);
    if (!la || !lb) return;
    if (!outLists.has(d.from)) outLists.set(d.from, []);
    if (!inLists.has(d.to)) inLists.set(d.to, []);
    outLists.get(d.from).push({ idx, sort: [lb.order, b.start_clock] });
    inLists.get(d.to).push({ idx, sort: [la.order, a.start_clock] });
  });
  const outRank = new Map(), inRank = new Map();
  for (const items of outLists.values()) {
    items.sort((a, b) => a.sort[0] - b.sort[0] || a.sort[1] - b.sort[1]);
    items.forEach((it, rank) => outRank.set(it.idx, [rank, items.length]));
  }
  for (const items of inLists.values()) {
    items.sort((a, b) => a.sort[0] - b.sort[0] || a.sort[1] - b.sort[1]);
    items.forEach((it, rank) => inRank.set(it.idx, [rank, items.length]));
  }
  return deps.map((_, idx) => {
    const [ro, no] = outRank.get(idx) || [0, 1];
    const [ri, ni] = inRank.get(idx) || [0, 1];
    const stepO = Math.min(12, barH / (no + 1));
    const stepI = Math.min(12, barH / (ni + 1));
    return {
      srcDy: (ro - (no - 1) / 2) * stepO,
      tgtDy: (ri - (ni - 1) / 2) * stepI,
    };
  });
}

function laneById(id) { return state.diagram.lanes.find(l => l.id === id); }
function resourceById(id) { return state.diagram.resources.find(r => r.id === id); }
function itemById(id) { return state.diagram.work_items.find(w => w.id === id); }
function resourceColor(item, idx) {
  const r = resourceById(item.resource_id);
  if (r) return r.color;
  return PALETTE[idx % PALETTE.length];
}
function totalClocks() {
  const items = state.diagram.work_items;
  const g = Math.max(1, state.diagram.grid_clocks);
  let total = g;
  for (const w of items) total = Math.max(total, w.start_clock + w.duration_clocks);
  return Math.ceil(total / g) * g;
}
function canvasSize() {
  const total = totalClocks();
  const width = xOfClock(total) + state.rightMargin;
  const maxOrder = state.diagram.lanes.reduce((m, l) => Math.max(m, l.order), 0);
  const height = yLaneTop(maxOrder) + state.laneHeight + state.bottomMargin;
  return { width, height, total };
}

// -------- rendering --------
function svgNs(tag, attrs = {}, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}

function render() {
  const root = document.getElementById("canvas");
  root.innerHTML = "";
  const { width, height, total } = canvasSize();

  const svg = svgNs("svg", {
    xmlns: "http://www.w3.org/2000/svg",
    width, height,
    viewBox: `0 0 ${width} ${height}`,
  });
  svg.addEventListener("click", onBackgroundClick);

  // defs
  const defs = svgNs("defs");
  defs.innerHTML = `
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#444"/>
    </marker>`;
  svg.appendChild(defs);

  svg.appendChild(svgNs("rect", { x: 0, y: 0, width, height, fill: "#fafafa" }));

  // title
  const title = svgNs("text", {
    x: width / 2, y: 28, "font-size": 16, "font-weight": 600,
    "text-anchor": "middle", fill: "#222",
  }, state.diagram.title);
  svg.appendChild(title);

  // grid
  const g = Math.max(1, state.diagram.grid_clocks);
  const minor = Math.max(1, Math.floor(g / 4));
  const top = state.topMargin - 8;
  const bottom = height - state.bottomMargin;
  for (let c = 0; c <= total; c += minor) {
    if (c % g === 0) continue;
    const x = xOfClock(c);
    svg.appendChild(svgNs("line", { x1: x, y1: top, x2: x, y2: bottom, stroke: "#eee" }));
  }
  for (let c = 0; c <= total; c += g) {
    const x = xOfClock(c);
    svg.appendChild(svgNs("line", { x1: x, y1: top, x2: x, y2: bottom, stroke: "#d0d0d0" }));
  }
  // Labels only at a stride that keeps them from overlapping.
  const maxChars = Math.max(2, String(total).length);
  const minPx = maxChars * 7 + 12;
  const pxPerMajor = g * state.pxPerClock;
  const strideTicks = pxPerMajor > 0 ? Math.max(1, Math.ceil(minPx / pxPerMajor)) : 1;
  for (let c = 0; c <= total; c += g * strideTicks) {
    const x = xOfClock(c);
    svg.appendChild(svgNs("text", {
      x, y: state.topMargin - 20, "font-size": 10, "text-anchor": "middle", fill: "#666",
    }, String(c)));
    if (state.diagram.clock_ghz != null) {
      const ns = (c / state.diagram.clock_ghz).toFixed(1);
      svg.appendChild(svgNs("text", {
        x, y: state.topMargin - 8, "font-size": 10, "text-anchor": "middle", fill: "#999",
      }, `${ns} ns`));
    }
  }

  // lanes
  const lanes = [...state.diagram.lanes].sort((a, b) => a.order - b.order);
  for (const lane of lanes) {
    const y = yLaneTop(lane.order);
    svg.appendChild(svgNs("rect", {
      x: state.leftMargin, y,
      width: width - state.leftMargin - state.rightMargin,
      height: state.laneHeight,
      fill: "#ffffff", stroke: "#d6d6d6",
    }));
    svg.appendChild(svgNs("text", {
      x: state.leftMargin - 12, y: y + state.laneHeight / 2 + 4,
      "font-size": 13, "font-weight": 600, "text-anchor": "end", fill: "#333",
    }, lane.name));
  }

  // work items
  state.diagram.work_items.forEach((item, idx) => {
    const lane = laneById(item.lane_id);
    if (!lane) return;
    const x = xOfClock(item.start_clock);
    const w = Math.max(2, item.duration_clocks * state.pxPerClock);
    const y = yLaneTop(lane.order) + 8;
    const h = state.laneHeight - 16;
    const group = svgNs("g", { "data-item-id": item.id, class: "work-item" });
    if (state.selection.has(item.id)) group.classList.add("selected");
    if (state.depSource === item.id) group.classList.add("dep-source");
    if (itemMatchesFilter(item)) group.classList.add("highlight");

    const t = svgNs("title", {}, itemTooltip(item));
    group.appendChild(t);
    group.appendChild(svgNs("rect", {
      x, y, width: w, height: h, rx: 6, ry: 6,
      fill: resourceColor(item, idx), stroke: "#555",
    }));
    group.appendChild(svgNs("text", {
      x: x + 6, y: y + h / 2 + 4, "font-size": 11, fill: "#222",
    }, item.label || item.id));

    group.addEventListener("mousedown", (e) => onItemMouseDown(e, item));
    group.addEventListener("click", (e) => onItemClick(e, item));
    group.addEventListener("mouseenter", () => highlightArrowsFor(item.id));
    group.addEventListener("mouseleave", () => highlightArrowsForSelection());
    svg.appendChild(group);
  });

  // dependencies
  const depOffsets = computeDepOffsets();
  state.diagram.dependencies.forEach((dep, idx) => {
    const a = itemById(dep.from);
    const b = itemById(dep.to);
    if (!a || !b) return;
    const la = laneById(a.lane_id);
    const lb = laneById(b.lane_id);
    if (!la || !lb) return;
    const { srcDy, tgtDy } = depOffsets[idx] || { srcDy: 0, tgtDy: 0 };
    const forward = b.start_clock >= a.start_clock;
    const ySrcC = yLaneCenter(la.order);
    const yTgtC = yLaneCenter(lb.order);
    const xSrcEnd = xOfClock(a.start_clock + a.duration_clocks);
    const xTgtStart = xOfClock(b.start_clock);

    let x1, y1, x2, y2;
    if (forward && b.start_clock < a.start_clock + a.duration_clocks && la.order !== lb.order) {
      const halfH = state.laneHeight / 2 - 2;
      if (lb.order > la.order) { y1 = ySrcC + halfH + srcDy; y2 = yTgtC - halfH + tgtDy; }
      else                     { y1 = ySrcC - halfH + srcDy; y2 = yTgtC + halfH + tgtDy; }
      x1 = xTgtStart; x2 = xTgtStart;
    } else {
      x1 = xSrcEnd; y1 = ySrcC + srcDy;
      x2 = xTgtStart; y2 = yTgtC + tgtDy;
    }
    const d = bezierPath(x1, y1, x2, y2, forward, la.order, lb.order);
    const stroke = dep.kind === "fifo" ? "#1976d2" : dep.kind === "flow" ? "#2e7d32" : "#444";
    const dash = dep.kind === "flow" ? "4 3" : "";
    const path = svgNs("path", {
      class: "dep-arrow",
      "data-from": dep.from,
      "data-to": dep.to,
      d, fill: "none", stroke, "stroke-width": 1.4,
      "marker-end": "url(#arrow)",
    });
    if (dash) path.setAttribute("stroke-dasharray", dash);
    svg.appendChild(path);
  });

  root.appendChild(svg);
  highlightArrowsForSelection();
  updateInspector();
  updateSelectionInfo();
  updateStatus();
}

// Dim all arrows except those connected to `ids`; add a `canvas-focused` class
// on the canvas so non-related arrows fade further.
function highlightArrows(ids) {
  const canvas = document.getElementById("canvas");
  if (!canvas) return;
  const set = new Set(ids || []);
  const arrows = canvas.querySelectorAll(".dep-arrow");
  let any = false;
  for (const a of arrows) {
    const related = set.has(a.getAttribute("data-from")) || set.has(a.getAttribute("data-to"));
    a.classList.toggle("related", related);
    if (related) any = true;
  }
  canvas.classList.toggle("canvas-focused", any);
}

function highlightArrowsFor(id) { highlightArrows([id]); }
function highlightArrowsForSelection() { highlightArrows([...state.selection]); }

function itemTooltip(item) {
  const parts = [item.id];
  if (item.label && item.label !== item.id) parts.push(item.label);
  parts.push(`start=${item.start_clock}clk dur=${item.duration_clocks}clk end=${item.start_clock + item.duration_clocks}clk`);
  if (state.diagram.clock_ghz != null) {
    parts.push(`(${(item.duration_clocks / state.diagram.clock_ghz).toFixed(2)} ns)`);
  }
  if (item.resource_id) parts.push(`resource=${item.resource_id}`);
  if (item.tags && item.tags.length) parts.push(`tags=${item.tags.join(",")}`);
  return parts.join(" | ");
}

function itemMatchesFilter(item) {
  const f = state.tagFilter.trim();
  if (!f) return false;
  if (!item.tags) return false;
  return item.tags.includes(f);
}

// -------- drag & dependency mode --------
function onItemMouseDown(e, item) {
  if (state.depMode) return; // click, not drag, in link mode
  if (e.button !== 0) return;
  e.preventDefault();
  if (!state.selection.has(item.id) && !e.shiftKey) {
    state.selection.clear();
    state.selection.add(item.id);
    render();
  } else if (!state.selection.has(item.id) && e.shiftKey) {
    state.selection.add(item.id);
    render();
  }

  // Grab the *live* SVG after any render() above — e.currentTarget's ownerSVGElement
  // points at the now-detached old SVG, whose getScreenCTM() returns null and would
  // make coordinate math collapse to (0,0), causing teleport-to-left.
  const svg = document.querySelector("#canvas svg");
  if (!svg) return;
  const pt = svg.createSVGPoint();
  function loc(ev) {
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    pt.x = ev.clientX; pt.y = ev.clientY;
    const p = pt.matrixTransform(ctm.inverse());
    return { x: p.x, y: p.y };
  }
  const start = loc(e);
  if (!start) return;
  const startClocks = new Map();
  const startOrders = new Map();
  for (const id of state.selection) {
    const w = itemById(id);
    if (w) {
      startClocks.set(id, w.start_clock);
      const lane = laneById(w.lane_id);
      startOrders.set(id, lane ? lane.order : 0);
    }
  }

  const sortedLanes = [...state.diagram.lanes].sort((a, b) => a.order - b.order);

  function move(ev) {
    // Re-query the current live SVG each frame: render() rebuilds it on every mutation.
    const liveSvg = document.querySelector("#canvas svg");
    if (!liveSvg) return;
    const ctm = liveSvg.getScreenCTM();
    if (!ctm) return;
    const p2 = liveSvg.createSVGPoint();
    p2.x = ev.clientX; p2.y = ev.clientY;
    const p = p2.matrixTransform(ctm.inverse());
    const dxClocks = (p.x - start.x) / state.pxPerClock;
    const dyLanes = Math.round((p.y - start.y) / (state.laneHeight + state.laneGutter));
    for (const id of state.selection) {
      const w = itemById(id);
      if (!w) continue;
      const raw = startClocks.get(id) + dxClocks;
      w.start_clock = Math.max(0, snapClock(raw, state.diagram.grid_clocks));
      const newIdx = Math.min(sortedLanes.length - 1,
        Math.max(0, (sortedLanes.findIndex(l => l.order === startOrders.get(id))) + dyLanes));
      const newLane = sortedLanes[newIdx];
      if (newLane) w.lane_id = newLane.id;
    }
    state.dirty = true;
    render();
  }
  function up() {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  }
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

function onItemClick(e, item) {
  e.stopPropagation();
  if (state.depMode) {
    if (!state.depSource) {
      state.depSource = item.id;
      render();
      return;
    }
    if (state.depSource === item.id) {
      state.depSource = null;
      render();
      return;
    }
    state.diagram.dependencies.push({ from: state.depSource, to: item.id, kind: "dep", label: "" });
    state.depSource = null;
    state.dirty = true;
    render();
    return;
  }
  if (e.shiftKey) {
    if (state.selection.has(item.id)) state.selection.delete(item.id);
    else state.selection.add(item.id);
  } else {
    state.selection.clear();
    state.selection.add(item.id);
  }
  render();
}

function onBackgroundClick() {
  state.selection.clear();
  state.depSource = null;
  render();
}

// -------- inspector --------
function updateInspector() {
  const form = document.getElementById("insp-form");
  const empty = document.getElementById("insp-empty");
  if (state.selection.size !== 1) {
    form.hidden = true;
    empty.hidden = false;
    empty.textContent = state.selection.size === 0
      ? "Select a work item to edit its properties. Shift-click to multi-select."
      : `${state.selection.size} items selected — use "Batch edit…" to modify.`;
    return;
  }
  empty.hidden = true;
  form.hidden = false;
  const id = [...state.selection][0];
  const item = itemById(id);
  if (!item) return;
  form.id.value = item.id;
  form.label.value = item.label || "";

  const laneSel = form.lane_id;
  laneSel.innerHTML = "";
  for (const l of state.diagram.lanes) {
    const o = document.createElement("option");
    o.value = l.id; o.textContent = l.name;
    if (l.id === item.lane_id) o.selected = true;
    laneSel.appendChild(o);
  }
  const resSel = form.resource_id;
  resSel.innerHTML = '<option value="">(none)</option>';
  for (const r of state.diagram.resources) {
    const o = document.createElement("option");
    o.value = r.id; o.textContent = r.name;
    if (r.id === item.resource_id) o.selected = true;
    resSel.appendChild(o);
  }
  form.start_clock.value = item.start_clock;
  form.duration_clocks.value = item.duration_clocks;
  form.tags.value = (item.tags || []).join(",");
}

function updateSelectionInfo() {
  const el = document.getElementById("sel-info");
  if (state.selection.size === 0) { el.textContent = "No selection."; return; }
  const ids = [...state.selection];
  const items = ids.map(itemById).filter(Boolean);
  const tagCounts = {};
  for (const it of items) for (const t of it.tags || []) tagCounts[t] = (tagCounts[t] || 0) + 1;
  const tags = Object.entries(tagCounts).map(([t, n]) => `${t}×${n}`).join(", ") || "—";
  el.innerHTML = `${items.length} item(s).<br/>Tags: ${tags}`;
}

function updateStatus() {
  document.getElementById("status").textContent = state.dirty ? "• unsaved changes" : "saved";
}

// -------- form submit --------
function onInspectorSubmit(ev) {
  ev.preventDefault();
  if (state.selection.size !== 1) return;
  const id = [...state.selection][0];
  const item = itemById(id);
  if (!item) return;
  const fd = new FormData(ev.target);
  const newId = fd.get("id").toString().trim();
  if (newId !== item.id) {
    if (state.diagram.work_items.some(w => w.id === newId)) {
      alert("Duplicate id: " + newId); return;
    }
    // rewrite dependency refs
    for (const dep of state.diagram.dependencies) {
      if (dep.from === item.id) dep.from = newId;
      if (dep.to === item.id) dep.to = newId;
    }
    state.selection.delete(item.id);
    state.selection.add(newId);
    item.id = newId;
  }
  item.label = fd.get("label") || "";
  item.lane_id = fd.get("lane_id");
  const res = fd.get("resource_id");
  item.resource_id = res ? res : null;
  item.start_clock = Math.max(0, parseInt(fd.get("start_clock"), 10) || 0);
  item.duration_clocks = Math.max(1, parseInt(fd.get("duration_clocks"), 10) || 1);
  const tags = (fd.get("tags") || "").toString().split(",").map(s => s.trim()).filter(Boolean);
  item.tags = tags;
  state.dirty = true;
  render();
}

// -------- add/delete --------
function nextId(prefix) {
  let i = state.diagram.work_items.length;
  while (state.diagram.work_items.some(w => w.id === `${prefix}${i}`)) i++;
  return `${prefix}${i}`;
}
function addItem() {
  if (state.diagram.lanes.length === 0) { alert("Add a lane first."); return; }
  const id = nextId("w");
  const it = {
    id, lane_id: state.diagram.lanes[0].id, resource_id: null,
    label: id, start_clock: 0, duration_clocks: state.diagram.grid_clocks,
    tags: [],
  };
  state.diagram.work_items.push(it);
  state.selection.clear();
  state.selection.add(id);
  state.dirty = true;
  render();
}
function deleteSelection() {
  const ids = new Set(state.selection);
  state.diagram.work_items = state.diagram.work_items.filter(w => !ids.has(w.id));
  state.diagram.dependencies = state.diagram.dependencies.filter(d => !ids.has(d.from) && !ids.has(d.to));
  state.selection.clear();
  state.dirty = true;
  render();
}

// Copy/paste: clipboard holds a *deep snapshot* of the selected items + the
// dependencies whose endpoints are both inside the selection (intra-selection
// edges). Paste re-keys IDs and offsets clocks so the new items don't collide
// with the originals, and applies the same offset to subsequent pastes.
function copySelection() {
  const ids = [...state.selection];
  if (ids.length === 0) return;
  const items = ids.map(itemById).filter(Boolean).map(w => JSON.parse(JSON.stringify(w)));
  const idSet = new Set(ids);
  const deps = state.diagram.dependencies
    .filter(d => idSet.has(d.from) && idSet.has(d.to))
    .map(d => JSON.parse(JSON.stringify(d)));
  state.clipboard = { items, deps };
  flashStatus(`Copied ${items.length} item(s)`);
}

function pasteClipboard() {
  if (!state.clipboard || state.clipboard.items.length === 0) return;
  const offsetClocks = Math.max(1, state.diagram.grid_clocks);
  // Find a non-colliding shift along the time axis by checking existing IDs.
  const idMap = new Map();
  const newItems = state.clipboard.items.map(src => {
    const newId = uniqueId(src.id);
    idMap.set(src.id, newId);
    return {
      ...JSON.parse(JSON.stringify(src)),
      id: newId,
      start_clock: src.start_clock + offsetClocks,
    };
  });
  const newDeps = state.clipboard.deps.map(d => ({
    ...JSON.parse(JSON.stringify(d)),
    from: idMap.get(d.from),
    to: idMap.get(d.to),
  }));
  state.diagram.work_items.push(...newItems);
  state.diagram.dependencies.push(...newDeps);
  state.selection = new Set(newItems.map(w => w.id));
  state.dirty = true;
  render();
  flashStatus(`Pasted ${newItems.length} item(s)`);
}

function uniqueId(baseId) {
  if (!state.diagram.work_items.some(w => w.id === baseId)) return baseId;
  // Strip any existing _N suffix, then find the next free integer.
  const m = baseId.match(/^(.*?)(?:_(\d+))?$/);
  const stem = m[1] || baseId;
  let n = parseInt(m[2] || "1", 10);
  while (state.diagram.work_items.some(w => w.id === `${stem}_${n}`)) n++;
  return `${stem}_${n}`;
}

function flashStatus(msg) {
  const el = document.getElementById("status");
  if (!el) return;
  const prev = el.textContent;
  el.textContent = msg;
  clearTimeout(flashStatus._t);
  flashStatus._t = setTimeout(() => updateStatus(), 1200);
}

function addLane() {
  const name = prompt("New lane name (e.g. TRISC 3):", "TRISC " + state.diagram.lanes.length);
  if (!name) return;
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "").replace(/(^_|_$)/g, "") || `lane${state.diagram.lanes.length}`;
  const order = state.diagram.lanes.reduce((m, l) => Math.max(m, l.order), -1) + 1;
  state.diagram.lanes.push({ id, name, order });
  state.dirty = true;
  render();
}
function addResource() {
  const name = prompt("New resource name (e.g. FPU):");
  if (!name) return;
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "") || `r${state.diagram.resources.length}`;
  const color = PALETTE[state.diagram.resources.length % PALETTE.length];
  state.diagram.resources.push({ id, name, color });
  state.dirty = true;
  render();
}

// -------- batch edit --------
function openBatchDialog() {
  if (state.selection.size === 0) { alert("Select items first (use shift-click, or a tag filter + ctrl-A-like 'Select all matching' below)."); return; }
  const dlg = document.getElementById("dlg-batch");
  const resSel = document.getElementById("dlg-resource");
  resSel.innerHTML = '<option value="">(unchanged)</option>';
  for (const r of state.diagram.resources) {
    const o = document.createElement("option");
    o.value = r.id; o.textContent = r.name;
    resSel.appendChild(o);
  }
  document.getElementById("dlg-shift").value = "0";
  document.getElementById("dlg-add-tag").value = "";
  document.getElementById("dlg-remove-tag").value = "";
  document.getElementById("dlg-batch-msg").textContent =
    `Editing ${state.selection.size} item(s).`;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", onBatchClose, { once: true });
}
function onBatchClose() {
  const dlg = document.getElementById("dlg-batch");
  if (dlg.returnValue !== "apply") return;
  const shift = parseInt(document.getElementById("dlg-shift").value, 10) || 0;
  const setRes = document.getElementById("dlg-resource").value;
  const addTag = document.getElementById("dlg-add-tag").value.trim();
  const rmTag = document.getElementById("dlg-remove-tag").value.trim();
  for (const id of state.selection) {
    const w = itemById(id);
    if (!w) continue;
    if (shift) w.start_clock = Math.max(0, w.start_clock + shift);
    if (setRes) w.resource_id = setRes;
    w.tags = w.tags || [];
    if (addTag && !w.tags.includes(addTag)) w.tags.push(addTag);
    if (rmTag) w.tags = w.tags.filter(t => t !== rmTag);
  }
  state.dirty = true;
  render();
}

function selectAllMatching() {
  const f = state.tagFilter.trim();
  if (!f) return;
  state.selection.clear();
  for (const w of state.diagram.work_items) {
    if ((w.tags || []).includes(f)) state.selection.add(w.id);
  }
  render();
}

// -------- persistence --------
async function loadFromServer() {
  const r = await fetch("/api/diagram");
  if (!r.ok) { alert("Failed to load"); return; }
  const body = await r.json();
  state.diagram = body.diagram;
  state.path = body.path;
  // normalize missing fields
  for (const w of state.diagram.work_items) if (!w.tags) w.tags = [];
  state.selection.clear();
  state.dirty = false;
  // sync toolbar
  document.getElementById("grid-clocks").value = state.diagram.grid_clocks;
  document.getElementById("px-per-clock").value = state.pxPerClock;
  render();
}

async function saveToServer() {
  const r = await fetch("/api/diagram", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ diagram: state.diagram }),
  });
  if (!r.ok) {
    const txt = await r.text();
    alert("Save failed: " + txt);
    return;
  }
  state.dirty = false;
  updateStatus();
}

function exportSvg() {
  const svg = document.querySelector("#canvas svg");
  if (!svg) return;
  const blob = new Blob([svg.outerHTML], { type: "image/svg+xml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (state.diagram.title || "diagram").replace(/[^a-z0-9]+/gi, "_") + ".svg";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// -------- wiring --------
function wireToolbar() {
  document.getElementById("btn-add-item").addEventListener("click", addItem);
  document.getElementById("btn-add-lane").addEventListener("click", addLane);
  document.getElementById("btn-add-resource").addEventListener("click", addResource);
  document.getElementById("btn-copy").addEventListener("click", copySelection);
  document.getElementById("btn-paste").addEventListener("click", pasteClipboard);
  document.getElementById("btn-duplicate").addEventListener("click", () => { copySelection(); pasteClipboard(); });
  document.getElementById("btn-save").addEventListener("click", saveToServer);
  document.getElementById("btn-reload").addEventListener("click", () => {
    if (state.dirty && !confirm("Discard unsaved changes?")) return;
    loadFromServer();
  });
  document.getElementById("btn-export-svg").addEventListener("click", exportSvg);
  document.getElementById("btn-batch-edit").addEventListener("click", openBatchDialog);
  document.getElementById("btn-delete-item").addEventListener("click", deleteSelection);
  document.getElementById("insp-form").addEventListener("submit", onInspectorSubmit);

  document.getElementById("dep-mode").addEventListener("change", (e) => {
    state.depMode = e.target.checked;
    state.depSource = null;
    render();
  });
  document.getElementById("grid-clocks").addEventListener("change", (e) => {
    const v = Math.max(1, parseInt(e.target.value, 10) || 1);
    state.diagram.grid_clocks = v; state.dirty = true; render();
  });
  document.getElementById("px-per-clock").addEventListener("change", (e) => {
    const v = Math.max(0.05, parseFloat(e.target.value) || 0.6);
    state.pxPerClock = v; render();
  });
  const tagInput = document.getElementById("tag-filter");
  tagInput.addEventListener("input", (e) => { state.tagFilter = e.target.value; render(); });
  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { selectAllMatching(); }
  });

  window.addEventListener("keydown", (e) => {
    const inField = ["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName);
    if ((e.key === "Delete" || e.key === "Backspace") && state.selection.size > 0 && !inField) {
      e.preventDefault();
      deleteSelection();
    }
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === "s") { e.preventDefault(); saveToServer(); }
    if (mod && (e.key === "c" || e.key === "C") && !inField) { e.preventDefault(); copySelection(); }
    if (mod && (e.key === "v" || e.key === "V") && !inField) { e.preventDefault(); pasteClipboard(); }
    if (mod && (e.key === "d" || e.key === "D") && !inField) {
      // Cmd/Ctrl-D: duplicate-in-place (copy + paste in one motion)
      e.preventDefault(); copySelection(); pasteClipboard();
    }
  });
  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });
}

wireToolbar();
loadFromServer();
