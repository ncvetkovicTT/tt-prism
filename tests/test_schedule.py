from __future__ import annotations

import pytest

from tt_prism.models import Block, Dependency, Diagram, Lane, Op, Resource
from tt_prism.schedule import ScheduleError, solve, to_render_diagram


def _lanes() -> list[Lane]:
    return [
        Lane(id="trisc0", name="TRISC 0", order=0),
        Lane(id="trisc1", name="TRISC 1", order=1),
        Lane(id="trisc2", name="TRISC 2", order=2),
    ]


def _resources() -> list[Resource]:
    return [
        Resource(id="unpack", name="UNPACK"),
        Resource(id="fpu", name="FPU"),
        Resource(id="sfpu", name="SFPU"),
        Resource(id="pack", name="PACK"),
    ]


def _op(op_id: str, unp: int, math: int, pak: int) -> Op:
    return Op(
        id=op_id,
        name=op_id,
        blocks=[
            Block(id=f"{op_id}_u", lane_id="trisc0", resource_id="unpack", duration_clocks=unp),
            Block(id=f"{op_id}_f", lane_id="trisc1", resource_id="fpu", duration_clocks=math),
            Block(id=f"{op_id}_p", lane_id="trisc2", resource_id="pack", duration_clocks=pak),
        ],
    )


def _diagram(ops, deps=None) -> Diagram:
    return Diagram(
        title="t", grid_clocks=32, lanes=_lanes(), resources=_resources(),
        ops=ops, dependencies=deps or [],
    )


def test_intra_op_precedence():
    # unpack -> fpu -> pack must run back to back within an op
    d = _diagram([_op("add", 100, 30, 60)])
    s = solve(d)
    assert s.start_of("add_u") == 0
    assert s.start_of("add_f") == 100          # after unpack
    assert s.start_of("add_p") == 130          # after fpu
    assert s.total_clocks() == 190


def test_resize_unpack_shifts_everything_right():
    base = solve(_diagram([_op("add", 100, 30, 60)]))
    grown = solve(_diagram([_op("add", 160, 30, 60)]))   # unpack grew by 60
    assert grown.start_of("add_f") == base.start_of("add_f") + 60
    assert grown.start_of("add_p") == base.start_of("add_p") + 60


def test_cross_op_l1_dependency():
    # mul cannot unpack until add has packed c
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate({"from": "add", "to": "mul", "kind": "l1_data"})],
    )
    s = solve(d)
    add_pack_end = s.start_of("add_p") + 60
    assert s.start_of("mul_u") >= add_pack_end
    assert s.start_of("mul_u") == add_pack_end


def test_min_gap_on_dependency():
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate(
            {"from": "add", "to": "mul", "kind": "l1_data", "min_gap_clocks": 8}
        )],
    )
    s = solve(d)
    assert s.start_of("mul_u") == (s.start_of("add_p") + 60) + 8


def test_resource_serialization_without_deps():
    # Two independent ops still serialize on the shared UNPACK/FPU/PACK resources,
    # but pipeline across resources: mul's unpack starts when add's unpack ends.
    d = _diagram([_op("add", 100, 30, 60), _op("mul", 100, 30, 60)])
    s = solve(d)
    assert s.start_of("mul_u") == 100          # UNPACK free after add's unpack
    assert s.start_of("add_u") == 0


def test_cycle_detected():
    # A backwards cross-op dep (mul before add) plus program order creates a cycle.
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate({"from": "mul", "to": "add", "kind": "l1_data"})],
    )
    with pytest.raises(ScheduleError):
        solve(d)


def test_to_render_diagram_roundtrips():
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate({"from": "add", "to": "mul", "kind": "l1_data"})],
    )
    flat = to_render_diagram(d)
    assert len(flat.work_items) == 6
    assert flat.ops == []                       # fully flattened
    # resolved starts present and non-overlapping on the FPU
    fpu_items = sorted(
        (w for w in flat.work_items if w.resource_id == "fpu"),
        key=lambda w: w.start_clock,
    )
    assert fpu_items[0].end_clock <= fpu_items[1].start_clock


def test_block_id_collision_rejected():
    with pytest.raises(Exception):
        _diagram([_op("add", 100, 30, 60), _op("add", 100, 30, 60)])  # dup ids
