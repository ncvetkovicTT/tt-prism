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
# FACE-LEVEL flows: each operand tile is two face-pairs (F0/1 top, F2/3 bottom)
# so the flow view shows faces entering Src and being consumed into DEST.
SDPA_FLOW = [
    FlowStep(label="unpack K F0/1", data="K.01", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="unpack K F2/3", data="K.23", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="unpack Q F0/1", data="Q.01", reads=["l1_in"], writes=["srcb"]),
    FlowStep(label="unpack Q F2/3", data="Q.23", reads=["l1_in"], writes=["srcb"]),
    FlowStep(label="QK^T matmul (FPU)", data="S", reads=["srca", "srcb"], writes=["dest"], expr="scores = Q@K^T"),
    FlowStep(label="reduce-max (SFPU)", data="m", reads=["dest"], writes=["dest"], expr="running max"),
    FlowStep(label="exp F0/1 (SFPU)",   data="P.01", reads=["dest"], writes=["dest"], expr="online softmax"),
    FlowStep(label="exp F2/3 (SFPU)",   data="P.23", reads=["dest"], writes=["dest"]),
    FlowStep(label="unpack V F0/1", data="V.01", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="unpack V F2/3", data="V.23", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="PV matmul (FPU)",   data="O", reads=["srca", "dest"], writes=["dest"], expr="out += P@V"),
    FlowStep(label="reduce-sum + recip", data="l", reads=["dest"], writes=["dest"], expr="normalize"),
    FlowStep(label="pack out F0/1", data="out.01", reads=["dest"], writes=["l1_out"]),
    FlowStep(label="pack out F2/3", data="out.23", reads=["dest"], writes=["l1_out"]),
]

# Generic face-level matmul flow (activation × weight → output), reused by the
# matmul-kind ops so the whole decoder shows face-level data movement.
MATMUL_FACE_FLOW = [
    FlowStep(label="unpack act F0/1", data="A.01", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="unpack act F2/3", data="A.23", reads=["l1_in"], writes=["srca"]),
    FlowStep(label="unpack wt F0/1",  data="W.01", reads=["l1_in"], writes=["srcb"]),
    FlowStep(label="unpack wt F2/3",  data="W.23", reads=["l1_in"], writes=["srcb"]),
    FlowStep(label="matmul (FPU)",    data="acc",  reads=["srca", "srcb"], writes=["dest"], expr="acc += A·W"),
    FlowStep(label="pack out F0/1",   data="O.01", reads=["dest"], writes=["l1_out"]),
    FlowStep(label="pack out F2/3",   data="O.23", reads=["dest"], writes=["l1_out"]),
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
        op("qproj",  "q_proj (matmul)",     "matmul",  [(U,"trisc0",10),(F,"trisc1",220),(P,"trisc2",20)], flow=MATMUL_FACE_FLOW),
        op("kvproj", "kv_a/b_proj (matmul)","matmul",  [(U,"trisc0",8),(F,"trisc1",120),(P,"trisc2",16)], flow=MATMUL_FACE_FLOW),
        op("rope",   "rope + kv_cache",     "rope",    [(U,"trisc0",8),(F,"trisc1",24),(S,"trisc2",40),(P,"trisc2",20)]),
        op("sdpa",   "flash_mla SDPA",      "flash_attention",
           [(U,"trisc0",40),(F,"trisc1",300),(S,"trisc2",120),(P,"trisc2",30)], flow=SDPA_FLOW),
        op("sdpar",  "sdpa_reduce_to_all",  "reduce",  [(F,"trisc1",30),(P,"trisc2",16)]),
        op("oproj",  "post_sdpa kv_b2+o_proj","matmul",[(U,"trisc0",10),(F,"trisc1",260),(P,"trisc2",24)], flow=MATMUL_FACE_FLOW),
        # ---- Phase B: dense MLP (fused_ops/moe, routing disabled) ----
        op("rmsn2",  "rmsnorm_mlp",         "rmsnorm", [(U,"trisc0",8),(F,"trisc1",30),(S,"trisc2",20),(P,"trisc2",12)]),
        op("gateup", "gate_up_proj + silu", "matmul",  [(U,"trisc0",16),(F,"trisc1",280),(S,"trisc2",40),(P,"trisc2",24)], flow=MATMUL_FACE_FLOW),
        op("down",   "down_proj + shared_expert","matmul",[(U,"trisc0",16),(F,"trisc1",260),(S,"trisc2",30),(P,"trisc2",28)], flow=MATMUL_FACE_FLOW),
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

# Collective topologies verified against tt-metal aho/sdpa-ops:
#  - sdpa_reduce_to_all: ring all-reduce over axis 0 (rows)         (sdpa_reduce_to_all/op.py)
#  - o_proj all-reduce:  over axis 1 (cols)                          (attention_block/op.py:1035)
#  - AllGather:          ring over axis 0 (rows) after the all-reduce (attention_block/op.py:1055)
#  - reduce_to_one:      3-level tree to root (1,1)                   (reduce_to_one_b1/op.py:9)
# Rings are modeled as forward chains (acyclic) rather than closed cycles.

# 1. input broadcast: sender -> every other chip's rmsnorm_in (neighbor-exchange)
s = cid(*SENDER)
for row in range(MESH_ROWS):
    for col in range(MESH_COLS):
        if (row, col) != SENDER:
            D(f"{s}_rmsn1", f"{cid(row,col)}_rmsn1", "input bcast")
# 2. SDPA all-reduce: ring across the 4 rows per column (axis 0); result on all rows
for col in range(MESH_COLS):
    for row in range(MESH_ROWS - 1):
        D(f"{cid(row,col)}_sdpa", f"{cid(row+1,col)}_sdpar", "SDPA all-reduce (ring, axis 0)")
# 3. o_proj all-reduce across cols (axis 1)
for row in range(MESH_ROWS):
    D(f"{cid(row,1)}_oproj", f"{cid(row,0)}_oproj", "o_proj all-reduce (cols, axis 1)")
# 4. AllGather of the SP-sharded output across the 4 rows (axis 0), after o_proj -> feeds MLP
for col in range(MESH_COLS):
    for row in range(MESH_ROWS - 1):
        D(f"{cid(row,col)}_oproj", f"{cid(row+1,col)}_rmsn2", "AllGather (ring, axis 0)")
# 5. reduce-to-one: 3-level tree to root (1,1)=dev3
for col in range(MESH_COLS):
    D(f"{cid(0,col)}_comb", f"{cid(1,col)}_comb", "reduce-to-one L1 (leaf row0->row1)")
    D(f"{cid(3,col)}_comb", f"{cid(2,col)}_comb", "reduce-to-one L1 (leaf row3->row2)")
    D(f"{cid(2,col)}_comb", f"{cid(1,col)}_comb", "reduce-to-one L2 (row2->row1)")
D(f"{cid(1,0)}_comb", f"{cid(1,1)}_comb", "reduce-to-one L3 (col0->root)")

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


# ======================================================================
# Intra-device view: the 8 Tensix cores cooperating WITHIN one device.
# Each device runs the decoder across ~8 worker cores: a coordinator core
# (c0) RMSNorms + mcasts the activation; all 8 cores compute a shard of the
# distributed matmuls (q/kv/o-proj, then gate/up/down) and a slice of flash
# SDPA; partials are gathered back to c0. (Per-core work distribution is
# modeled coarsely/SPMD; the real grid assignment is finer.)
# ======================================================================
LANES = [Lane(id="trisc0", name="TRISC0/UNPACK", order=0),
         Lane(id="trisc1", name="TRISC1/FPU", order=1),
         Lane(id="trisc2", name="TRISC2/SFPU+PACK", order=2)]
RES = [Resource(id="unpack", name="UNPACK", color="#ef9a9a"),
       Resource(id="fpu", name="FPU", color="#a5d6a7"),
       Resource(id="sfpu", name="SFPU", color="#90caf9"),
       Resource(id="pack", name="PACK", color="#ffcc80")]
NCORES = 8

dcores, dops, ddeps = [], [], []
for i in range(NCORES):
    cc = f"c{i}"
    role = " [coordinator]" if i == 0 else ""
    dcores.append(Core(id=cc, x=i % 4, y=i // 4, name=f"core {i}{role}"))

    def dop(suffix, name, kind, blocks, flow=None, core=None):
        cc_ = core
        bl = [Block(id=f"{cc_}_{suffix}_{r}", lane_id=lane, resource_id=r, label=r, duration_clocks=d)
              for (r, lane, d) in blocks]
        return Op(id=f"{cc_}_{suffix}", name=name, kind=kind, core_id=cc_, tiles=4, blocks=bl, flow=flow or [])

    U, F, S, P = "unpack", "fpu", "sfpu", "pack"
    seq = []
    seq.append(dop("init", "decoder_init", "init", [(F, "trisc1", 12)], core=cc))
    if i == 0:
        seq.append(dop("rmsn", "rmsnorm_in (coordinator)", "rmsnorm",
                       [(U, "trisc0", 8), (F, "trisc1", 30), (S, "trisc2", 20), (P, "trisc2", 12)], core=cc))
    seq.append(dop("attn", "attn proj shard (matmul)", "matmul",
                   [(U, "trisc0", 10), (F, "trisc1", 200), (P, "trisc2", 18)], flow=MATMUL_FACE_FLOW, core=cc))
    seq.append(dop("sdpa", "flash SDPA shard", "flash_attention",
                   [(U, "trisc0", 30), (F, "trisc1", 220), (S, "trisc2", 90), (P, "trisc2", 24)],
                   flow=SDPA_FLOW, core=cc))
    seq.append(dop("mlp", "MLP proj shard (gate/up/down)", "matmul",
                   [(U, "trisc0", 14), (F, "trisc1", 280), (S, "trisc2", 30), (P, "trisc2", 22)], flow=MATMUL_FACE_FLOW, core=cc))
    if i == 0:
        seq.append(dop("comb", "gather + combine (coordinator)", "eltwise",
                       [(U, "trisc0", 8), (F, "trisc1", 40), (P, "trisc2", 16)], core=cc))
    dops.extend(seq)
    for a, b in zip(seq, seq[1:]):
        ddeps.append(Dependency.model_validate({"from": a.id, "to": b.id, "kind": "l1_data"}))

def DD(frm, to, label, gap=20):
    ddeps.append(Dependency.model_validate({"from": frm, "to": to, "kind": "noc", "min_gap_clocks": gap, "label": label}))

# intra-device collectives: c0 mcasts normalized activation to peers; peers gather partials back
for i in range(1, NCORES):
    DD("c0_rmsn", f"c{i}_attn", "RMSNorm mcast → peers")
for i in range(1, NCORES):
    DD(f"c{i}_mlp", "c0_comb", "partial gather → c0")

ddiagram = Diagram(
    title="DeepSeek-V3 decoder — 8 Tensix cores within ONE device",
    clock_ghz=1.0, grid_clocks=128, dest_banks=2,
    cores=dcores, lanes=LANES, resources=RES, ops=dops, dependencies=ddeps,
)
out2 = Path(__file__).with_name("decoder_mlp_device8.yaml")
storage.dump(ddiagram, out2)
print(f"wrote {out2}  ({len(dcores)} cores, {len(dops)} ops, {len(ddeps)} deps)")
