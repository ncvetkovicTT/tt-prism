"""Tests for true data (RAW) dependencies in the op-scheduler.

A "data dependency" here is a read-after-write edge: a consumer op cannot begin
operating on an operand until the producer op has finished producing it. In the
model this is the ``l1_data`` dependency kind (RAW through L1), plus block-level
explicit deps.

Endpoint resolution (see schedule._resolve_endpoint): when a dependency names an
OP id, the *source* resolves to the op's LAST block (its pack) and the *target*
to the op's FIRST block (its unpack). So ``{from: add, to: mul, kind: l1_data}``
means ``mul.first_block.start >= add.last_block.end + min_gap_clocks``.

These tests use deterministic durations and assert exact integer clocks. To
isolate the effect of a data dependency from incidental resource/lane
serialization, several tests place producers/consumers on *distinct* lanes and
resources so the only cross-op edge is the data dep under test.
"""

from __future__ import annotations

import pytest

from tt_prism.models import Block, Dependency, Diagram, Lane, Op, Resource
from tt_prism.schedule import ScheduleError, solve


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _lanes() -> list[Lane]:
    """One lane per op-slot plus the canonical three, so tests can put ops on
    fully disjoint lanes when they want to avoid lane serialization."""
    return [
        Lane(id="trisc0", name="TRISC 0", order=0),
        Lane(id="trisc1", name="TRISC 1", order=1),
        Lane(id="trisc2", name="TRISC 2", order=2),
        # Extra disjoint lanes for isolation tests.
        Lane(id="laneA", name="Lane A", order=3),
        Lane(id="laneB", name="Lane B", order=4),
        Lane(id="laneC", name="Lane C", order=5),
        Lane(id="laneD", name="Lane D", order=6),
    ]


def _resources() -> list[Resource]:
    """Canonical four resources plus disjoint per-op resources for isolation."""
    return [
        Resource(id="unpack", name="UNPACK"),
        Resource(id="fpu", name="FPU"),
        Resource(id="sfpu", name="SFPU"),
        Resource(id="pack", name="PACK"),
        # Extra disjoint resources so isolation tests aren't confounded by the
        # singleton resource-serialization edges.
        Resource(id="unpackA", name="UNPACK A"),
        Resource(id="fpuA", name="FPU A"),
        Resource(id="packA", name="PACK A"),
        Resource(id="unpackB", name="UNPACK B"),
        Resource(id="fpuB", name="FPU B"),
        Resource(id="packB", name="PACK B"),
        Resource(id="unpackC", name="UNPACK C"),
        Resource(id="fpuC", name="FPU C"),
        Resource(id="packC", name="PACK C"),
        Resource(id="unpackD", name="UNPACK D"),
        Resource(id="fpuD", name="FPU D"),
        Resource(id="packD", name="PACK D"),
    ]


def _op(op_id: str, unp: int, math: int, pak: int) -> Op:
    """Three-block op (unpack -> fpu -> pack) on the canonical shared lanes and
    resources. Two such ops will serialize on every shared resource/lane."""
    return Op(
        id=op_id,
        name=op_id,
        blocks=[
            Block(id=f"{op_id}_u", lane_id="trisc0", resource_id="unpack", duration_clocks=unp),
            Block(id=f"{op_id}_f", lane_id="trisc1", resource_id="fpu", duration_clocks=math),
            Block(id=f"{op_id}_p", lane_id="trisc2", resource_id="pack", duration_clocks=pak),
        ],
    )


def _iso_op(op_id: str, suffix: str, unp: int, math: int, pak: int) -> Op:
    """Like _op but on fully disjoint lanes and resources (identified by
    ``suffix`` -> laneX / *X resources), so this op shares nothing with another
    iso-op carrying a different suffix. The only cross-op edges are then the
    explicit dependencies we add by hand."""
    lane = f"lane{suffix}"
    return Op(
        id=op_id,
        name=op_id,
        blocks=[
            Block(id=f"{op_id}_u", lane_id=lane, resource_id=f"unpack{suffix}", duration_clocks=unp),
            Block(id=f"{op_id}_f", lane_id=lane, resource_id=f"fpu{suffix}", duration_clocks=math),
            Block(id=f"{op_id}_p", lane_id=lane, resource_id=f"pack{suffix}", duration_clocks=pak),
        ],
    )


def _diagram(ops, deps=None) -> Diagram:
    return Diagram(
        title="t", grid_clocks=32, lanes=_lanes(), resources=_resources(),
        ops=ops, dependencies=deps or [],
    )


# --------------------------------------------------------------------------- #
# 1. producer -> consumer forces consumer.first.start >= producer.last.end
# --------------------------------------------------------------------------- #
def test_data_dep_forces_consumer_after_producer_exact():
    """With min_gap=0, the consumer's first block (unpack) must start exactly
    when the producer's last block (pack) ends."""
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate({"from": "add", "to": "mul", "kind": "l1_data"})],
    )
    s = solve(d)
    add_pack_end = s.start_of("add_p") + 60  # producer last block end
    assert add_pack_end == 190
    assert s.start_of("mul_u") >= add_pack_end
    assert s.start_of("mul_u") == add_pack_end          # exact, gap=0


# --------------------------------------------------------------------------- #
# 2. min_gap_clocks adds latency exactly
# --------------------------------------------------------------------------- #
def test_min_gap_adds_exact_latency():
    """min_gap_clocks pushes the consumer start out by exactly that many clocks
    beyond the producer end (semaphore / sync latency)."""
    gap = 8
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate(
            {"from": "add", "to": "mul", "kind": "l1_data", "min_gap_clocks": gap}
        )],
    )
    s = solve(d)
    add_pack_end = s.start_of("add_p") + 60
    assert s.start_of("mul_u") == add_pack_end + gap
    assert s.start_of("mul_u") == 198


# --------------------------------------------------------------------------- #
# 3. three-op chain a -> b -> c serializes along the critical path
# --------------------------------------------------------------------------- #
def test_three_op_chain_cascades_serially():
    """a feeds b feeds c. On disjoint resources the only cross-op edges are the
    two data deps, so the schedule is a clean serial cascade and total time is
    the sum of all three ops' end-to-end latencies."""
    a = _iso_op("a", "A", 100, 30, 60)   # latency 190
    b = _iso_op("b", "B", 50, 20, 40)    # latency 110
    c = _iso_op("c", "C", 70, 10, 20)    # latency 100
    d = _diagram(
        [a, b, c],
        deps=[
            Dependency.model_validate({"from": "a", "to": "b", "kind": "l1_data"}),
            Dependency.model_validate({"from": "b", "to": "c", "kind": "l1_data"}),
        ],
    )
    s = solve(d)

    # a runs first, from 0.
    assert s.start_of("a_u") == 0
    assert s.start_of("a_p") == 130
    a_end = s.start_of("a_p") + 60
    assert a_end == 190

    # b starts (unpack) exactly when a's pack ends.
    assert s.start_of("b_u") == a_end          # 190
    assert s.start_of("b_f") == 240            # +50 unpack
    assert s.start_of("b_p") == 260            # +20 fpu
    b_end = s.start_of("b_p") + 40
    assert b_end == 300

    # c starts exactly when b's pack ends.
    assert s.start_of("c_u") == b_end          # 300
    assert s.start_of("c_p") == 380            # +70 unpack +10 fpu
    c_end = s.start_of("c_p") + 20
    assert c_end == 400

    # Total time == serialized critical path == 190 + 110 + 100.
    assert s.total_clocks() == 400
    assert s.total_clocks() == 190 + 110 + 100


# --------------------------------------------------------------------------- #
# 4. diamond: consumer waits for the LATER producer (max, not sum)
# --------------------------------------------------------------------------- #
def test_diamond_consumer_waits_for_later_producer():
    """Two producers p1 (latency 190) and p2 (latency 300) both feed consumer
    cc. cc must wait for the LATER of the two (max == 300), not their sum."""
    p1 = _iso_op("p1", "A", 100, 30, 60)   # latency 190
    p2 = _iso_op("p2", "B", 200, 40, 60)   # latency 300
    cc = _iso_op("cc", "C", 50, 20, 40)
    d = _diagram(
        [p1, p2, cc],
        deps=[
            Dependency.model_validate({"from": "p1", "to": "cc", "kind": "l1_data"}),
            Dependency.model_validate({"from": "p2", "to": "cc", "kind": "l1_data"}),
        ],
    )
    s = solve(d)

    p1_end = s.start_of("p1_p") + 60
    p2_end = s.start_of("p2_p") + 60
    assert p1_end == 190
    assert p2_end == 300

    # Consumer waits for the later producer, i.e. the max, NOT the sum.
    assert s.start_of("cc_u") == max(p1_end, p2_end)   # 300
    assert s.start_of("cc_u") == 300
    assert s.start_of("cc_u") != p1_end + p2_end        # explicitly not the sum


# --------------------------------------------------------------------------- #
# 5. block-level explicit dependency honored (block ids, not op ids)
# --------------------------------------------------------------------------- #
def test_block_level_explicit_dependency():
    """A dependency may name BLOCK ids directly. Here we wire add's fpu block ->
    mul's fpu block, which is a different edge than the op-level pack->unpack
    resolution. Block ids are used verbatim (no first/last resolution)."""
    add = _iso_op("add", "A", 100, 30, 60)
    mul = _iso_op("mul", "B", 100, 30, 60)
    d = _diagram(
        [add, mul],
        deps=[Dependency.model_validate(
            {"from": "add_f", "to": "mul_f", "kind": "l1_data"}
        )],
    )
    s = solve(d)

    add_fpu_end = s.start_of("add_f") + 30      # 100 + 30 = 130
    assert add_fpu_end == 130
    # mul's fpu must start no earlier than add's fpu end.
    assert s.start_of("mul_f") >= add_fpu_end
    assert s.start_of("mul_f") == add_fpu_end   # 130, the binding constraint

    # Scheduling is ASAP (longest path from t=0), not "as late as possible", so
    # mul's unpack is NOT pulled forward to hug the constraint -- it stays at 0,
    # and the data dep is what holds mul_f back to 130.
    assert s.start_of("mul_u") == 0
    assert s.start_of("mul_f") - s.start_of("mul_u") == 130   # slack absorbed here


# --------------------------------------------------------------------------- #
# 6. removing the dep allows overlap (the dep actually changed the schedule)
# --------------------------------------------------------------------------- #
def test_dep_actually_constrains_vs_no_dep():
    """On disjoint resources, two independent ops fully overlap (both start at
    0). Adding the l1_data dep forces the consumer strictly later, proving the
    dependency edge changed the schedule."""
    def make(deps):
        return _diagram(
            [_iso_op("add", "A", 100, 30, 60), _iso_op("mul", "B", 100, 30, 60)],
            deps=deps,
        )

    # No dep: nothing shared, so mul can run fully in parallel with add.
    free = solve(make([]))
    assert free.start_of("add_u") == 0
    assert free.start_of("mul_u") == 0          # full overlap, no constraint

    # With dep: mul's unpack cannot start until add's pack ends.
    dep = solve(make([
        Dependency.model_validate({"from": "add", "to": "mul", "kind": "l1_data"})
    ]))
    add_pack_end = dep.start_of("add_p") + 60
    assert dep.start_of("mul_u") == add_pack_end          # 190

    # The dep strictly delayed the consumer relative to the unconstrained case.
    assert dep.start_of("mul_u") > free.start_of("mul_u")
    assert dep.start_of("mul_u") - free.start_of("mul_u") == 190


# --------------------------------------------------------------------------- #
# 7. backwards data dep contradicting program order raises ScheduleError
# --------------------------------------------------------------------------- #
def test_backwards_dep_creates_cycle():
    """add is declared before mul (program order). A data dep mul -> add runs
    backwards against the resource/lane serialization edges add->mul, forming a
    cycle the scheduler cannot satisfy."""
    d = _diagram(
        [_op("add", 100, 30, 60), _op("mul", 100, 30, 60)],
        deps=[Dependency.model_validate({"from": "mul", "to": "add", "kind": "l1_data"})],
    )
    with pytest.raises(ScheduleError):
        solve(d)


def test_self_chain_cycle_two_ops():
    """A pair of contradicting deps (add->mul and mul->add) is unsatisfiable
    regardless of serialization, and must raise ScheduleError."""
    d = _diagram(
        [_iso_op("add", "A", 100, 30, 60), _iso_op("mul", "B", 100, 30, 60)],
        deps=[
            Dependency.model_validate({"from": "add", "to": "mul", "kind": "l1_data"}),
            Dependency.model_validate({"from": "mul", "to": "add", "kind": "l1_data"}),
        ],
    )
    with pytest.raises(ScheduleError):
        solve(d)
