from __future__ import annotations

from tt_prism.flow import STAGE_ORDER, execute_ops, flow_payload, resolved_flow
from tt_prism.models import Block, Diagram, FlowStep, Lane, Op, Resource


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
