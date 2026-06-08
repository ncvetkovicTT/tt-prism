"""Generate examples/deepseek/decoder_mlp_chip.yaml — a tt-prism model of the whole
`test_decoder_mlp` run (DeepSeek-V3 decoder block) across an 8-chip Blackhole 4x2 mesh.

Each chip runs the same SPMD pipeline (attention/MLA incl. flash SDPA, then dense MLP);
the four cross-chip collectives are modeled as `noc` dependencies:
  1. input broadcast        sender chip (1,0)=dev2 -> all
  2. SDPA reduce (rows)      within each column, rows 1-3 -> row 0   (sdpa_cluster_axis=0)
  3. o_proj all-reduce (cols) col 1 -> col 0 in each row             (reduce_cluster_axis=1)
  4. reduce-to-one           all chips -> root chip (1,1)=dev3

Durations are coarse proportional estimates (this test can't run on the single
Wormhole card — it needs 8 Blackhole chips — so they are modelled, not measured).

Run:  PYTHONPATH=src python examples/deepseek/gen_decoder_mlp.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tt_prism import storage
from tt_prism.models import Block, Core, Dependency, Diagram, FlowStep, Lane, Op, Resource

MESH_ROWS, MESH_COLS = 4, 2          # test parametrization (test_decoder_block.py:859)
SENDER = (1, 0)                      # input sender chip (test_decoder_block.py:853) -> dev2
ROOT = (1, 1)                        # reduce-to-one root (reduce_root_coord default) -> dev3

def dev(row, col):       return row * MESH_COLS + col
def cid(row, col):       return f"dev{dev(row,col)}"

# Per-chip op pipeline: (suffix, name, kind, [(resource, lane, duration)], optional flow)
#   lanes: trisc0=UNPACK, trisc1=FPU, trisc2=SFPU+PACK
SDPA_FLOW = [
    FlowStep(label="unpack Q,K",        reads=["l1_in"], writes=["srca", "srcb"]),
    FlowStep(label="QK^T matmul (FPU)", reads=["srca", "srcb"], writes=["dest"], expr="scores = Q@K^T"),
    FlowStep(label="reduce-max (SFPU)", reads=["dest"], writes=["dest"], expr="running max"),
    FlowStep(label="exp / correction",  reads=["dest"], writes=["dest"], expr="online softmax"),
    FlowStep(label="PV matmul (FPU)",   reads=["srca", "dest"], writes=["dest"], expr="out += P@V"),
    FlowStep(label="reduce-sum + recip",reads=["dest"], writes=["dest"], expr="normalize"),
    FlowStep(label="pack out",          reads=["dest"], writes=["l1_out"]),
]

def pipeline(chip):
    """Return the ordered ops for one chip (see fused_ops/* and micro_ops/* in tt-metal)."""
    def op(suffix, name, kind, blocks, flow=None):
        bl = [Block(id=f"{chip}_{suffix}_{r}", lane_id=lane, resource_id=r, label=r, duration_clocks=d)
              for (r, lane, d) in blocks]
        return Op(id=f"{chip}_{suffix}", name=name, kind=kind, core_id=chip, tiles=4,
                  blocks=bl, flow=flow or [])
    U, F, S, P = "unpack", "fpu", "sfpu", "pack"
    return [
        op("init",   "decoder_init",       "init",    [(F, "trisc1", 16)]),
        # ---- Phase A: MLA attention (fused_ops/attention_block) ----
        op("rmsn1",  "rmsnorm_in",          "rmsnorm", [(U,"trisc0",8),(F,"trisc1",30),(S,"trisc2",20),(P,"trisc2",12)]),
        op("qproj",  "q_proj (matmul)",     "matmul",  [(U,"trisc0",10),(F,"trisc1",220),(P,"trisc2",20)]),
        op("kvproj", "kv_a/b_proj (matmul)","matmul",  [(U,"trisc0",8),(F,"trisc1",120),(P,"trisc2",16)]),
        op("rope",   "rope + kv_cache",     "rope",    [(U,"trisc0",8),(F,"trisc1",24),(S,"trisc2",40),(P,"trisc2",20)]),
        op("sdpa",   "flash_mla SDPA",      "flash_attention",
           [(U,"trisc0",40),(F,"trisc1",300),(S,"trisc2",120),(P,"trisc2",30)], flow=SDPA_FLOW),
        op("sdpar",  "sdpa_reduce_to_all",  "reduce",  [(F,"trisc1",30),(P,"trisc2",16)]),
        op("oproj",  "post_sdpa kv_b2+o_proj","matmul",[(U,"trisc0",10),(F,"trisc1",260),(P,"trisc2",24)]),
        # ---- Phase B: dense MLP (fused_ops/moe, routing disabled) ----
        op("rmsn2",  "rmsnorm_mlp",         "rmsnorm", [(U,"trisc0",8),(F,"trisc1",30),(S,"trisc2",20),(P,"trisc2",12)]),
        op("gateup", "gate_up_proj + silu", "matmul",  [(U,"trisc0",16),(F,"trisc1",280),(S,"trisc2",40),(P,"trisc2",24)]),
        op("down",   "down_proj + shared_expert","matmul",[(U,"trisc0",16),(F,"trisc1",260),(S,"trisc2",30),(P,"trisc2",28)]),
        op("comb",   "combine + reduce_to_one","eltwise",[(U,"trisc0",8),(F,"trisc1",40),(P,"trisc2",16)]),
    ]

# ---- build all chips ----
cores, ops, deps = [], [], []
for row in range(MESH_ROWS):
    for col in range(MESH_COLS):
        c = cid(row, col)
        nm = f"chip{dev(row,col)} ({row},{col})"
        if (row, col) == SENDER: nm += " [sender]"
        if (row, col) == ROOT:   nm += " [root]"
        cores.append(Core(id=c, x=col, y=row, name=nm))
        chip_ops = pipeline(c)
        ops.extend(chip_ops)
        # intra-chip data chain (init -> ... -> combine)
        for a, b in zip(chip_ops, chip_ops[1:]):
            deps.append(Dependency.model_validate({"from": a.id, "to": b.id, "kind": "l1_data"}))

def D(frm, to, label, gap=60):
    deps.append(Dependency.model_validate(
        {"from": frm, "to": to, "kind": "noc", "min_gap_clocks": gap, "label": label}))

# 1. input broadcast: sender -> every other chip's rmsnorm_in
s = cid(*SENDER)
for row in range(MESH_ROWS):
    for col in range(MESH_COLS):
        if (row, col) != SENDER:
            D(f"{s}_rmsn1", f"{cid(row,col)}_rmsn1", "input bcast")
# 2. SDPA reduce across rows (axis 0): rows 1-3 -> row 0, per column
for col in range(MESH_COLS):
    for row in range(1, MESH_ROWS):
        D(f"{cid(row,col)}_sdpa", f"{cid(0,col)}_sdpar", "SDPA reduce (rows)")
# 3. o_proj all-reduce across cols (axis 1): col1 -> col0 per row
for row in range(MESH_ROWS):
    D(f"{cid(row,1)}_oproj", f"{cid(row,0)}_oproj", "o_proj all-reduce (cols)")
# 4. reduce-to-one: every chip -> root
r = cid(*ROOT)
for row in range(MESH_ROWS):
    for col in range(MESH_COLS):
        if (row, col) != ROOT:
            D(f"{cid(row,col)}_comb", f"{r}_comb", "reduce-to-one -> root")

diagram = Diagram(
    title="DeepSeek-V3 decoder (test_decoder_mlp) — 8-chip Blackhole 4x2 mesh",
    clock_ghz=1.0, grid_clocks=128, dest_banks=2,
    cores=cores,
    lanes=[Lane(id="trisc0", name="TRISC0/UNPACK", order=0),
           Lane(id="trisc1", name="TRISC1/FPU", order=1),
           Lane(id="trisc2", name="TRISC2/SFPU+PACK", order=2)],
    resources=[Resource(id="unpack", name="UNPACK", color="#ef9a9a"),
               Resource(id="fpu", name="FPU", color="#a5d6a7"),
               Resource(id="sfpu", name="SFPU", color="#90caf9"),
               Resource(id="pack", name="PACK", color="#ffcc80")],
    ops=ops, dependencies=deps,
)
out = Path(__file__).with_name("decoder_mlp_chip.yaml")
storage.dump(diagram, out)
print(f"wrote {out}  ({len(cores)} chips, {len(ops)} ops, {len(deps)} deps)")
