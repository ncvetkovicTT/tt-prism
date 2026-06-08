// tt-prism flow view — vanilla JS, no build step.
//
// Left: the sequence of ops (execute ops are selectable; init ops are greyed).
// Center: for the selected op, the Tensix storage stages (Input L1 / SrcA /
// SrcB / DEST / SFPU / Output L1) as columns, with tile tokens that hop between
// stages as you step through the op's dataflow. Right: the op's step list.
//
// Scheduling/derivation happens server-side (/api/flow). This file only paints.

const DATUM_COLORS = ["#0f766e", "#b45309", "#b91c1c", "#4d7c0f", "#6d28d9", "#0369a1", "#9d174d", "#115e59"];

// L1 stages are shared source/sink pools: many operands live there at once, so
// a step reading L1 (an unpack picking one operand) must NOT consume the other
// residents. Only the register stages (SrcA/SrcB/DEST/SFPU) hold the exact data
// being computed on, so reading them consumes the occupant.
const POOL_STAGES = new Set(["l1_in", "l1_out"]);

const state = {
  data: null,        // /api/flow payload
  op: null,          // selected op object (single-op mode)
  step: 0,           // 0 = synthetic "Inputs in L1" frame; 1..N = real steps
  mode: "op",        // "op" | "chip"
  chip: null,        // chip payload {multicore, cores[], interactions[]}
  noc: 0,            // current interaction index (chip mode)
  playing: false,
  timer: null,
  datums: [],        // [{name, origin, produced_at, el}] — one labeled token per datum
  frames: [],        // frames[k] = Map(name -> {stage, consumed}); k in 0..N (see computeFrames)
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
// The view has N+1 frames: frame 0 is a synthetic "Inputs in L1" state (every
// input datum resident in Input L1, nothing moved yet) and frames 1..N each
// correspond to real flow step 1..N. state.step indexes the frame directly.
//
// Each step "produces/moves" a datum (step.data). A datum advances to a step's
// `writes` stage when that step produces it; an operand sitting in a stage that
// a later step *reads* (without re-writing that same datum) is "consumed" — it
// leaves Src and folds into the result, instead of lingering forever.
function datumOf(st) { return st.data || st.label; }

function computeDatums() {
  const steps = state.op.steps;
  const seen = new Map();           // name -> {name, origin, produced_at}
  const order = [];
  steps.forEach((st, i) => {
    const name = datumOf(st);
    if (!seen.has(name)) {
      const origin = st.reads[0] || st.writes[0] || state.op.stages[0];
      // produced_at is a 1-based step index (frame 0 is the synthetic L1 state).
      seen.set(name, { name, origin, produced_at: i + 1 });
      order.push(name);
    }
  });
  return order.map((n) => seen.get(n));
}

// Simulate the flow once to produce, for every frame k in 0..N, a snapshot of
// where each datum sits and whether it has been consumed.
//   frame 0: each datum at its origin, not consumed (the "all in L1" state).
//   frame k (k>=1): apply real step k = steps[k-1]:
//     - the datum it produces (data) moves to the step's write stage and is
//       (re)marked live;
//     - any *other* datum currently occupying one of the step's `reads` stages
//       is marked consumed (it has been read into the result and leaves Src).
function computeFrames() {
  const steps = state.op.steps;
  const frames = [];
  const snap = new Map();
  for (const d of state.datums) snap.set(d.name, { stage: d.origin, consumed: false });
  frames.push(cloneSnap(snap));
  steps.forEach((st) => {
    const produced = datumOf(st);
    const writeStage = st.writes[0] || st.reads[0];
    // Consume operands this step reads out of a register stage (not an L1 pool)
    // and does not itself re-produce.
    for (const [name, s] of snap) {
      if (name === produced) continue;
      if (!s.consumed && !POOL_STAGES.has(s.stage) && st.reads.includes(s.stage)) {
        s.consumed = true;
      }
    }
    // The produced datum (re)appears live at its write stage.
    const ps = snap.get(produced);
    if (ps) { if (writeStage) ps.stage = writeStage; ps.consumed = false; }
    frames.push(cloneSnap(snap));
  });
  return frames;
}

function cloneSnap(snap) {
  const m = new Map();
  for (const [k, v] of snap) m.set(k, { stage: v.stage, consumed: v.consumed });
  return m;
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
  state.frames = computeFrames();
}

function setStep(k) {
  const steps = state.op.steps;
  if (!steps.length) {
    document.getElementById("step-caption").textContent = "No dataflow steps for this op.";
    document.getElementById("step-info").textContent = "";
    return;
  }
  // Frame index: 0 = "Inputs in L1", 1..N = real steps (steps[frame-1]).
  state.step = Math.max(0, Math.min(k, steps.length));
  const isL1 = state.step === 0;
  const st = isL1 ? null : steps[state.step - 1];
  const curDatum = st ? datumOf(st) : null;

  // highlight active stage columns (reads ∪ writes); none on the L1 frame.
  const reads = st ? st.reads : [];
  const writes = st ? st.writes : [];
  const active = new Set([...reads, ...writes]);
  document.querySelectorAll("#diagram .stage-col").forEach((c) => {
    c.classList.toggle("active", active.has(c.dataset.stage));
    c.classList.toggle("reads", reads.includes(c.dataset.stage));
    c.classList.toggle("writes", writes.includes(c.dataset.stage));
  });

  positionDatums();
  // emphasize the datum moved this step; fade consumed datums and result datums
  // not yet produced. Input operands resident in an L1 pool are "present from
  // the start", so they show clearly (not dimmed) even before their unpack step.
  const frame = state.frames[state.step] || new Map();
  state.datums.forEach((d) => {
    const fs = frame.get(d.name) || { stage: d.origin, consumed: false };
    const resident = POOL_STAGES.has(fs.stage) && !fs.consumed;
    const notYet = state.step < d.produced_at && !resident;
    d.el.classList.toggle("active", d.name === curDatum && !fs.consumed);
    d.el.classList.toggle("consumed", !!fs.consumed);
    d.el.classList.toggle("dimmed", (notYet || fs.consumed) && d.name !== curDatum);
  });

  const cap = document.getElementById("step-caption");
  if (isL1) {
    cap.innerHTML = `<strong>Inputs in L1</strong>` +
      ` <span class="flowarrow">all input operands resident in Input L1 — nothing moved yet</span>`;
  } else {
    const flow = `${(reads.join("+") || "—")} → ${(writes.join("+") || "—")}`;
    cap.innerHTML = `<strong>${esc(st.label)}</strong>` +
      ` <span class="datum-chip">${esc(curDatum)}</span>` +
      ` <span class="flowarrow">${esc(flow)}</span>` +
      (st.expr ? `<div class="expr">DEST: <code>${esc(st.expr)}</code></div>` : "") +
      (st.note ? `<div class="note">${esc(st.note)}</div>` : "");
  }

  // Step list is 1-based against real steps; highlight none on the L1 frame.
  document.querySelectorAll("#step-list li").forEach((li, i) =>
    li.classList.toggle("current", i === state.step - 1));
  document.getElementById("step-info").textContent = isL1
    ? `step 0 / ${steps.length} — inputs in L1`
    : `step ${state.step} / ${steps.length}`;
}

// Place every datum token in the stage column it currently occupies, stacking
// datums that share a column.
function positionDatums() {
  const host = document.getElementById("diagram");
  const hb = host.getBoundingClientRect();
  const frame = state.frames[state.step] || new Map();
  const perCol = {};
  for (const d of state.datums) {
    const fs = frame.get(d.name) || { stage: d.origin };
    const stage = fs.stage;
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
    '<path id="noc-path" fill="none" stroke="#8e24aa" stroke-width="2.5" marker-end="url(#noc-arrow)"/>' +
    '<rect id="noc-label-bg" class="noc-label-bg" rx="4" hidden/>' +
    '<text id="noc-label" class="noc-label" text-anchor="middle"></text>';
  grid.appendChild(svg);
  renderNocList();
}

// Phase grouping so the NoC list reads as "what happens when".
function nocPhase(it) {
  const l = (it.label || "").toLowerCase();
  if (l.includes("bcast") || l.includes("broadcast")) return "① broadcast";
  if (l.includes("sdpa")) return "② SDPA reduce";
  if (l.includes("all-reduce") || l.includes("allreduce")) return "③ all-reduce";
  if (l.includes("reduce-to-one") || l.includes("reduce to one")) return "④ reduce-to-one";
  return "NoC";
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
    li.innerHTML =
      `<span class="s-label">${i + 1}. ${esc(it.from_core)} → ${esc(it.to_core)} ` +
      `<span class="noc-phase">${esc(nocPhase(it))}</span></span>` +
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
    const lbl = document.getElementById("noc-label"), lblBg = document.getElementById("noc-label-bg");
    if (lbl) lbl.textContent = ""; if (lblBg) lblBg.setAttribute("hidden", "");
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
  drawNocArrow(it);
  cap.innerHTML =
    `<strong>Step ${state.noc + 1}/${items.length} · ${esc(nocPhase(it))}</strong>` +
    `<div>${esc(it.from_core)} <b>sends</b> → ${esc(it.to_core)} <b>receives</b>` +
    `<span class="datum-chip">${esc(it.label)}</span></div>` +
    `<div class="note">producer op <code>${esc(it.from_op)}</code> → consumer op <code>${esc(it.to_op)}</code></div>`;
  document.querySelectorAll("#noc-list li").forEach((li, i) => li.classList.toggle("current", i === state.noc));
  document.getElementById("step-info").textContent = `NoC ${state.noc + 1} / ${items.length}`;
}

// Look up a core card by id via iteration — core ids come from YAML and are
// unconstrained, so we must not interpolate them into a CSS selector.
function coreCard(id) {
  return [...document.getElementById("chip-grid").querySelectorAll(".core-card")]
    .find((c) => c.dataset.core === id) || null;
}

function drawNocArrow(it) {
  const grid = document.getElementById("chip-grid");
  const path = document.getElementById("noc-path");
  const lbl = document.getElementById("noc-label");
  const lblBg = document.getElementById("noc-label-bg");
  const a = coreCard(it.from_core);
  const b = coreCard(it.to_core);
  if (!a || !b || !path) return;
  const gb = grid.getBoundingClientRect();
  const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
  const ax = ra.left - gb.left + ra.width / 2, ay = ra.top - gb.top + ra.height / 2;
  const bx = rb.left - gb.left + rb.width / 2, by = rb.top - gb.top + rb.height / 2;
  const mx = (ax + bx) / 2, my = (ay + by) / 2 - 28;   // slight arc
  path.setAttribute("d", `M ${ax} ${ay} Q ${mx} ${my} ${bx} ${by}`);
  // payload label centered on the arc apex (the Q control point is the apex)
  if (lbl) {
    const lx = (ax + 2 * mx + bx) / 4, ly = (ay + 2 * my + by) / 4;  // bezier midpoint
    lbl.textContent = it.label || "";
    lbl.setAttribute("x", lx); lbl.setAttribute("y", ly - 4);
    if (lblBg) {
      const bb = lbl.getBBox();
      lblBg.setAttribute("x", bb.x - 5); lblBg.setAttribute("y", bb.y - 2);
      lblBg.setAttribute("width", bb.width + 10); lblBg.setAttribute("height", bb.height + 4);
      lblBg.removeAttribute("hidden");
    }
  }
}

// ---- controls (mode-aware) ----
// Single-op mode has steps.length + 1 frames (frame 0 = "Inputs in L1").
function len() { return state.mode === "chip" ? state.chip.interactions.length : (state.op ? state.op.steps.length + 1 : 0); }
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
  if (state.mode === "chip") { const it = state.chip.interactions[state.noc]; if (it) drawNocArrow(it); }
  else if (state.op && state.datums.length) positionDatums();
});

load();
