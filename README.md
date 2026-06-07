# tt-prism

Pipeline swim-lane diagrams for TRISC/Tensix performance modeling. Replaces the
manual Excalidraw workflow the LLK team uses today, and provides the
visualization layer for the perf simulator + RTL-wave post-processor described
in the [perf optimization tooling proposal](https://tenstorrent.atlassian.net/wiki/spaces/LLK/pages/1116438541).

## What it does

- Lets you **author** a pipeline diagram (TRISC0/1/2 lanes + UNPACK/FPU/SFPU/PACK
  resources) as a hand-editable YAML file — either as high-level **ops** (one
  Compute API call each) whose timing the scheduler derives, or as hand-placed
  **work items** with explicit start clocks.
- **Schedules** op-authored diagrams against Tensix constraints (intra-op
  unpack→math→pack handshake, singleton engines, virtual DEST double-buffering,
  cross-op data dependencies through L1) so the picture is always physically
  legal — move or resize one block and everything downstream reflows.
- **Renders** the diagram to SVG or PDF from the CLI, or to an interactive
  editor in a local web app.
- Exposes the diagram as **JSON over HTTP** so other tools (e.g. the perf
  simulator's pipeline assembler) can produce or consume the same format.

Everything operates on one data model — see [`models.py`](src/tt_prism/models.py).
There's no separate "presentation" type: the YAML file is the source of truth,
and the CLI, web editor, and API all read/write the same shape.

## Install

Requires Python 3.10+.

```bash
git clone <repo-url> tt-prism
cd tt-prism
python -m venv .venv && source .venv/bin/activate   # recommended
pip install -e .                                    # installs all runtime deps
```

This installs everything needed to author, schedule, serve, and render to SVG
**and PDF**. PDF rendering additionally needs the native **Cairo** library on the
system (the `cairosvg` Python package is installed for you, but it loads
`libcairo2` at render time):

```bash
# Debian/Ubuntu, if PDF export complains about the Cairo library:
sudo apt-get install libcairo2
```

For development (tests + linter): `pip install -e '.[dev]'`.

After install, the `tt-prism` console command is available. If it isn't on your
PATH, run `python -m tt_prism.cli ...` instead.

## Usage

There are two ways to author a diagram, and both render/edit through the same
tools:

- **Op-authored** (recommended) — describe *ops* (one Compute API call each) and
  their dependencies; the scheduler **derives** every start clock so the picture
  is always a legal Tensix schedule. See
  [Op-authored diagrams](#op-authored-diagrams-constraint-scheduler). Examples:
  [`examples/ops_dest_banks.yaml`](examples/ops_dest_banks.yaml),
  [`examples/ops_add_then_mul.yaml`](examples/ops_add_then_mul.yaml).
- **Flat** (legacy) — hand-place `work_items` with explicit `start_clock`s.
  Example: [`examples/sub_exp_reduce.yaml`](examples/sub_exp_reduce.yaml).

### CLI

```bash
# 1. (optional) scaffold a blank flat diagram to start from
tt-prism new my_pipeline.yaml

# 2. validate — checks references, and for op-diagrams that they SCHEDULE
tt-prism validate examples/ops_dest_banks.yaml

# 3. schedule — solve an op-diagram and print the resolved timeline (start/dur/end)
tt-prism schedule examples/ops_dest_banks.yaml

# 4. render — to SVG (default) or PDF; op-diagrams are scheduled first
tt-prism render examples/ops_dest_banks.yaml -o out.svg
tt-prism render examples/ops_dest_banks.yaml -o out.pdf      # PDF also needs libcairo2

# edit loop: tweak a duration / dest_bank in the YAML, re-run schedule/render to
# see everything downstream reflow.
```

CLI reference:

| Command    | Purpose                                                                 | Key options                                                   |
|------------|-------------------------------------------------------------------------|---------------------------------------------------------------|
| `new`      | Scaffold a starter YAML                                                  | `--force/-f` overwrite existing                               |
| `validate` | Load, validate references; for op-diagrams, confirm they schedule        | —                                                             |
| `schedule` | Solve an op-authored diagram and print the resolved timeline             | —                                                             |
| `render`   | Render to SVG (default) or PDF (if `-o` ends in `.pdf`)                  | `-o/--output`, `--px-per-clock`, `--lane-height`              |
| `serve`    | Launch the local web editor on a YAML file                               | `--host` (`127.0.0.1`), `--port` (`8765`), `--open/--no-open` |

### Web editor (GUI)

```bash
tt-prism serve examples/ops_dest_banks.yaml      # browser opens at http://127.0.0.1:8765
```

The YAML file on disk is the source of truth; the editor reads and writes it.

- **Op-authored diagrams** open in a **scheduled (read-only-position) view**: bars
  are placed by the solver, not by hand. Select a block and edit its **Duration**
  or **DEST bank** in the inspector, then **Apply** — the whole schedule re-solves
  and reflows live. **Save** writes the edited ops back to the YAML.
- **Flat diagrams** open in the full editor: drag to move/restage items, edit in
  the inspector, link dependencies, batch-edit, copy/paste. See
  [Editor](#editor) for the mouse/keyboard reference.

If `tt-prism` isn't on your `PATH`, use `python -m tt_prism.cli serve …`. Stop the
server with `Ctrl-C`.

### Running tests

```bash
pytest                              # if the package is installed (pip install -e .)
PYTHONPATH=src python -m pytest     # run from source without installing
```

Coverage: model validation + YAML round-trip, the SVG renderer, and the scheduler
— intra-op precedence, resource/lane serialization, cross-op `l1_data`
dependencies, and virtual DEST-bank double-buffering
(`tests/test_schedule.py`, `test_data_deps.py`, `test_resource_deps.py`,
`test_dest_banks.py`).

## Editor

The editor is a single page hand-written in vanilla JS + SVG — no build step.
Open it via `tt-prism serve <diagram.yaml>`; the file on disk is the source of
truth, and saves write back to it.

### Mouse / drag

- **Click** a work item to select; **shift-click** to add/remove from selection
- **Drag** an item along its lane to change its `start_clock` (snaps to `grid_clocks`)
- **Drag vertically** to move an item between lanes
- **Click the background** to clear selection
- Hover an item: its dependency arrows thicken (still fully visible by default)
- **Link mode** (toolbar checkbox): click source then target to add a dependency

### Toolbar

- `+ Item / + Lane / + Resource` — append a new element
- `Copy / Paste / Duplicate` — clipboard operations on the selection
- `Grid` and `px/clk` — change snap unit and zoom live
- `Filter by tag` — type a tag, press Enter to select all items with that tag
- `Batch edit…` — apply a shift/resource/tag change to every selected item
- `Save / Reload / Export SVG` — save back to YAML, reload from disk, or download an SVG snapshot

### Keyboard

| Shortcut          | Action                                          |
|-------------------|-------------------------------------------------|
| `Ctrl/Cmd-S`      | Save to disk                                    |
| `Ctrl/Cmd-C`      | Copy selection to in-app clipboard               |
| `Ctrl/Cmd-V`      | Paste (offset by one grid step from the original) |
| `Ctrl/Cmd-D`      | Duplicate selection in place (copy + paste)      |
| `Delete` / `Backspace` | Delete selection + any deps referencing it  |

Paste re-keys IDs (`foo` → `foo_1`, etc.) and preserves intra-selection
dependencies between the new items.

## YAML schema

A diagram is a single YAML document with five top-level lists:

```yaml
title: "sub+exp / reduce pipeline (fragment)"
clock_ghz: 1.0          # optional — enables ns labels in the ruler
grid_clocks: 32         # snap unit and major-tick interval

lanes:
  - { id: trisc0, name: "TRISC 0", order: 0 }
  - { id: trisc1, name: "TRISC 1", order: 1 }
  - { id: trisc2, name: "TRISC 2", order: 2 }

resources:
  - { id: fpu,    name: "FPU",    color: "#a5d6a7" }
  - { id: sfpu,   name: "SFPU",   color: "#90caf9" }
  - { id: unpack, name: "UNPACK", color: "#ef9a9a" }
  - { id: pack,   name: "PACK",   color: "#ffcc80" }

work_items:
  - id: unp_qk_0
    lane_id: trisc0          # required, must be a defined lane
    resource_id: unpack      # optional, colors the bar
    label: "sub_bcast_cols QK[0,0:7] D0"
    start_clock: 79
    duration_clocks: 257
    tags: [unpack]           # free-form tags, used for batch edit / filter

dependencies:
  - { from: unp_qk_0, to: fpu_sub_0, kind: fifo }
  - { from: fpu_sub_0, to: sfpu_exp_0, kind: dep }
  - { from: pack_0a, to: pack_0b, kind: flow, label: "acc" }
```

### Concepts

| Concept      | Purpose                                                          |
|--------------|------------------------------------------------------------------|
| **Lane**     | A row in the diagram. Represents an execution context (TRISC0/1/2). Scales to any count via `order`. |
| **Resource** | A type of work (UNPACK/FPU/SFPU/PACK/THCON, or whatever you define). Provides color + semantic tag. Orthogonal to lane — one lane can host work items of any resource. |
| **WorkItem** | A bar with `start_clock`, `duration_clocks`, on one lane, optionally of one resource. The `tags` list enables tag-based filtering and batch edits. |
| **Dependency** | An arrow from one item to another. `kind ∈ {dep, fifo, flow}` currently controls *color only* (`dep`=grey, `fifo`=blue, `flow`=green dashed). |

### Cross-reference validation

On load, the YAML is validated for:
- Unique work-item IDs
- Every `lane_id` and `resource_id` resolves
- Every `from` / `to` in a dependency resolves

Invalid files fail with a clear error from `tt-prism validate` and a 400 from
the editor's PUT endpoint.

## Op-authored diagrams (constraint scheduler)

Authoring absolute `start_clock`s by hand lets you draw physically impossible
pictures (unpack after math; resize one bar without shifting its dependents). A
diagram can instead be authored as **ops**, in which case start clocks are
*derived* by a scheduler so the picture is always a legal Tensix schedule.

An **Op** is one Compute API call (`mm_init`, `reduce_tile`,
`pack_untilize_dest`, …) — the smallest unit tt-prism models. Each op lists
**blocks** in pipeline order; a block holds one of the four resources
(UNPACK/FPU/SFPU/PACK) for a user-supplied duration, dispatched by one TRISC
lane. THCON / sync time is folded into whichever block wraps it.

```yaml
ops:
  - id: add
    name: "add_tiles"          # the Compute API call
    kind: eltwise
    tiles: 8                   # metadata for now
    blocks:                    # pipeline order => unpack -> fpu -> pack
      - { id: add_unp, lane_id: trisc0, resource_id: unpack, duration_clocks: 104 }
      - { id: add_fpu, lane_id: trisc1, resource_id: fpu,    duration_clocks: 32 }
      - { id: add_pak, lane_id: trisc2, resource_id: pack,   duration_clocks: 64 }

dependencies:
  # cross-op RAW through L1; an op id resolves to its pack (source) / unpack (target)
  - { from: add, to: mul, kind: l1_data, min_gap_clocks: 0 }
```

The scheduler (`schedule.py`) computes each block's start as the longest path
(ASAP) over four kinds of precedence edge:

- **intra-op** — consecutive blocks of an op (the unpack→math→pack handshake).
- **resource serialization** — a resource is a singleton; same-resource blocks
  can't overlap.
- **lane serialization** — a TRISC dispatches in order; same-lane blocks can't
  overlap.
- **explicit dependencies** — e.g. `l1_data` (`c = a + b` then `e = d * c`:
  can't unpack `c` until it's packed to L1). `min_gap_clocks` adds sync latency.
- **virtual DEST banks** — DEST is split in software into `dest_banks` banks (2
  by default) so PACK can drain one bank while MATH fills another. A block sets
  `dest_bank: 0|1|…` (MATH writes it, PACK reads it); blocks sharing a bank
  serialize, so MATH can't reuse a bank until the PACK that last read it has
  finished. Alternating banks across ops is what lets MATH overlap PACK — see
  `examples/ops_dest_banks.yaml` (set every `dest_bank` to 0 to watch the
  makespan grow as it single-buffers).

Because placement is derived, *moving or resizing any block reflows everything
downstream automatically*. New semantic dependency `kind`s — `src_valid`,
`dst_valid`, `l1_data`, `issue_order` — join the cosmetic `dep`/`fifo`/`flow`.

```bash
tt-prism schedule examples/ops_add_then_mul.yaml   # print resolved timeline
tt-prism render   examples/ops_add_then_mul.yaml -o out.svg   # flatten + render
tt-prism validate examples/ops_add_then_mul.yaml   # also checks schedulability
```

This declarative form is intended to be easy to generate from a kernel — by a
human or an AI — for zero-day understanding of what a compute kernel does.

## Architecture

```
┌─────────────┐    yaml    ┌──────────────┐
│ diagram.yaml├───load────▶│   models.py  │  Pydantic data model
└─────────────┘            │  (Diagram)   │
       ▲                   └──────┬───────┘
       │                          │
       │ storage.dump             │
       │                          │  used by:
       │                          ▼
       │              ┌────────────────────────┐
       │              │ renderer/svg.py        │  pure-Python SVG generator
       │              │   render_svg(diagram)  │  (also feeds PDF via cairosvg)
       │              └────────────────────────┘
       │                          ▲
       │                          │
       │                          │
┌──────┴────────┐    HTTP    ┌────┴────────┐    HTML/JS    ┌──────────────┐
│   cli.py      ├───────────▶│  server.py  ├──────────────▶│  app.js      │
│  (typer)      │            │  (FastAPI)  │               │  (vanilla JS │
│   new         │            │             │               │  + SVG, no   │
│   validate    │            │  /api/      │               │  build step) │
│   render      │            │  diagram    │               │              │
│   serve───────┘            │  (GET/PUT)  │               │ renders SVG, │
└───────────────┘            │  render.svg │               │ drags, edits,│
                             └─────────────┘               │ saves        │
                                                           └──────────────┘
```

### Python ↔ Web bridge

The frontend talks to the backend over a tiny HTTP API:

| Endpoint           | Method | Purpose                                            |
|--------------------|--------|----------------------------------------------------|
| `/`                | GET    | Editor HTML shell                                   |
| `/api/diagram`     | GET    | Load YAML, return `{diagram: {...}}` JSON          |
| `/api/diagram`     | PUT    | Validate `{diagram: {...}}` body, write YAML back  |
| `/api/render.svg`  | GET    | Server-side SVG render of the on-disk diagram      |
| `/static/*`        | GET    | JS/CSS assets                                       |

The JS doesn't use a generated client — it consumes/produces JSON matching the
Pydantic schema directly. There's no auth, no CORS, no websocket — everything
is one-shot REST. See [`server.py`](src/tt_prism/server.py) and the round-trip
in [`app.js`](src/tt_prism/web/static/app.js) (`loadFromServer` / `saveToServer`).

### Renderer

The SVG renderer in [`renderer/svg.py`](src/tt_prism/renderer/svg.py) is pure
Python — no headless browser. It produces a self-contained SVG with:

- Time ruler with auto-thinned labels (no overlap at high zoom-out)
- Per-lane rows with a left gutter for the lane name
- Color-by-resource work-item bars
- **Cubic Bezier dependency arrows** with per-item fan-out: edges sharing a
  source attach to that source at distinct Y positions sorted by target lane,
  so parallel edges don't cross unnecessarily. Three tangent strategies:
  - horizontal tangents for forward-with-gap (classic flow curve)
  - vertical tangents for overlap (smooth vertical drop between lanes)
  - big lift over the top for back-edges
- PDF export via `cairosvg` (optional `[pdf]` extra)

The editor's JS mirrors the renderer geometrically so what you see while
editing matches what `tt-prism render` will produce.

## Repository layout

```
tt-prism/
├── pyproject.toml
├── src/tt_prism/
│   ├── cli.py                 # Typer CLI: new / validate / render / serve
│   ├── models.py              # Pydantic Diagram / Lane / Resource / WorkItem / Dependency
│   ├── storage.py             # YAML load/dump
│   ├── grid.py                # clock↔px Layout
│   ├── server.py              # FastAPI: REST API + static + template
│   ├── renderer/
│   │   ├── svg.py             # Pure-Python SVG renderer
│   │   └── pdf.py             # SVG → PDF via cairosvg
│   └── web/
│       ├── templates/editor.html
│       └── static/{app.js, styles.css, favicon.svg}
├── examples/
│   ├── matmul_minimal.yaml
│   └── sub_exp_reduce.yaml
└── tests/
    ├── test_models.py         # ref/duplicate/roundtrip validation
    └── test_renderer.py       # SVG output smoke tests
```

## Extending

### Add a field to the data model

1. Add it to the relevant Pydantic class in [`models.py`](src/tt_prism/models.py).
2. (Optional) Render it in [`renderer/svg.py`](src/tt_prism/renderer/svg.py)
   and [`web/static/app.js`](src/tt_prism/web/static/app.js).
3. (Optional) Read/write it in the inspector panel by editing
   [`web/templates/editor.html`](src/tt_prism/web/templates/editor.html) and
   `onInspectorSubmit` in `app.js`.

No code generation, no schema regeneration. The JSON exchanged with the editor
just gets the new field.

### Use as a Python library

```python
from tt_prism import storage
from tt_prism.models import Diagram, Lane, WorkItem
from tt_prism.renderer.svg import render_svg

diagram = Diagram(
    title="example",
    grid_clocks=8,
    lanes=[Lane(id="l0", name="TRISC 0", order=0)],
    work_items=[WorkItem(id="a", lane_id="l0", start_clock=0, duration_clocks=16)],
)
svg = render_svg(diagram)
storage.dump(diagram, "out.yaml")
```

### Programmatic diagram producers

If you're building something that emits diagrams (e.g. the perf simulator's
pipeline-assembler stage), construct a `Diagram` and write YAML via
`storage.dump`. Anything that conforms to the schema renders without further
plumbing.

## Tests

See [Running tests](#running-tests) under Usage.

## Status

This is a working tool — actively used to author the LLK team's pipeline
diagrams. Working today: op-authored diagrams with the constraint scheduler
(intra-op + resource/lane serialization + cross-op `l1_data` deps + virtual DEST
banks), CLI (`new`/`validate`/`schedule`/`render`/`serve`), and a web editor that
renders and live-reflows op-diagrams. Known gaps / planned work:

- Op-group drag / reorder and right-edge resize-drag in the editor (today: edit
  durations and DEST banks via the inspector)
- An op palette ("+ Matmul", "+ Reduce", …) and a validate-vs-auto-flow toggle
- Calibrating / validating durations against ttsim; importers from tracy traces
  and RTL wave dumps
- Per-op dataflow drill-down (see [`reference/`](reference/)) and modeled extras
  like expected bandwidth / NOP counts
- Undo/redo
