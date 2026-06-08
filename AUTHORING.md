# Authoring tt-prism models — guide for humans & LLMs

How to map an AI model layer / kernel / workload into a tt-prism YAML. This is
written so a person *or* an LLM (given a kernel and these rules) can fill it out.
There is a copy-paste **LLM prompt** at the end.

---

## 0. Mental model (read this first)

- A diagram describes a **workload** as a set of **ops** scheduled across one or
  more **Tensix cores**. The scheduler *derives* all timing; you author the
  **structure** (what runs where, in what order) and **durations**.
- **Granularity rule:** one **op = one Compute API call** (e.g. `mm_init`,
  `matmul_tiles`, `reduce_tile`, `pack_untilize`). **Do not** model individual
  LLK / Tensix instructions — fold their time into the op that wraps them.
- Each Tensix core has **3 dispatch threads** (TRISC0/1/2) and **4 execution
  resources** we model: **UNPACK, FPU (matrix), SFPU (vector), PACK**. THCON /
  sync time is folded into whichever resource wraps it.
- Data flows: **L1 → (UNPACK) → SrcA/SrcB → (FPU) → DEST → (SFPU on DEST) →
  (PACK) → L1**. Cross-core/chip movement is the **NoC**.

---

## 1. The mapping recipe (workload → YAML)

Work top-down, then fill detail:

1. **Topology.** How many chips, and how many Tensix cores per chip are used?
   - 1 core → omit `cores` (single implicit core).
   - N cores/chips → list `cores: [{id, x, y, name}]`. Use `(x,y)` = grid
     position (chip mesh coord, or core coord within a chip — pick one scale per
     diagram; model the other scale in a second diagram if needed).
2. **List the ops in program order.** Each op = one compute API call. For each:
   name, `kind` (free-form: matmul/reduce/rmsnorm/…), `core_id` (which core),
   `tiles`, and whether it's **init** or **execute** (see §3).
3. **Resource blocks per op.** Which engines does the op use, on which TRISC
   lane, for how long? One `block` per (resource, lane) with a `duration_clocks`
   estimate. List blocks in execution order (they serialize within the op).
4. **Data dependencies.** Producer → consumer edges. `l1_data` within a core;
   `noc` across cores/chips. `min_gap_clocks` adds transfer/sync latency.
5. **(Optional) Dataflow `flow`** for execute ops you want to drill into: an
   ordered list of steps moving a named `data` datum between **stages**
   (`l1_in/srca/srcb/dest/sfpu/l1_out`), with an `expr`. Powers the flow view.
6. **DEST double-buffering.** Alternate `dest_bank: 0/1` across ops so MATH can
   fill one bank while PACK drains the other (models overlap).

Then `tt-prism validate <file>` → `tt-prism schedule <file>` → `tt-prism serve <file>`.

---

## 2. Field reference (the schema)

**Diagram (top level)**
| field | meaning |
|---|---|
| `title` | label |
| `clock_ghz` | optional; enables ns labels |
| `grid_clocks` | ruler tick / snap unit |
| `dest_banks` | # virtual DEST banks (default 2) |
| `cores` | list of `Core` (omit for single core) |
| `lanes` | the TRISC dispatch rows (template, instantiated per core) |
| `resources` | UNPACK/FPU/SFPU/PACK definitions (id + color) |
| `ops` | the workload (authoring unit) |
| `dependencies` | producer→consumer edges |

**Core** `{ id, x, y, name }` — a Tensix core or a device, at grid `(x,y)`.
**Lane** `{ id, name, order }` — a TRISC row (typically trisc0/1/2).
**Resource** `{ id, name, color }` — usually `unpack/fpu/sfpu/pack`.

**Op**
| field | meaning |
|---|---|
| `id` | unique |
| `name` | the compute API call, e.g. `"reduce_tile"` |
| `kind` | free-form category |
| `tiles` | # tiles operated on (≥1) |
| `core_id` | which `Core` (required when >1 core) |
| `category` | `init` or `execute`; omit to infer from name/kind (§3) |
| `blocks` | resource blocks, in execution order |
| `flow` | optional dataflow steps (flow view) |

**Block** `{ id, lane_id, resource_id, label, duration_clocks, dest_bank }`
— one engine held for a duration, dispatched by one TRISC lane. `dest_bank`
(int or omit) for MATH/PACK blocks that touch DEST.

**FlowStep** `{ id, label, reads:[stage], writes:[stage], data, expr, note }`
— stages ∈ `l1_in, srca, srcb, dest, sfpu, l1_out`. `data` names the datum/token
(e.g. `"Q"`, `"scores"`); defaults to `label`.

**Dependency** `{ from, to, kind, min_gap_clocks, label }`
— `kind`: `l1_data` (RAW within a core), `noc` (cross-core/chip transfer),
`src_valid`/`dst_valid` (handshakes), `issue_order`; `fifo`/`flow`/`dep`
(cosmetic). Endpoints may be an op id, a block id, or (for `noc`) op ids on
different cores. `min_gap_clocks` = latency the consumer waits beyond producer end.

---

## 3. How to decide each thing (conventions)

- **Compute-API → resource:**
  - matmul / `mm_block` / elementwise-on-DEST / copy / MOVD2B → **FPU**
  - reduce / exp / recip / sigmoid / SiLU / any vector/LReg op → **SFPU**
  - `unpack_*` (L1→Src) → **UNPACK**; `pack_*` (DEST→L1) → **PACK**
  - THCON / semaphores / sync → fold into the wrapping block's resource.
- **Lanes:** conventionally TRISC0=UNPACK, TRISC1=FPU, TRISC2=PACK — **but check
  the kernel**: SFPU vector ops are sometimes dispatched by the PACK thread
  (then put SFPU + PACK on the same lane, e.g. trisc2).
- **init vs execute:** an op whose name/kind contains `init`/`reinit` (e.g.
  `mm_init`) is **init** (config only, no dataflow, greyed in the flow view).
  Everything else is **execute**. Override with `category:` when a name is
  misleading.
- **Durations:** relative integers on a common scale. Rules of thumb:
  matmul ∝ (K·N tiles), reduce/exp ∝ (tiles), unpack/pack ∝ (tiles). Calibrate
  against a ttsim / tracy run later — they're user-supplied estimates.
- **Multi-core / multi-chip:** each Tensix core *or* device = one `Core (x,y)`.
  Collectives map to `noc` deps between the involved cores:
  - **broadcast / mcast**: sender op → each receiver op
  - **all-reduce / reduce-scatter**: edges between the peers on the reduce axis
  - **reduce-to-one / gather**: every contributor op → the root op
  - tag the `noc` `label` with the payload (e.g. `"o_proj partial [1,7168]"`).
- **Flow steps:** one step = one datum moved. Split per operand for clarity
  (unpack Q, unpack K as separate steps). A matmul step `reads:[srca,srcb]
  writes:[dest]` shows operands combining into a result in DEST. Name `data`
  meaningfully (Q/K/V/scores/probs/output) so tokens are legible.

---

## 4. Worked micro-example (annotated)

```yaml
title: "matmul + exp (one core)"
clock_ghz: 1.0
lanes:     [ {id: trisc0, name: UNPACK, order: 0}, {id: trisc1, name: FPU, order: 1}, {id: trisc2, name: SFPU+PACK, order: 2} ]
resources: [ {id: unpack, name: UNPACK}, {id: fpu, name: FPU}, {id: sfpu, name: SFPU}, {id: pack, name: PACK} ]
ops:
  - { id: mmi, name: mm_init, kind: init, blocks: [ {id: mmi_c, lane_id: trisc1, resource_id: fpu, duration_clocks: 16} ] }
  - id: mm
    name: matmul_exp
    kind: matmul
    tiles: 4
    flow:                                  # one datum per step; faces split out
      - { label: "unpack A", data: A, reads: [l1_in], writes: [srca] }
      - { label: "unpack B", data: B, reads: [l1_in], writes: [srcb] }
      - { label: "matmul",   data: acc, reads: [srca, srcb], writes: [dest], expr: "DEST += A·B" }
      - { label: "exp",      data: y,   reads: [dest], writes: [sfpu], expr: "exp(acc)" }
      - { label: "pack",     data: out, reads: [dest], writes: [l1_out] }
    blocks:
      - { id: mm_u, lane_id: trisc0, resource_id: unpack, duration_clocks: 60, dest_bank: 0 }
      - { id: mm_f, lane_id: trisc1, resource_id: fpu,    duration_clocks: 48, dest_bank: 0 }
      - { id: mm_s, lane_id: trisc2, resource_id: sfpu,   duration_clocks: 64, dest_bank: 0 }
      - { id: mm_p, lane_id: trisc2, resource_id: pack,   duration_clocks: 32, dest_bank: 0 }
dependencies:
  - { from: mmi, to: mm, kind: l1_data }
```

Bigger, real references: see [`examples/deepseek/`](examples/deepseek/) — a flash
SDPA op (`sdpa_chunk.yaml`), an 8-device mesh (`decoder_mlp_chip.yaml`), and the
8-cores-within-a-device view (`decoder_mlp_device8.yaml`), with a generator
(`gen_decoder_mlp.py`) showing how to emit SPMD pipelines programmatically.

---

## 5. LLM prompt (copy-paste)

> You convert a Tenstorrent compute workload into a **tt-prism** YAML perf model.
> Follow the schema and rules in `AUTHORING.md` (summarized below). Output only YAML.
>
> Rules:
> - One **op** = one Compute API call; never model individual LLK/Tensix
>   instructions (fold their time in). Resources: UNPACK, FPU, SFPU, PACK
>   (matmul/eltwise→FPU; reduce/exp/recip/silu→SFPU; unpack_*→UNPACK; pack_*→PACK;
>   THCON/sync folded in). Lanes trisc0/1/2 (SFPU may share the PACK lane).
> - Each op has `blocks` (one per resource it uses, with a `duration_clocks`
>   estimate; matmul∝K·N tiles, reduce/exp∝tiles) in execution order, and may have
>   a `flow` (steps moving a named `data` datum across stages
>   l1_in/srca/srcb/dest/sfpu/l1_out with an `expr`).
> - Ops whose name/kind contains "init"/"reinit" are `category: init` (no flow).
> - One `Core {id,x,y,name}` per Tensix core or device. Within-core data deps are
>   `l1_data`; cross-core/chip transfers (mcast/all-reduce/gather/NoC) are `noc`
>   deps between ops on different cores, with `min_gap_clocks` latency and a
>   `label` naming the payload. Alternate `dest_bank: 0/1` across ops for overlap.
> - Validate mentally: every op references a defined core/lane/resource; deps
>   reference real op/block ids; the dependency graph is acyclic.
>
> **Workload to model:** <paste the kernel source, op sequence, model layer, and
> tensor shapes / device count here>.
>
> Produce the tt-prism YAML.

After generating, run `tt-prism validate <file>` (it also checks schedulability)
and `tt-prism serve <file>` to inspect the Gantt and flow views.
