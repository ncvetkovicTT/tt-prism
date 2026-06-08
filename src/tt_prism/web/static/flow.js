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
  op: null,          // selected op object
  step: 0,
  playing: false,
  timer: null,
  datums: [],        // [{name, origin, produced_at, el}] — one labeled token per datum
};

async function load() {
  const r = await fetch("/api/flow");
  if (!r.ok) { document.getElementById("op-list").textContent = "Failed to load."; return; }
  state.data = await r.json();
  renderOpList();
  const first = state.data.ops.find((o) => !o.is_init);
  if (first) selectOp(first.id);
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

// ---- controls ----
function next() { if (state.op) setStep(state.step + 1 >= state.op.steps.length ? 0 : state.step + 1); }
function prev() { if (state.op) setStep(state.step - 1 < 0 ? state.op.steps.length - 1 : state.step - 1); }
function reset() { stopPlay(); if (state.op) setStep(0); }
function togglePlay() { state.playing ? stopPlay() : startPlay(); }
function startPlay() {
  if (!state.op) return;
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
window.addEventListener("resize", () => { if (state.op && state.datums.length) positionDatums(); });

load();
