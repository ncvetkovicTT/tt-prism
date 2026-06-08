// tt-prism flow walkthrough — vanilla JS, no build step.
//
// ONE unified timeline. The chip grid (1+ cores from the YAML) is always shown.
// Prev/Next/Play walk through ALL ops in YAML order:
//   - a COMPUTE op animates the Tensix engine view (faces moving L1→Src→DEST→L1);
//   - a DATA-MOVEMENT op freezes the engines and animates the chip grid (the
//     cores mcasting/gathering — possibly several transfers at once);
//   - an INIT op is a brief config marker.
// Within a compute op you step its dataflow; continuing advances to the next op.

const DATUM_COLORS = ["#0f766e", "#b45309", "#b91c1c", "#4d7c0f", "#6d28d9", "#0369a1", "#9d174d", "#115e59"];
// L1 stages are shared pools: a step reading L1 (an unpack picking one operand)
// must NOT consume the other residents. Register stages hold the exact data.
const POOL_STAGES = new Set(["l1_in", "l1_out"]);

const state = {
  data: null,        // /api/flow payload
  frames: [],        // global timeline: [{opIndex, kind:'compute'|'movement'|'init', sub}]
  i: 0,              // current global frame index
  op: null,          // the compute op currently built into the Tensix panel
  datums: [],        // tokens for state.op
  opFrames: [],      // per-step datum snapshots for state.op (0 = inputs in L1)
  playing: false,
  timer: null,
};

async function load() {
  const r = await fetch("/api/flow");
  if (!r.ok) { document.getElementById("op-list").textContent = "Failed to load."; return; }
  state.data = await r.json();
  buildChipGrid();
  buildFrames();
  renderOpList();
  setFrame(0);
}

// ---- global timeline ----
function buildFrames() {
  const frames = [];
  state.data.ops.forEach((op, opIndex) => {
    if (op.is_movement) {
      frames.push({ opIndex, kind: "movement", sub: 0 });
    } else if (op.is_init) {
      frames.push({ opIndex, kind: "init", sub: 0 });
    } else {
      // compute op: sub 0 = "inputs in L1", sub 1..N = flow steps
      const n = (op.steps || []).length;
      for (let sub = 0; sub <= n; sub++) frames.push({ opIndex, kind: "compute", sub });
    }
  });
  state.frames = frames.length ? frames : [{ opIndex: 0, kind: "init", sub: 0 }];
}

function firstFrameOfOp(opIndex) {
  const k = state.frames.findIndex((f) => f.opIndex === opIndex);
  return k < 0 ? 0 : k;
}

function renderOpList() {
  const host = document.getElementById("op-list");
  host.classList.remove("muted");
  host.innerHTML = "";
  state.data.ops.forEach((op, opIndex) => {
    const row = document.createElement("div");
    row.className = "op-item" + (op.is_init ? " is-init" : "") + (op.is_movement ? " is-move" : "");
    const kind = op.kind ? ` · ${op.kind}` : "";
    const tag = op.is_movement ? ' <span class="tag move">NoC</span>'
              : op.is_init ? ' <span class="tag">init</span>'
              : (op.derived ? ' <span class="tag">derived</span>' : "");
    row.dataset.opIndex = String(opIndex);
    row.innerHTML = `<span class="op-name">${esc(op.name)}</span><span class="op-meta">${esc(kind)}</span>${tag}`;
    row.addEventListener("click", () => { stopPlay(); setFrame(firstFrameOfOp(opIndex)); });
    host.appendChild(row);
  });
}

// ---- chip grid (always shown) ----
function buildChipGrid() {
  const grid = document.getElementById("chip-grid");
  grid.innerHTML = "";
  const cores = state.data.cores || [];
  const cols = Math.max(1, ...cores.map((c) => (c.x | 0) + 1));
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(120px, 1fr))`;
  for (const c of cores) {
    const card = document.createElement("div");
    card.className = "core-card";
    card.dataset.core = c.id;
    card.style.gridColumn = String(Math.max(1, (c.x | 0) + 1));
    card.style.gridRow = String(Math.max(1, (c.y | 0) + 1));
    card.innerHTML =
      `<div class="core-head">${esc(c.name)}</div>` +
      `<div class="core-pipe"><span>L1</span><span>Src</span><span>DEST</span><span>L1</span></div>`;
    grid.appendChild(card);
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "chip-arrows");
  svg.innerHTML =
    '<defs><marker id="noc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" ' +
    'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8e24aa"/></marker></defs>' +
    '<g id="noc-arrows"></g>';
  grid.appendChild(svg);
}

function coreCard(id) {
  return [...document.getElementById("chip-grid").querySelectorAll(".core-card")]
    .find((c) => c.dataset.core === id) || null;   // core ids are unconstrained → no selector interpolation
}

function clearGrid() {
  document.querySelectorAll("#chip-grid .core-card").forEach((c) =>
    c.classList.remove("send", "recv", "computing", "dim"));
  document.getElementById("noc-arrows").innerHTML = "";
}

function highlightComputing(op) {
  const cores = state.data.cores || [];
  const on = (op.on_cores && op.on_cores.length) ? new Set(op.on_cores) : new Set(cores.map((c) => c.id));
  document.querySelectorAll("#chip-grid .core-card").forEach((c) => {
    c.classList.toggle("computing", on.has(c.dataset.core));
    c.classList.toggle("dim", !on.has(c.dataset.core) && cores.length > 1);
  });
}

function drawTransfers(op) {
  const grid = document.getElementById("chip-grid");
  const g = document.getElementById("noc-arrows");
  g.innerHTML = "";
  const gb = grid.getBoundingClientRect();
  const senders = new Set(), recvers = new Set();
  for (const t of op.transfers) {
    const a = coreCard(t.from), b = coreCard(t.to);
    if (!a || !b) continue;
    senders.add(t.from); recvers.add(t.to);
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const ax = ra.left - gb.left + ra.width / 2, ay = ra.top - gb.top + ra.height / 2;
    const bx = rb.left - gb.left + rb.width / 2, by = rb.top - gb.top + rb.height / 2;
    const mx = (ax + bx) / 2, my = (ay + by) / 2 - 26;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", `M ${ax} ${ay} Q ${mx} ${my} ${bx} ${by}`);
    p.setAttribute("class", "noc-path");
    g.appendChild(p);
  }
  document.querySelectorAll("#chip-grid .core-card").forEach((c) => {
    const s = senders.has(c.dataset.core), r = recvers.has(c.dataset.core);
    c.classList.toggle("send", s);
    c.classList.toggle("recv", r && !s);
    c.classList.toggle("dim", !s && !r && (state.data.cores || []).length > 1);
  });
}

// ---- Tensix engine panel (per compute op) ----
function datumOf(st) { return st.data || st.label; }

function computeDatums(op) {
  const seen = new Map(); const order = [];
  (op.steps || []).forEach((st, i) => {
    const name = datumOf(st);
    if (!seen.has(name)) {
      seen.set(name, { name, origin: st.reads[0] || st.writes[0] || op.stages[0], produced_at: i + 1 });
      order.push(name);
    }
  });
  return order.map((n) => seen.get(n));
}

function computeOpFrames(op) {
  const frames = [];
  const snap = new Map();
  for (const d of state.datums) snap.set(d.name, { stage: d.origin, consumed: false });
  frames.push(cloneSnap(snap));                       // sub 0: inputs in L1
  (op.steps || []).forEach((st) => {
    const produced = datumOf(st);
    const writeStage = st.writes[0] || st.reads[0];
    for (const [name, s] of snap) {
      if (name === produced) continue;
      if (!s.consumed && !POOL_STAGES.has(s.stage) && st.reads.includes(s.stage)) s.consumed = true;
    }
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

function buildOpDiagram(op) {
  state.op = op;
  const host = document.getElementById("diagram");
  host.innerHTML = "";
  host.classList.remove("idle");
  state.datums = [];
  if (!op.stages.length || !(op.steps || []).length) {
    host.innerHTML = '<div class="muted" style="padding:20px">No engine dataflow for this op.</div>';
    state.opFrames = [];
    return;
  }
  const cols = document.createElement("div");
  cols.className = "stage-cols";
  for (const s of op.stages) {
    const col = document.createElement("div");
    col.className = "stage-col";
    col.dataset.stage = s;
    col.innerHTML = `<div class="stage-head">${esc(state.data.stage_labels[s] || s)}</div><div class="stage-body"></div>`;
    cols.appendChild(col);
  }
  host.appendChild(cols);
  const layer = document.createElement("div");
  layer.className = "token-layer";
  host.appendChild(layer);
  state.datums = computeDatums(op);
  state.datums.forEach((d, i) => {
    const el = document.createElement("div");
    el.className = "token datum";
    el.style.background = DATUM_COLORS[i % DATUM_COLORS.length];
    el.textContent = d.name; el.title = d.name;
    layer.appendChild(el); d.el = el;
  });
  state.opFrames = computeOpFrames(op);
}

function showTensixIdle(msg) {
  const host = document.getElementById("diagram");
  host.classList.add("idle");
  host.innerHTML = `<div class="muted" style="padding:20px">${esc(msg)}</div>`;
  state.op = null; state.datums = []; state.opFrames = [];
}

function renderTensixStep(op, sub) {
  const st = sub === 0 ? null : (op.steps || [])[sub - 1];
  const reads = st ? st.reads : [], writes = st ? st.writes : [];
  const active = new Set([...reads, ...writes]);
  document.querySelectorAll("#diagram .stage-col").forEach((c) => {
    c.classList.toggle("active", active.has(c.dataset.stage));
    c.classList.toggle("reads", reads.includes(c.dataset.stage));
    c.classList.toggle("writes", writes.includes(c.dataset.stage));
  });
  positionDatums(sub);
  const curDatum = st ? datumOf(st) : null;
  const frame = state.opFrames[sub] || new Map();
  state.datums.forEach((d) => {
    const fs = frame.get(d.name) || { stage: d.origin, consumed: false };
    const resident = POOL_STAGES.has(fs.stage) && !fs.consumed;
    const notYet = sub < d.produced_at && !resident;
    d.el.classList.toggle("active", d.name === curDatum && !fs.consumed);
    d.el.classList.toggle("consumed", !!fs.consumed);
    d.el.classList.toggle("dimmed", (notYet || fs.consumed) && d.name !== curDatum);
  });
  return st;
}

function positionDatums(sub) {
  const host = document.getElementById("diagram");
  const hb = host.getBoundingClientRect();
  const frame = state.opFrames[sub] || new Map();
  const perCol = {};
  for (const d of state.datums) {
    const fs = frame.get(d.name) || { stage: d.origin };
    const col = host.querySelector(`.stage-col[data-stage="${cssEsc(fs.stage)}"] .stage-body`);
    if (!col) continue;
    const cb = col.getBoundingClientRect();
    const idx = (perCol[fs.stage] = (perCol[fs.stage] || 0)); perCol[fs.stage] += 1;
    const w = d.el.offsetWidth || 50;
    d.el.style.transform = `translate(${cb.left - hb.left + cb.width / 2 - w / 2}px, ${cb.top - hb.top + 8 + idx * 26}px)`;
  }
}

function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s; }

// ---- the unified renderer ----
function setFrame(i) {
  if (!state.frames.length) return;
  state.i = Math.max(0, Math.min(i, state.frames.length - 1));
  const f = state.frames[state.i];
  const op = state.data.ops[f.opIndex];
  const cap = document.getElementById("step-caption");
  const title = document.getElementById("op-title");

  // op list highlight
  document.querySelectorAll("#op-list .op-item").forEach((r) =>
    r.classList.toggle("selected", Number(r.dataset.opIndex) === f.opIndex));

  if (f.kind === "movement") {
    clearGrid();
    drawTransfers(op);
    if (state.op) document.getElementById("diagram").classList.add("idle");
    title.innerHTML = `<strong>${esc(op.name)}</strong> <span class="tag move">NoC data movement</span>`;
    const legs = op.transfers.map((t) => `${esc(t.from)}→${esc(t.to)}`).join(", ");
    cap.innerHTML = `<strong>${esc(op.name)}</strong>` +
      ` <span class="flowarrow">${op.transfers.length} transfer(s): ${legs}</span>` +
      `<div class="note">engines idle — data moves over the NoC</div>`;
    stepInfo();
    return;
  }

  if (f.kind === "init") {
    clearGrid();
    if (op.core_id) { const c = coreCard(op.core_id); if (c) c.classList.add("computing"); }
    showTensixIdle("config / init — no dataflow");
    title.innerHTML = `<strong>${esc(op.name)}</strong> <span class="tag">init</span>`;
    cap.innerHTML = `<strong>${esc(op.name)}</strong> <span class="flowarrow">configuration, no data movement</span>`;
    stepInfo();
    return;
  }

  // compute op
  if (!state.op || state.op.id !== op.id) buildOpDiagram(op);
  clearGrid();
  highlightComputing(op);
  const bits = [];
  if (op.kind) bits.push(op.kind);
  bits.push(`${op.tiles} tile${op.tiles === 1 ? "" : "s"}`);
  if (op.on_cores && op.on_cores.length) bits.push(`on ${op.on_cores.length} core(s)`);
  title.innerHTML = `<strong>${esc(op.name)}</strong> <span class="muted">${esc(bits.join(" · "))}</span>` +
    (op.derived ? ` <span class="tag">derived flow</span>` : "");
  const st = renderTensixStep(op, f.sub);
  if (f.sub === 0) {
    cap.innerHTML = `<strong>Inputs in L1</strong> <span class="flowarrow">operands resident in Input L1 — nothing moved yet</span>`;
  } else if (st) {
    const flow = `${(st.reads.join("+") || "—")} → ${(st.writes.join("+") || "—")}`;
    cap.innerHTML = `<strong>${esc(st.label)}</strong>` +
      ` <span class="datum-chip">${esc(datumOf(st))}</span> <span class="flowarrow">${esc(flow)}</span>` +
      (st.expr ? `<div class="expr">DEST: <code>${esc(st.expr)}</code></div>` : "") +
      (st.note ? `<div class="note">${esc(st.note)}</div>` : "");
  } else {
    cap.innerHTML = `<strong>${esc(op.name)}</strong>`;
  }
  stepInfo();
}

function stepInfo() {
  const f = state.frames[state.i];
  const op = state.data.ops[f.opIndex];
  let detail = op.name;
  if (f.kind === "compute") detail += f.sub === 0 ? " — inputs in L1" : ` — step ${f.sub}/${(op.steps || []).length}`;
  document.getElementById("step-info").textContent =
    `${state.i + 1} / ${state.frames.length} · ${detail}`;
}

// ---- controls ----
function next() { setFrame((state.i + 1) % state.frames.length); }
function prev() { setFrame((state.i - 1 + state.frames.length) % state.frames.length); }
function reset() { stopPlay(); setFrame(0); }
function togglePlay() { state.playing ? stopPlay() : startPlay(); }
function startPlay() {
  if (!state.frames.length) return;
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
window.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { stopPlay(); next(); }
  else if (e.key === "ArrowLeft") { stopPlay(); prev(); }
  else if (e.key === " ") { e.preventDefault(); togglePlay(); }
});
window.addEventListener("resize", () => {
  const f = state.frames[state.i];
  if (!f) return;
  const op = state.data.ops[f.opIndex];
  if (f.kind === "movement") drawTransfers(op);
  else if (f.kind === "compute" && state.op) positionDatums(f.sub);
});

load();
