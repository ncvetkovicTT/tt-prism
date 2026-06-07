// tt-prism flow view — vanilla JS, no build step.
//
// Left: the sequence of ops (execute ops are selectable; init ops are greyed).
// Center: for the selected op, the Tensix storage stages (Input L1 / SrcA /
// SrcB / DEST / SFPU / Output L1) as columns, with tile tokens that hop between
// stages as you step through the op's dataflow. Right: the op's step list.
//
// Scheduling/derivation happens server-side (/api/flow). This file only paints.

const TILE_CAP = 6;                 // max tokens drawn (tiles beyond this are summarized)
const TILE_COLORS = ["#0f766e", "#b45309", "#b91c1c", "#4d7c0f", "#6d28d9", "#0369a1"];

const state = {
  data: null,        // /api/flow payload
  op: null,          // selected op object
  step: 0,
  playing: false,
  timer: null,
  tokens: [],        // [{el, tile}]
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

// ---- diagram: stage columns + tile tokens ----
function buildDiagram() {
  const host = document.getElementById("diagram");
  host.innerHTML = "";
  const labels = state.data.stage_labels;
  // stage columns (only the stages this op uses, in canonical order)
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

  // token layer
  const layer = document.createElement("div");
  layer.className = "token-layer";
  host.appendChild(layer);
  const n = Math.min(state.op.tiles, TILE_CAP);
  state.tokens = [];
  for (let i = 0; i < n; i++) {
    const el = document.createElement("div");
    el.className = "token";
    el.style.background = TILE_COLORS[i % TILE_COLORS.length];
    el.textContent = `T${i}`;
    layer.appendChild(el);
    state.tokens.push({ el, tile: i });
  }
  if (state.op.tiles > TILE_CAP) {
    const more = document.createElement("div");
    more.className = "token more";
    more.textContent = `+${state.op.tiles - TILE_CAP}`;
    layer.appendChild(more);
    state.tokens.push({ el: more, tile: -1 });
  }
}

// The stage a token sits in after step k: the step's primary write (data just
// landed there), else its read, else stay at the input.
function stageAtStep(k) {
  const steps = state.op.steps;
  if (k < 0 || !steps.length) return state.op.stages[0];
  const st = steps[k];
  return (st.writes[0] || st.reads[0] || state.op.stages[0]);
}

function setStep(k) {
  const steps = state.op.steps;
  if (!steps.length) return;
  state.step = Math.max(0, Math.min(k, steps.length - 1));
  const st = steps[state.step];

  // highlight active stage columns (reads ∪ writes)
  const active = new Set([...st.reads, ...st.writes]);
  document.querySelectorAll("#diagram .stage-col").forEach((c) => {
    c.classList.toggle("active", active.has(c.dataset.stage));
    c.classList.toggle("reads", st.reads.includes(c.dataset.stage));
    c.classList.toggle("writes", st.writes.includes(c.dataset.stage));
  });

  // move tokens into the current stage column
  const stage = stageAtStep(state.step);
  positionTokens(stage);

  // caption + step list + progress
  const cap = document.getElementById("step-caption");
  const flow = st.reads.length || st.writes.length
    ? `${(st.reads.join("+") || "—")} → ${(st.writes.join("+") || "—")}`
    : "";
  cap.innerHTML = `<strong>${esc(st.label)}</strong>` +
    (flow ? ` <span class="flowarrow">${esc(flow)}</span>` : "") +
    (st.expr ? `<div class="expr">DEST: <code>${esc(st.expr)}</code></div>` : "") +
    (st.note ? `<div class="note">${esc(st.note)}</div>` : "");

  document.querySelectorAll("#step-list li").forEach((li, i) =>
    li.classList.toggle("current", i === state.step));
  document.getElementById("step-info").textContent =
    `step ${state.step + 1} / ${steps.length}`;
}

function positionTokens(stage) {
  const host = document.getElementById("diagram");
  const col = host.querySelector(`.stage-col[data-stage="${stage}"] .stage-body`);
  if (!col) return;
  const hb = host.getBoundingClientRect();
  const cb = col.getBoundingClientRect();
  const cx = cb.left - hb.left + cb.width / 2;
  const top = cb.top - hb.top + 8;
  state.tokens.forEach((t, i) => {
    const w = t.el.offsetWidth || 46;
    t.el.style.transform = `translate(${cx - w / 2}px, ${top + i * 28}px)`;
  });
}

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
window.addEventListener("resize", () => { if (state.op) positionTokens(stageAtStep(state.step)); });

load();
