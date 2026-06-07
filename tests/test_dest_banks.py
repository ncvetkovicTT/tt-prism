"""Tests for virtual DEST bank modeling.

DEST is split in software into N banks so PACK can drain one bank while MATH
fills another. A block declares which bank it writes (MATH) or reads (PACK);
blocks sharing a bank serialize, so MATH cannot reuse a bank until the PACK that
last read it has finished. These tests isolate the dest_bank edge from the
resource/lane serialization edges by giving each op its own resource+lane where
needed."""

from __future__ import annotations

import pytest

from tt_prism.models import Block, Diagram, Lane, Op, Resource
from tt_prism.schedule import solve


def _diagram(ops, *, lanes, resources, dest_banks=2) -> Diagram:
    return Diagram(
        title="dest", grid_clocks=32, dest_banks=dest_banks,
        lanes=[Lane(id=l, name=l, order=i) for i, l in enumerate(lanes)],
        resources=[Resource(id=r, name=r) for r in resources],
        ops=ops,
    )


def _math_pack_op(op_id, *, math_lane, math_res, pack_lane, pack_res, bank, math=30, pack=80):
    """An op with just a MATH block (writes `bank`) and a PACK block (reads `bank`)."""
    return Op(
        id=op_id, name=op_id,
        blocks=[
            Block(id=f"{op_id}_m", lane_id=math_lane, resource_id=math_res,
                  duration_clocks=math, dest_bank=bank),
            Block(id=f"{op_id}_p", lane_id=pack_lane, resource_id=pack_res,
                  duration_clocks=pack, dest_bank=bank),
        ],
    )


def test_same_bank_math_waits_for_prior_pack():
    # Two ops on DISJOINT resources/lanes so the ONLY coupling is the shared bank.
    # op1's MATH reuses bank0 -> must wait until op0's PACK frees it.
    ops = [
        _math_pack_op("o0", math_lane="la", math_res="ma", pack_lane="lb", pack_res="pa", bank=0),
        _math_pack_op("o1", math_lane="lc", math_res="mb", pack_lane="ld", pack_res="pb", bank=0),
    ]
    s = _diagram(ops, lanes=["la", "lb", "lc", "ld"], resources=["ma", "pa", "mb", "pb"])
    sch = solve(s)
    assert sch.start_of("o0_m") == 0
    assert sch.start_of("o0_p") == 30          # pack after its own math (intra-op)
    o0_pack_end = 30 + 80                       # = 110, frees bank0
    assert sch.start_of("o1_m") == o0_pack_end  # 110: math can't reuse bank0 sooner
    assert sch.start_of("o1_p") == o0_pack_end + 30


def test_different_banks_run_in_parallel():
    # Same disjoint resources, but op1 uses bank1 -> no bank coupling -> parallel.
    ops = [
        _math_pack_op("o0", math_lane="la", math_res="ma", pack_lane="lb", pack_res="pa", bank=0),
        _math_pack_op("o1", math_lane="lc", math_res="mb", pack_lane="ld", pack_res="pb", bank=1),
    ]
    s = _diagram(ops, lanes=["la", "lb", "lc", "ld"], resources=["ma", "pa", "mb", "pb"])
    sch = solve(s)
    assert sch.start_of("o0_m") == 0
    assert sch.start_of("o1_m") == 0           # different bank: starts immediately


def test_double_buffering_beats_single_bank():
    # Realistic: 4 ops sharing FPU (trisc1) + PACK (trisc2), PACK is the long pole.
    # Compare a 2-bank assignment (0,1,0,1) against forcing everything onto bank0.
    def build(banks):
        ops = [
            _math_pack_op(f"o{i}", math_lane="trisc1", math_res="fpu",
                          pack_lane="trisc2", pack_res="pack", bank=banks[i])
            for i in range(4)
        ]
        return _diagram(ops, lanes=["trisc1", "trisc2"], resources=["fpu", "pack"])

    two_bank = solve(build([0, 1, 0, 1]))
    one_bank = solve(build([0, 0, 0, 0]))
    # With one bank, each op's MATH waits for the previous op's PACK -> fully serial.
    # With two banks, MATH of op_{i+1} overlaps PACK of op_i -> shorter makespan.
    assert two_bank.total_clocks() < one_bank.total_clocks()


def test_bank_index_out_of_range_rejected():
    with pytest.raises(Exception):
        _diagram(
            [_math_pack_op("o0", math_lane="la", math_res="ma",
                           pack_lane="lb", pack_res="pa", bank=2)],  # only banks 0,1 valid
            lanes=["la", "lb"], resources=["ma", "pa"], dest_banks=2,
        )


def test_none_bank_blocks_do_not_serialize():
    # UNPACK-like blocks with dest_bank=None must not be coupled by the bank edge.
    ops = [
        Op(id="u0", name="u0", blocks=[
            Block(id="u0_b", lane_id="la", resource_id="ua", duration_clocks=50)]),
        Op(id="u1", name="u1", blocks=[
            Block(id="u1_b", lane_id="lb", resource_id="ub", duration_clocks=50)]),
    ]
    s = _diagram(ops, lanes=["la", "lb"], resources=["ua", "ub"])
    sch = solve(s)
    assert sch.start_of("u0_b") == 0
    assert sch.start_of("u1_b") == 0           # no bank, no resource/lane sharing -> parallel
