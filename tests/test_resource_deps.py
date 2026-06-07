"""Tests for *resource dependencies* (resource serialization) in the scheduler.

The four execution resources (UNPACK / FPU / SFPU / PACK) are modeled as
singletons: blocks that share a ``resource_id`` cannot overlap in time and are
ordered by program order (op declaration order, then block order within an op).
This holds even when there is *no* explicit data dependency between two ops.

Work on *different* resources, however, may overlap (pipelining): op2's UNPACK
can run while op1's FPU runs, as long as op2's UNPACK does not overlap op1's
UNPACK.

Separately, blocks on the same *lane* (TRISC) also serialize — a TRISC
dispatches in order — even when they name different resources.
"""

from __future__ import annotations

from tt_prism.models import Block, Diagram, Lane, Op, Resource
from tt_prism.schedule import solve


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
    """A standard op: UNPACK on trisc0, FPU (math) on trisc1, PACK on trisc2.

    Each resource lives on its own lane, so for this op resource serialization
    and lane serialization coincide on a per-resource basis."""
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


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True iff intervals [a_start, a_end) and [b_start, b_end) overlap."""
    return a_start < b_end and b_start < a_end


# 1. ------------------------------------------------------------------------

def test_unpack_serializes_without_explicit_dependency():
    """With NO explicit dependency, op2's UNPACK starts exactly when op1's
    UNPACK ends — purely from the resource-serialization edge, not at clock 0."""
    d = _diagram([_op("add", 100, 30, 60), _op("mul", 100, 30, 60)])
    s = solve(d)
    assert s.start_of("add_u") == 0
    # mul has no data dependency on add, yet shares the UNPACK singleton:
    assert s.start_of("mul_u") != 0
    assert s.start_of("mul_u") == s.start_of("add_u") + 100   # add's unpack end


# 2. ------------------------------------------------------------------------

def test_pipelining_across_resources_overlaps():
    """Different resources run concurrently: op2's UNPACK overlaps op1's FPU in
    time. add_u=[0,100), add_f=[100,130), mul_u=[100,200) -> mul_u overlaps
    add_f."""
    d = _diagram([_op("add", 100, 30, 60), _op("mul", 100, 30, 60)])
    s = solve(d)

    add_f_start = s.start_of("add_f")
    add_f_end = add_f_start + 30
    mul_u_start = s.start_of("mul_u")
    mul_u_end = mul_u_start + 100

    # sanity: they are on different resources (fpu vs unpack)
    assert add_f_start == 100 and mul_u_start == 100
    assert _overlap(mul_u_start, mul_u_end, add_f_start, add_f_end)


# 3. ------------------------------------------------------------------------

def test_three_ops_unpack_chain_back_to_back():
    """3+ ops sharing UNPACK serialize into a back-to-back chain in program
    order; each unpack starts when the previous ended, and total UNPACK span is
    the sum of the durations."""
    durs = [40, 70, 25]
    ops = [_op(name, u, 10, 10) for name, u in zip(("a", "b", "c"), durs)]
    s = solve(_diagram(ops))

    assert s.start_of("a_u") == 0
    assert s.start_of("b_u") == s.start_of("a_u") + durs[0]          # 40
    assert s.start_of("c_u") == s.start_of("b_u") + durs[1]          # 110
    # back-to-back: last unpack ends at the sum of all unpack durations
    assert s.start_of("c_u") + durs[2] == sum(durs)                 # 135


# 4. ------------------------------------------------------------------------

def test_lane_serialization_different_resources_same_lane():
    """Two blocks on the SAME lane but DIFFERENT resources cannot overlap.

    Here a single op dispatches both an FPU block and an SFPU block from
    trisc1. They share no resource, but the TRISC dispatches serially, so the
    second must wait for the first (lane-serialization edge)."""
    op = Op(
        id="combo",
        name="combo",
        blocks=[
            Block(id="combo_u", lane_id="trisc0", resource_id="unpack", duration_clocks=20),
            Block(id="combo_f", lane_id="trisc1", resource_id="fpu", duration_clocks=50),
            Block(id="combo_s", lane_id="trisc1", resource_id="sfpu", duration_clocks=30),
        ],
    )
    s = solve(_diagram([op]))

    f_start = s.start_of("combo_f")
    f_end = f_start + 50
    s_start = s.start_of("combo_s")
    s_end = s_start + 30

    # different resources, same lane -> must NOT overlap
    assert not _overlap(f_start, f_end, s_start, s_end)
    # sfpu follows fpu (program order on the shared lane)
    assert s_start >= f_end


# 5. ------------------------------------------------------------------------

def test_growing_op1_unpack_shifts_op2_unpack_right():
    """Growing op1's UNPACK duration pushes op2's UNPACK start right by the same
    amount, via the shared-resource edge."""
    base = solve(_diagram([_op("add", 100, 30, 60), _op("mul", 100, 30, 60)]))
    grown = solve(_diagram([_op("add", 145, 30, 60), _op("mul", 100, 30, 60)]))  # +45

    assert base.start_of("mul_u") == 100
    assert grown.start_of("mul_u") == base.start_of("mul_u") + 45               # 145


# 6. ------------------------------------------------------------------------

def test_resources_independent_growing_fpu_does_not_delay_unpack():
    """Resources are independent: making op1's FPU block longer does NOT delay
    op2's UNPACK start. Only the shared-resource (UNPACK) predecessor matters."""
    base = solve(_diagram([_op("add", 100, 30, 60), _op("mul", 100, 30, 60)]))
    bigger_fpu = solve(_diagram([_op("add", 100, 500, 60), _op("mul", 100, 30, 60)]))

    # op2's unpack only depends on op1's unpack (duration unchanged at 100):
    assert base.start_of("mul_u") == 100
    assert bigger_fpu.start_of("mul_u") == 100


# 7. ------------------------------------------------------------------------

def test_unequal_unpack_durations_serialize_exactly():
    """Unequal durations: a longer op1 UNPACK then a shorter op2 UNPACK — verify
    the exact serialized start clocks."""
    d = _diagram([_op("big", 200, 10, 10), _op("small", 35, 10, 10)])
    s = solve(d)

    assert s.start_of("big_u") == 0
    assert s.start_of("small_u") == 200                 # after big's 200-clock unpack
    assert s.start_of("small_u") + 35 == 235            # small's unpack end
