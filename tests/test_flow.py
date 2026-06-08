from __future__ import annotations

from tt_prism.flow import STAGE_ORDER, chip_payload, execute_ops, flow_payload, resolved_flow
from tt_prism.models import Block, Core, Dependency, Diagram, FlowStep, Lane, Op, Resource


def _op(op_id, *, name="", kind="", category=None, flow=None, blocks=None):
    return Op(
        id=op_id, name=name, kind=kind, category=category,
        flow=flow or [],
        blocks=blocks or [
            Block(id=f"{op_id}_u", lane_id="t0", resource_id="unpack", duration_clocks=10),
            Block(id=f"{op_id}_m", lane_id="t1", resource_id="fpu", duration_clocks=10),
            Block(id=f"{op_id}_p", lane_id="t2", resource_id="pack", duration_clocks=10),
        ],
    )


def _diagram(ops):
    return Diagram(
        title="t",
        lanes=[Lane(id="t0", name="T0", order=0), Lane(id="t1", name="T1", order=1),
               Lane(id="t2", name="T2", order=2)],
        resources=[Resource(id="unpack", name="U"), Resource(id="fpu", name="F"),
                   Resource(id="sfpu", name="S"), Resource(id="pack", name="P")],
        ops=ops,
    )


# ---- init vs execute inference ----

def test_init_inferred_from_name():
    assert _op("a", name="mm_init").is_init
    assert _op("a", name="reduce_reinit").is_init        # reinit contains "init"
    assert _op("a", kind="hw_init").is_init
    assert not _op("a", name="matmul", kind="matmul").is_init


def test_explicit_category_overrides_inference():
    # name says init, but explicit category wins
    assert not _op("a", name="weird_init_name", category="execute").is_init
    assert _op("a", name="matmul", category="init").is_init


def test_execute_ops_excludes_init():
    d = _diagram([_op("i", name="mm_init"), _op("mm", name="matmul")])
    assert [o.id for o in execute_ops(d)] == ["mm"]


# ---- flow resolution (hybrid) ----

def test_derived_flow_from_blocks():
    steps = resolved_flow(_op("mm", name="matmul"))
    assert [s.reads for s in steps] == [["l1_in"], ["srca", "srcb"], ["dest"]]
    assert [s.writes for s in steps] == [["srca", "srcb"], ["dest"], ["l1_out"]]


def test_explicit_flow_takes_precedence():
    flow = [
        FlowStep(id="s1", label="unpack", reads=["l1_in"], writes=["srca"]),
        FlowStep(id="s2", label="MOVD2B reuse", reads=["dest"], writes=["srcb"]),
        FlowStep(id="s3", label="pack", reads=["dest"], writes=["l1_out"], expr="DEST = exp(A)"),
    ]
    op = _op("mm", name="matmul", flow=flow)
    steps = resolved_flow(op)
    assert [s.label for s in steps] == ["unpack", "MOVD2B reuse", "pack"]
    assert steps[2].expr == "DEST = exp(A)"


# ---- payload ----

def test_flow_payload_shape_and_stage_usage():
    explicit = [
        FlowStep(label="unpack", reads=["l1_in"], writes=["srca", "srcb"]),
        FlowStep(label="load SFPU", reads=["dest"], writes=["sfpu"]),
        FlowStep(label="store DEST", reads=["sfpu"], writes=["dest"]),
        FlowStep(label="pack", reads=["dest"], writes=["l1_out"]),
    ]
    d = _diagram([_op("i", name="mm_init"), _op("mm", name="matmul", flow=explicit)])
    p = flow_payload(d)
    assert p["stage_order"] == STAGE_ORDER
    by_id = {o["id"]: o for o in p["ops"]}
    assert by_id["i"]["is_init"] is True
    mm = by_id["mm"]
    assert mm["is_init"] is False and mm["derived"] is False
    # stages used are reported in canonical order and include sfpu (from explicit flow)
    assert mm["stages"] == ["l1_in", "srca", "srcb", "dest", "sfpu", "l1_out"]
    assert mm["steps"][1]["writes"] == ["sfpu"]


def test_derived_flow_marked_derived():
    d = _diagram([_op("mm", name="matmul")])
    assert flow_payload(d)["ops"][0]["derived"] is True


def test_init_ops_emit_empty_flow():
    d = _diagram([_op("i", name="mm_init")])
    o = flow_payload(d)["ops"][0]
    assert o["is_init"] is True and o["stages"] == [] and o["steps"] == []


def _mc_diagram(ops, deps):
    return Diagram(
        title="chip",
        cores=[Core(id="c00", x=0, y=0), Core(id="c10", x=1, y=0), Core(id="c20", x=2, y=0)],
        lanes=[Lane(id="t0", name="T0", order=0), Lane(id="t1", name="T1", order=1),
               Lane(id="t2", name="T2", order=2)],
        resources=[Resource(id="unpack", name="U"), Resource(id="fpu", name="F"),
                   Resource(id="sfpu", name="S"), Resource(id="pack", name="P")],
        ops=ops, dependencies=deps,
    )


def _mc_op(op_id, core):
    return Op(id=op_id, name=op_id, core_id=core, blocks=[
        Block(id=f"{op_id}_u", lane_id="t0", resource_id="unpack", duration_clocks=10),
        Block(id=f"{op_id}_p", lane_id="t2", resource_id="pack", duration_clocks=10)])


def test_chip_payload_cores_and_ops():
    d = _mc_diagram([_mc_op("a", "c00"), _mc_op("b", "c10"), _mc_op("c", "c20")], [])
    p = chip_payload(d)
    assert p["multicore"] is True
    assert [c["id"] for c in p["cores"]] == ["c00", "c10", "c20"]
    c00 = next(c for c in p["cores"] if c["id"] == "c00")
    assert [o["id"] for o in c00["ops"]] == ["a"]
    assert c00["x"] == 0 and c00["y"] == 0


def test_chip_interactions_are_cross_core_only_and_ordered():
    deps = [
        Dependency.model_validate({"from": "a", "to": "b", "kind": "noc", "label": "A→B"}),
        Dependency.model_validate({"from": "b", "to": "c", "kind": "noc"}),
    ]
    d = _mc_diagram([_mc_op("a", "c00"), _mc_op("b", "c10"), _mc_op("c", "c20")], deps)
    its = chip_payload(d)["interactions"]
    assert [(i["from_core"], i["to_core"]) for i in its] == [("c00", "c10"), ("c10", "c20")]
    assert its[0]["label"] == "A→B"


def test_chip_same_core_dep_is_not_an_interaction():
    # two ops on the SAME core with a dep → not a NoC interaction
    deps = [Dependency.model_validate({"from": "a", "to": "a2", "kind": "l1_data"})]
    d = _mc_diagram([_mc_op("a", "c00"), _mc_op("a2", "c00")], deps)
    assert chip_payload(d)["interactions"] == []


def test_chip_single_core_diagram():
    d = Diagram(
        title="one", lanes=[Lane(id="t0", name="T0", order=0)],
        resources=[Resource(id="unpack", name="U"), Resource(id="pack", name="P")],
        ops=[Op(id="a", name="a", blocks=[Block(id="a_u", lane_id="t0", resource_id="unpack")])],
    )
    p = chip_payload(d)
    assert p["multicore"] is False
    assert len(p["cores"]) == 1 and p["interactions"] == []


def test_stage_usage_invariant_holds():
    # Every step's read/write stage must be in the op's `stages`, and `stages`
    # must be the canonical-ordered subset actually used. flow.js token
    # placement/highlighting depends on this.
    explicit = [
        FlowStep(label="u", reads=["l1_in"], writes=["srca", "srcb"]),
        FlowStep(label="m", reads=["srca", "srcb"], writes=["dest"]),
        FlowStep(label="s", reads=["dest"], writes=["sfpu"]),
        FlowStep(label="p", reads=["sfpu", "dest"], writes=["l1_out"]),
    ]
    d = _diagram([_op("mm", name="matmul", flow=explicit), _op("cpy", name="copy")])
    for o in flow_payload(d)["ops"]:
        used = {s for st in o["steps"] for s in (*st["reads"], *st["writes"])}
        assert used <= set(o["stages"])                       # no orphan target stage
        assert o["stages"] == [s for s in STAGE_ORDER if s in used]   # canonical order


def test_flow_step_data_identity():
    # explicit data is emitted; empty data falls back to the step label
    explicit = [
        FlowStep(label="Unpack A", data="A", reads=["l1_in"], writes=["srca"]),
        FlowStep(label="exp", reads=["dest"], writes=["sfpu"]),   # no data -> label
    ]
    d = _diagram([_op("mm", name="matmul", flow=explicit)])
    steps = flow_payload(d)["ops"][0]["steps"]
    assert steps[0]["data"] == "A"
    assert steps[1]["data"] == "exp"


def test_derived_flow_has_datum_labels():
    d = _diagram([_op("mm", name="matmul")])      # unpack -> fpu -> pack
    data = [s["data"] for s in flow_payload(d)["ops"][0]["steps"]]
    assert data == ["operands", "result", "output"]
