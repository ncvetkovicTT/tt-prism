"""Contract tests for the data the web layer consumes, plus scheduler soundness
and a cheap static guard on flow.js.

These cover the *Python data preconditions* that the browser-only flow walkthrough
relies on. We cannot execute flow.js here (no node/browser), so the JS check is
explicitly STATIC (bracket balance + no dead references), and the behavioral GUI
checks live in tests/MANUAL_GUI_CHECKLIST.md.
"""

from __future__ import annotations

import glob
import os

import pytest

from tt_prism import storage
from tt_prism.flow import STAGE_ORDER, chip_payload, flow_payload
from tt_prism.models import (
    Block,
    Core,
    Dependency,
    Diagram,
    Lane,
    Op,
    Resource,
    Transfer,
)
from tt_prism.schedule import ScheduleError, solve

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_JS = os.path.join(_REPO_ROOT, "src", "tt_prism", "web", "static", "flow.js")
EXAMPLES = sorted(
    glob.glob(os.path.join(_REPO_ROOT, "examples", "**", "*.yaml"), recursive=True)
)


def _multicore_examples() -> list[str]:
    out = []
    for p in EXAMPLES:
        d = storage.load(p)
        if len(d.cores) > 1:
            out.append(p)
    return out


MULTICORE_EXAMPLES = _multicore_examples()


# --------------------------------------------------------------------------
# A small diagram with one compute op, one movement op, one init op.
# --------------------------------------------------------------------------
def _mixed_diagram() -> Diagram:
    cores = [Core(id="c0", x=0, y=0), Core(id="c1", x=1, y=0)]
    lanes = [Lane(id="t0", name="U", order=0), Lane(id="t1", name="M", order=1),
             Lane(id="t2", name="P", order=2)]
    resources = [Resource(id="unpack", name="U"), Resource(id="fpu", name="F"),
                 Resource(id="pack", name="P")]
    ops = [
        Op(id="ini", name="mm_init", core_id="c0",
           blocks=[Block(id="ini_b", lane_id="t0", resource_id="unpack", duration_clocks=4)]),
        Op(id="mm", name="matmul", kind="matmul", core_id="c0", blocks=[
            Block(id="mm_u", lane_id="t0", resource_id="unpack", duration_clocks=10),
            Block(id="mm_m", lane_id="t1", resource_id="fpu", duration_clocks=8),
            Block(id="mm_p", lane_id="t2", resource_id="pack", duration_clocks=10)]),
        Op(id="bc", name="broadcast", kind="broadcast",
           transfers=[Transfer.model_validate({"from": "c0", "to": "c1", "label": "x"})]),
    ]
    return Diagram(title="mixed", cores=cores, lanes=lanes, resources=resources, ops=ops)


# ---- flow_payload contract ----

def test_flow_payload_cores_present_and_ops_carry_required_fields():
    p = flow_payload(_mixed_diagram())
    assert [c["id"] for c in p["cores"]] == ["c0", "c1"]
    required = {"is_movement", "is_init", "transfers", "on_cores", "steps", "stages"}
    for op in p["ops"]:
        assert required <= set(op), f"op {op['id']} missing keys {required - set(op)}"


def test_flow_payload_compute_steps_within_stages_and_canonical():
    p = flow_payload(_mixed_diagram())
    mm = next(o for o in p["ops"] if o["id"] == "mm")
    assert mm["is_movement"] is False and mm["is_init"] is False
    used = set()
    for step in mm["steps"]:
        for s in (*step["reads"], *step["writes"]):
            used.add(s)
        # every stage a step touches is declared in the op's stages
        assert set(step["reads"]) <= set(mm["stages"])
        assert set(step["writes"]) <= set(mm["stages"])
    # stages are exactly the canonical-ordered subset actually used
    assert mm["stages"] == [s for s in STAGE_ORDER if s in used]


def test_flow_payload_movement_op_has_transfers_to_real_cores_and_no_steps():
    p = flow_payload(_mixed_diagram())
    core_ids = {c["id"] for c in p["cores"]}
    bc = next(o for o in p["ops"] if o["id"] == "bc")
    assert bc["is_movement"] is True
    assert bc["steps"] == []
    assert bc["transfers"], "movement op should carry transfers"
    for t in bc["transfers"]:
        assert t["from"] in core_ids and t["to"] in core_ids


def test_flow_payload_init_op_has_no_steps():
    p = flow_payload(_mixed_diagram())
    ini = next(o for o in p["ops"] if o["id"] == "ini")
    assert ini["is_init"] is True
    assert ini["steps"] == [] and ini["stages"] == [] and ini["transfers"] == []


@pytest.mark.parametrize(
    "path",
    [p for p in EXAMPLES if "decoder_walkthrough" in p],
    ids=lambda p: os.path.relpath(p, _REPO_ROOT),
)
def test_flow_payload_property_over_deepseek_walkthrough(path):
    d = storage.load(path)
    p = flow_payload(d)
    core_ids = {c["id"] for c in p["cores"]}
    assert core_ids, "no cores in payload"
    for op in p["ops"]:
        if op["is_movement"]:
            assert op["steps"] == []
            for t in op["transfers"]:
                assert t["from"] in core_ids and t["to"] in core_ids
        elif op["is_init"]:
            assert op["steps"] == []
        else:  # compute
            assert op["transfers"] == []
            used = set()
            for step in op["steps"]:
                used.update(step["reads"])
                used.update(step["writes"])
                assert set(step["reads"]) <= set(op["stages"])
                assert set(step["writes"]) <= set(op["stages"])
            assert op["stages"] == [s for s in STAGE_ORDER if s in used]


# ---- chip_payload contract ----

@pytest.mark.parametrize(
    "path", MULTICORE_EXAMPLES, ids=lambda p: os.path.relpath(p, _REPO_ROOT)
)
def test_chip_payload_interactions_are_cross_core_and_sorted(path):
    d = storage.load(path)
    p = chip_payload(d)
    core_ids = {c["id"] for c in p["cores"]}
    last = None
    for it in p["interactions"]:
        # cross-core only
        assert it["from_core"] != it["to_core"]
        # endpoints reference real cores
        assert it["from_core"] in core_ids and it["to_core"] in core_ids
        # at_clock / to_clock present as int or None
        assert it["at_clock"] is None or isinstance(it["at_clock"], int)
        assert it["to_clock"] is None or isinstance(it["to_clock"], int)
        # sorted by at_clock (None sorts last)
        key = (it["at_clock"] is None, it["at_clock"] or 0)
        if last is not None:
            assert key >= last, "interactions not sorted by at_clock"
        last = key


# ---- scheduler soundness ----

def test_schedule_is_deterministic():
    d = _mixed_diagram()
    s1 = solve(d)
    s2 = solve(d)
    starts1 = {b.block_id: b.start_clock for b in s1.blocks}
    starts2 = {b.block_id: b.start_clock for b in s2.blocks}
    assert starts1 == starts2
    # also identical across the deepseek device8 example (larger, multi-core)
    big = storage.load(
        os.path.join(_REPO_ROOT, "examples", "deepseek", "decoder_mlp_device8.yaml")
    )
    a = {b.block_id: b.start_clock for b in solve(big).blocks}
    b = {b.block_id: b.start_clock for b in solve(big).blocks}
    assert a == b


def test_cycle_detection_raises_schedule_error():
    # a -> b and b -> a (cross-op deps that contradict) => unsatisfiable cycle
    d = Diagram(
        title="cyc",
        lanes=[Lane(id="t0", name="T0", order=0)],
        resources=[Resource(id="u", name="U")],
        ops=[
            Op(id="a", name="a", blocks=[Block(id="a_b", lane_id="t0", resource_id="u", duration_clocks=5)]),
            Op(id="b", name="b", blocks=[Block(id="b_b", lane_id="t0", resource_id="u", duration_clocks=5)]),
        ],
        dependencies=[
            Dependency.model_validate({"from": "a", "to": "b"}),
            Dependency.model_validate({"from": "b", "to": "a"}),
        ],
    )
    with pytest.raises(ScheduleError):
        solve(d)


# ---- static guard on flow.js (NOT behavioral) ----

def _read_flow_js() -> str:
    with open(FLOW_JS, encoding="utf-8") as fh:
        return fh.read()


def test_flow_js_exists():
    assert os.path.isfile(FLOW_JS)


def test_flow_js_brackets_balanced():
    """STATIC check: bracket / paren / brace counts match. This is a cheap
    structural guard, not a parse — it won't catch all syntax errors, but it does
    catch the common 'dropped a closing brace while editing' mistake."""
    src = _read_flow_js()
    for open_ch, close_ch in (("(", ")"), ("[", "]"), ("{", "}")):
        assert src.count(open_ch) == src.count(close_ch), (
            f"unbalanced {open_ch}{close_ch} in flow.js: "
            f"{src.count(open_ch)} vs {src.count(close_ch)}"
        )


def test_flow_js_has_no_removed_mode_references():
    """STATIC check: the unified view removed the old mode toggle. Guard against
    a stray leftover reference to the removed functions/identifiers."""
    src = _read_flow_js()
    for dead in ("setMode", "mode-op"):
        assert dead not in src, f"flow.js still references removed {dead!r}"
