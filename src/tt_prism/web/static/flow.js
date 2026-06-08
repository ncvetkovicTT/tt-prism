// tt-prism flow view — vanilla JS, no build step.
//
// Left: the sequence of ops (execute ops are selectable; init ops are greyed).
// Center: for the selected op, the Tensix storage stages (Input L1 / SrcA /
// SrcB / DEST / SFPU / Output L1) as columns, with tile tokens that hop between
// stages as you step through the op's dataflow. Right: the op's step list.
//
// Scheduling/derivation happens server-side (/api/flow). This file only paints.

const DATUM_COLORS = ["#0f766e", "#b45309", "#b91c1c", "#4d7c0f", "#6d28d9", "#0369a1", "#9d174d", "#115e59"];

const state = {
  data: null,        // /api/flow payload
  op: null,          // selected op object (single-op mode)
  step: 0,
  mode: "op",        // "op" | "chip"
  chip: null,        // chip payload {multicore, cores[], interactions[]}
  noc: 0,            // current interaction index (chip mode)
  playing: false,
  timer: null,
  datums: [],        // [{name, origin, produced_at, el}] — one labeled token per datum
};

async function load() {
  const r = await fetch("/api/flow");
  if (!r.ok) { document.getElementById("op-list").textContent = "Failed to load."; return; }
  state.data = await r.json();
  state.chip = state.data.chip || { multicore: false, cores: [], interactions: [] };
  renderOpList();
  const first = state.data.ops.find((o) => !o.is_init);
  if (first) selectOp(first.id);
}

function setMode(m) {
  stopPlay();
  state.mode = m;
  document.getElementById("mode-op").classList.toggle("active", m === "op");
  document.getElementById("mode-chip").classList.toggle("active", m === "chip");
  document.querySelector(".flow-main").hidden = m !== "op";
  document.getElementById("chip-main").hidden = m !== "chip";
  if (m === "chip") { buildChip(); setNoc(state.noc); }
  else { document.getElementById("step-info").textContent = ""; if (state.op) setStep(state.step); }
}

function renderOpList() {
  const host = document.getElementById("op-list");
  host.classList.remove("muted");
  host.innerHTML = "";
  state.data.ops.forEach((op) => {
    const row = document.createElement("div");
    row.className = "op-item" + (op.is_init ? " is-init" : "");
    if (state.op && op.id === state.op.id) row.classList.add("selected");
    const kind = op.kind ? ` · ${op.kind}` : "";
    const tag = op.is_init ? ' <span class="tag">init — no flow</span>'
              : (op.derived ? ' <span class="tag">derived</span>' : "");
    row.innerHTML = `<span class="op-name">${esc(op.name)}</span><span class="op-meta">${esc(kind)}</span>${tag}`;
    if (!op.is_init) row.addEventListener("click", () => selectOp(op.id));
    host.appendChild(row);
  });
}

function selectOp(id) {
  const op = state.data.ops.find((o) => o.id === id);
  if (!op || op.is_init) return;
  stopPlay();
  state.op = op;
  state.step = 0;
  renderOpList();
  renderOpTitle();
  buildDiagram();
  renderStepList();
  setStep(0);
}

function renderOpTitle() {
  const op = state.op;
  const bits = [op.name];
  if (op.kind) bits.push(op.kind);
  bits.push(`${op.tiles} tile${op.tiles === 1 ? "" : "s"}`);
  if (op.core_id) bits.push(op.core_id);
  document.getElementById("op-title").innerHTML =
    `<strong>${esc(op.name)}</strong> <span class="muted">${esc(bits.slice(1).join(" · "))}</span>` +
    (op.derived ? ` <span class="tag">derived flow</span>` : "");
}

// ---- diagram: stage columns + per-datum labeled tokens ----
// Each step "produces/moves" a datum (step.data). A datum that is read from L1
// before it is produced is shown sitting in Input L1, so you see data start in
// L1 and move through Src -> DEST -> ... as you step.
function datumOf(st) { return st.data || st.label; }

function computeDatums() {
  const steps = state.op.steps;
  const seen = new Map();           // name -> {name, origin, produced_at}
  const order = [];
  steps.forEach((st, i) => {
    const name = datumOf(st);
    if (!seen.has(name)) {
      const origin = st.reads[0] || st.writes[0] || state.op.stages[0];
      seen.set(name, { name, origin, produced_at: i });
      order.push(name);
    }
  });
  return order.map((n) => seen.get(n));
}

// Stage a datum occupies after step k: the writes (or reads) of the latest step
// <= k that produces it; before that, its origin (typically Input L1).
function datumStageAt(d, k) {
  let stage = d.origin;
  const steps = state.op.steps;
  for (let i = 0; i <= k && i < steps.length; i++) {
    if (datumOf(steps[i]) === d.name) {
      stage = steps[i].writes[0] || steps[i].reads[0] || stage;
    }
  }
  return stage;
}

function buildDiagram() {
  const host = document.getElementById("diagram");
  host.innerHTML = "";
  state.datums = [];
  const labels = state.data.stage_labels;
  if (!state.op.stages.length || !state.op.steps.length) {
    host.innerHTML = '<div class="muted" style="padding:20px">This op has no dataflow steps.</div>';
    return;
  }
  const cols = document.createElement("div");
  cols.className = "stage-cols";
  for (const s of state.op.stages) {
    const col = document.createElement("div");
    col.className = "stage-col";
    col.dataset.stage = s;
    col.innerHTML = `<div class="stage-head">${esc(labels[s] || s)}</div><div class="stage-body"></div>`;
    cols.appendChild(col);
  }
  host.appendChild(cols);

  const layer = document.createElement("div");
  layer.className = "token-layer";
  host.appendChild(layer);
  state.datums = computeDatums();
  state.datums.forEach((d, i) => {
    const el = document.createElement("div");
    el.className = "token datum";
    el.style.background = DATUM_COLORS[i % DATUM_COLORS.length];
    el.textContent = d.name;
    el.title = d.name;
    layer.appendChild(el);
    d.el = el;
  });
}

function setStep(k) {
  const steps = state.op.steps;
  if (!steps.length) {
    document.getElementById("step-caption").textContent = "No dataflow steps for this op.";
    document.getElementById("step-info").textContent = "";
    return;
  }
  state.step = Math.max(0, Math.min(k, steps.length - 1));
  const st = steps[state.step];
  const curDatum = datumOf(st);

  // highlight active stage columns (reads ∪ writes)
  const active = new Set([...st.reads, ...st.writes]);
  document.querySelectorAll("#diagram .stage-col").forEach((c) => {
    c.classList.toggle("active", active.has(c.dataset.stage));
    c.classList.toggle("reads", st.reads.includes(c.dataset.stage));
    c.classList.toggle("writes", st.writes.includes(c.dataset.stage));
  });

  positionDatums();
  // emphasize the datum moved this step; fade datums not yet produced
  state.datums.forEach((d) => {
    d.el.classList.toggle("active", d.name === curDatum);
    d.el.classList.toggle("dimmed", state.step < d.produced_at && d.name !== curDatum);
  });

  const cap = document.getElementById("step-caption");
  const flow = `${(st.reads.join("+") || "—")} → ${(st.writes.join("+") || "—")}`;
  cap.innerHTML = `<strong>${esc(st.label)}</strong>` +
    ` <span class="datum-chip">${esc(curDatum)}</span>` +
    ` <span class="flowarrow">${esc(flow)}</span>` +
    (st.expr ? `<div class="expr">DEST: <code>${esc(st.expr)}</code></div>` : "") +
    (st.note ? `<div class="note">${esc(st.note)}</div>` : "");

  document.querySelectorAll("#step-list li").forEach((li, i) =>
    li.classList.toggle("current", i === state.step));
  document.getElementById("step-info").textContent = `step ${state.step + 1} / ${steps.length}`;
}

// Place every datum token in the stage column it currently occupies, stacking
// datums that share a column.
function positionDatums() {
  const host = document.getElementById("diagram");
  const hb = host.getBoundingClientRect();
  const perCol = {};
  for (const d of state.datums) {
    const stage = datumStageAt(d, state.step);
    const col = host.querySelector(`.stage-col[data-stage="${cssEsc(stage)}"] .stage-body`);
    if (!col) continue;
    const cb = col.getBoundingClientRect();
    const idx = (perCol[stage] = (perCol[stage] || 0)) ;
    perCol[stage] += 1;
    const w = d.el.offsetWidth || 50;
    const cx = cb.left - hb.left + cb.width / 2;
    const top = cb.top - hb.top + 8 + idx * 26;
    d.el.style.transform = `translate(${cx - w / 2}px, ${top}px)`;
  }
}

function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s; }

function renderStepList() {
  const ol = document.getElementById("step-list");
  ol.classList.remove("muted");
  ol.innerHTML = "";
  state.op.steps.forEach((st, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="s-label">${esc(st.label)}</span>` +
      (st.expr ? `<span class="s-expr">${esc(st.expr)}</span>` : "");
    li.addEventListener("click", () => { stopPlay(); setStep(i); });
    ol.appendChild(li);
  });
}

// ---- chip (multi-core) view ----
function buildChip() {
  const grid = document.getElementById("chip-grid");
  grid.innerHTML = "";
  const cores = state.chip.cores;
  if (!cores.length) { grid.innerHTML = '<div class="muted" style="padding:20px">No cores.</div>'; return; }
  const cols = Math.max(1, ...cores.map((c) => (c.x | 0) + 1));
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(150px, 1fr))`;
  for (const c of cores) {
    const card = document.createElement("div");
    card.className = "core-card";
    card.dataset.core = c.id;
    card.style.gridColumn = String(Math.max(1, (c.x | 0) + 1));
    card.style.gridRow = String(Math.max(1, (c.y | 0) + 1));
    const ops = c.ops.map((o) =>
      `<li class="${o.is_init ? "is-init" : ""}">${esc(o.name)}</li>`).join("");
    card.innerHTML =
      `<div class="core-head">${esc(c.name)}</div>` +
      `<div class="core-pipe"><span>L1</span><span>Src</span><span>DEST</span><span>L1</span></div>` +
      `<ul class="core-ops">${ops || '<li class="muted">—</li>'}</ul>`;
    grid.appendChild(card);
  }
  // arrow overlay
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "chip-arrows");
  svg.innerHTML =
    '<defs><marker id="noc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" ' +
    'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8e24aa"/></marker></defs>' +
    '<path id="noc-path" fill="none" stroke="#8e24aa" stroke-width="2.5" marker-end="url(#noc-arrow)"/>';
  grid.appendChild(svg);
  renderNocList();
}

function renderNocList() {
  const ol = document.getElementById("noc-list");
  ol.classList.remove("muted");
  ol.innerHTML = "";
  if (!state.chip.interactions.length) {
    ol.innerHTML = '<li class="muted">No cross-core (NoC) interactions.</li>';
    return;
  }
  state.chip.interactions.forEach((it, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="s-label">${esc(it.from_core)} → ${esc(it.to_core)}</span>` +
      `<span class="s-expr">${esc(it.label)}</span>`;
    li.addEventListener("click", () => { stopPlay(); setNoc(i); });
    ol.appendChild(li);
  });
}

function setNoc(k) {
  const items = state.chip.interactions;
  const grid = document.getElementById("chip-grid");
  const cap = document.getElementById("chip-caption");
  const path = document.getElementById("noc-path");
  if (!items.length) {
    cap.textContent = "Cores run independently — no cross-core interactions in this diagram.";
    document.getElementById("step-info").textContent = "";
    grid.querySelectorAll(".core-card").forEach((c) => c.classList.remove("dim", "send", "recv"));
    if (path) path.removeAttribute("d");
    return;
  }
  state.noc = Math.max(0, Math.min(k, items.length - 1));
  const it = items[state.noc];
  grid.querySelectorAll(".core-card").forEach((c) => {
    const involved = c.dataset.core === it.from_core || c.dataset.core === it.to_core;
    c.classList.toggle("dim", !involved);
    c.classList.toggle("send", c.dataset.core === it.from_core);
    c.classList.toggle("recv", c.dataset.core === it.to_core);
  });
  drawNocArrow(it.from_core, it.to_core);
  cap.innerHTML = `<strong>NoC:</strong> ${esc(it.from_core)} → ${esc(it.to_core)} ` +
    `<span class="flowarrow">${esc(it.label)}</span>`;
  document.querySelectorAll("#noc-list li").forEach((li, i) => li.classList.toggle("current", i === state.noc));
  document.getElementById("step-info").textContent = `NoC ${state.noc + 1} / ${items.length}`;
}

// Look up a core card by id via iteration — core ids come from YAML and are
// unconstrained, so we must not interpolate them into a CSS selector.
function coreCard(id) {
  return [...document.getElementById("chip-grid").querySelectorAll(".core-card")]
    .find((c) => c.dataset.core === id) || null;
}

function drawNocArrow(fromId, toId) {
  const grid = document.getElementById("chip-grid");
  const path = document.getElementById("noc-path");
  const a = coreCard(fromId);
  const b = coreCard(toId);
  if (!a || !b || !path) return;
  const gb = grid.getBoundingClientRect();
  const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
  const ax = ra.left - gb.left + ra.width / 2, ay = ra.top - gb.top + ra.height / 2;
  const bx = rb.left - gb.left + rb.width / 2, by = rb.top - gb.top + rb.height / 2;
  const mx = (ax + bx) / 2, my = (ay + by) / 2 - 28;   // slight arc
  path.setAttribute("d", `M ${ax} ${ay} Q ${mx} ${my} ${bx} ${by}`);
}

// ---- controls (mode-aware) ----
function len() { return state.mode === "chip" ? state.chip.interactions.length : (state.op ? state.op.steps.length : 0); }
function cur() { return state.mode === "chip" ? state.noc : state.step; }
function go(i) { state.mode === "chip" ? setNoc(i) : setStep(i); }
function next() { const n = len(); if (n) go((cur() + 1) % n); }
function prev() { const n = len(); if (n) go((cur() - 1 + n) % n); }
function reset() { stopPlay(); if (len()) go(0); }
function togglePlay() { state.playing ? stopPlay() : startPlay(); }
function startPlay() {
  if (!len()) return;
  state.playing = true;
  document.getElementById("play").textContent = "❚❚ Pause";
  state.timer = setInterval(next, 1100);
}
function stopPlay() {
  state.playing = false;
  document.getElementById("play").textContent = "▶ Play";
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

document.getElementById("next").addEventListener("click", () => { stopPlay(); next(); });
document.getElementById("prev").addEventListener("click", () => { stopPlay(); prev(); });
document.getElementById("reset").addEventListener("click", reset);
document.getElementById("play").addEventListener("click", togglePlay);
document.getElementById("mode-op").addEventListener("click", () => setMode("op"));
document.getElementById("mode-chip").addEventListener("click", () => setMode("chip"));
window.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { stopPlay(); next(); }
  else if (e.key === "ArrowLeft") { stopPlay(); prev(); }
  else if (e.key === " ") { e.preventDefault(); togglePlay(); }
});
window.addEventListener("resize", () => {
  if (state.mode === "chip") { const it = state.chip.interactions[state.noc]; if (it) drawNocArrow(it.from_core, it.to_core); }
  else if (state.op && state.datums.length) positionDatums();
});

load();
