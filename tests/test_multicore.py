"""Tests for multi-core diagrams: ops run on a named Core (x,y); engines, lanes,
and DEST banks serialize *per core*, so different cores run in parallel except
where an explicit cross-core dependency (a NoC transfer) couples them."""

from __future__ import annotations

import pytest

from tt_prism.models import Block, Core, Dependency, Diagram, Lane, Op, Resource
from tt_prism.schedule import solve, to_render_diagram


def _base(cores, ops, deps=None) -> Diagram:
    return Diagram(
        title="mc", grid_clocks=32, dest_banks=2,
        cores=cores,
        lanes=[Lane(id="trisc0", name="TRISC 0", order=0),
               Lane(id="trisc1", name="TRISC 1", order=1),
               Lane(id="trisc2", name="TRISC 2", order=2)],
        resources=[Resource(id="unpack", name="U"), Resource(id="fpu", name="F"),
                   Resource(id="sfpu", name="S"), Resource(id="pack", name="P")],
        ops=ops, dependencies=deps or [],
    )


def _mm(op_id, core, u=52, m=32, p=64, bank=0):
    return Op(id=op_id, name=op_id, core_id=core, blocks=[
        Block(id=f"{op_id}_u", lane_id="trisc0", resource_id="unpack", duration_clocks=u),
        Block(id=f"{op_id}_m", lane_id="trisc1", resource_id="fpu", duration_clocks=m, dest_bank=bank),
        Block(id=f"{op_id}_p", lane_id="trisc2", resource_id="pack", duration_clocks=p, dest_bank=bank),
    ])


C2 = [Core(id="c00", x=0, y=0), Core(id="c10", x=1, y=0)]


def test_different_cores_run_in_parallel():
    # No dependency between the two ops; they're on different cores → both at t=0.
    s = solve(_base(C2, [_mm("a", "c00"), _mm("b", "c10")]))
    assert s.start_of("a_u") == 0
    assert s.start_of("b_u") == 0          # NOT serialized behind a_u


def test_same_core_serializes():
    # Two ops on the SAME core share that core's UNPACK → serialize.
    s = solve(_base(C2, [_mm("a0", "c00"), _mm("a1", "c00")]))
    assert s.start_of("a0_u") == 0
    assert s.start_of("a1_u") == 52        # behind a0's unpack on the same core


def test_same_resource_different_cores_independent():
    # Growing core c00's UNPACK must not delay core c10's UNPACK.
    s = solve(_base(C2, [_mm("a", "c00", u=500), _mm("b", "c10")]))
    assert s.start_of("b_u") == 0


def test_same_dest_bank_different_cores_independent():
    # DEST banks are per core: both ops use bank 0 but on different cores → the
    # bank reuse edge must not couple them.
    s = solve(_base(C2, [_mm("a", "c00", bank=0), _mm("b", "c10", bank=0)]))
    assert s.start_of("a_m") == s.start_of("b_m")   # neither waits for the other's pack


def test_cross_core_noc_dependency():
    # b waits for a's pack + NoC latency.
    s = solve(_base(C2, [_mm("a", "c00"), _mm("b", "c10")],
                    deps=[Dependency.model_validate(
                        {"from": "a", "to": "b", "kind": "l1_data", "min_gap_clocks": 40})]))
    assert s.start_of("b_u") == s.start_of("a_p") + 64 + 40


def test_flatten_builds_per_core_lanes_with_gap_and_groups():
    flat = to_render_diagram(_base(C2, [_mm("a", "c00"), _mm("b", "c10")]))
    by_id = {l.id: l for l in flat.lanes}
    # synthetic per-core lane ids, grouped, with a blank gap row between cores
    assert "c00::trisc0" in by_id and "c10::trisc0" in by_id
    assert by_id["c00::trisc0"].group == "c00"
    orders = sorted(l.order for l in flat.lanes)
    assert orders == [1, 2, 3, 5, 6, 7]            # gap rows at 0 and 4
    # every work-item lands on a lane that exists
    lane_ids = set(by_id)
    assert all(w.lane_id in lane_ids for w in flat.work_items)


def test_missing_core_ref_rejected():
    with pytest.raises(Exception):
        _base(C2, [_mm("a", "nope")])


def test_core_id_required_when_multiple_cores():
    with pytest.raises(Exception):
        _base(C2, [Op(id="a", blocks=[                      # no core_id, but 2 cores declared
            Block(id="a_u", lane_id="trisc0", resource_id="unpack", duration_clocks=10)])])


def test_single_core_backward_compatible():
    # No cores declared → lanes pass through unchanged (no group chrome, original ids).
    d = _base([], [_mm("a", None)])
    flat = to_render_diagram(d)
    assert {l.id for l in flat.lanes} == {"trisc0", "trisc1", "trisc2"}
    assert all(l.group is None for l in flat.lanes)
    assert all("::" not in w.lane_id for w in flat.work_items)
