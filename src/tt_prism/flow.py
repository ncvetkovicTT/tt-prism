"""Dataflow ("flow") view model.

The flow view shows, for a single *execute* op, how a tile moves through the
Tensix storage stages — L1 → Source Registers → DEST (as a growing expression)
→ L1 — one step at a time. Init ops have no meaningful dataflow and are skipped.

An op's steps are taken from its explicit ``flow`` if authored, otherwise a
generic unpack→math→pack flow is derived from its blocks (the hybrid model).
"""

from __future__ import annotations

from tt_prism.models import Diagram, FlowStep, Op

# Canonical left→right column order and display labels for the stages.
STAGE_ORDER: list[str] = ["l1_in", "srca", "srcb", "dest", "sfpu", "l1_out"]
STAGE_LABELS: dict[str, str] = {
    "l1_in": "Input L1",
    "srca": "SrcA",
    "srcb": "SrcB",
    "dest": "DEST",
    "sfpu": "SFPU",
    "l1_out": "Output L1",
}

# Generic per-resource step used when an op has no explicit flow.
_GENERIC: dict[str, tuple[list[str], list[str], str, str]] = {
    # resource_id : (reads, writes, default label, datum name)
    "unpack": (["l1_in"], ["srca", "srcb"], "unpack operands", "operands"),
    "fpu": (["srca", "srcb"], ["dest"], "math → DEST", "result"),
    "sfpu": (["dest"], ["dest"], "vector (SFPU) on DEST", "result"),
    "pack": (["dest"], ["l1_out"], "pack → L1", "output"),
}


def resolved_flow(op: Op) -> list[FlowStep]:
    """The op's explicit flow, or a generic one derived from its blocks."""
    if op.flow:
        return list(op.flow)
    steps: list[FlowStep] = []
    for b in op.blocks:
        reads, writes, default_label, datum = _GENERIC.get(
            b.resource_id, ([], [], b.resource_id, b.resource_id))
        steps.append(FlowStep(
            id=b.id,
            label=b.label or default_label,
            reads=list(reads),
            writes=list(writes),
            data=datum,
        ))
    return steps


def execute_ops(diagram: Diagram) -> list[Op]:
    """Ops the flow view applies to (everything that isn't an init op)."""
    return [op for op in diagram.ops if not op.is_init]


def _stages_used(steps: list[FlowStep]) -> list[str]:
    touched = {s for st in steps for s in (*st.reads, *st.writes)}
    return [s for s in STAGE_ORDER if s in touched]


def flow_payload(diagram: Diagram) -> dict:
    """JSON-able payload for the flow view: every op (init ones flagged so the
    client can grey them out) with its resolved steps and the stages they use."""
    ops = []
    for op in diagram.ops:
        # Init ops have no dataflow; emit them flagged but empty (the client
        # greys them out and never opens them).
        if op.is_init:
            ops.append({
                "id": op.id, "name": op.name or op.id, "kind": op.kind,
                "tiles": op.tiles, "core_id": op.core_id,
                "is_init": True, "derived": False, "stages": [], "steps": [],
            })
            continue
        steps = resolved_flow(op)
        ops.append({
            "id": op.id,
            "name": op.name or op.id,
            "kind": op.kind,
            "tiles": op.tiles,
            "core_id": op.core_id,
            "is_init": op.is_init,
            "derived": not op.flow,            # True → steps came from the generic fallback
            "stages": _stages_used(steps),
            "steps": [
                {
                    "id": s.id,
                    "label": s.label,
                    "reads": list(s.reads),
                    "writes": list(s.writes),
                    "data": s.data or s.label,   # datum identity (token label)
                    "expr": s.expr,
                    "note": s.note,
                }
                for s in steps
            ],
        })
    return {
        "title": diagram.title,
        "stage_order": STAGE_ORDER,
        "stage_labels": STAGE_LABELS,
        "ops": ops,
    }


def _endpoint_op(diagram: Diagram, ref: str) -> Op | None:
    """Resolve a dependency endpoint (op id or block id) to its Op."""
    op = diagram.op_by_id(ref)
    if op is not None:
        return op
    return diagram.op_of_block(ref)


def chip_payload(diagram: Diagram) -> dict:
    """Whole-chip view: each core as a [L1→Src→DEST→L1] block plus the cross-core
    (NoC) interactions derived from dependencies whose two ops live on different
    cores. Interactions are time-stamped with the producer op's scheduled end
    clock (when the transfer can fire) and ordered by that time, so stepping
    through them follows the schedule — you see *when* each transfer happens."""
    from tt_prism.schedule import ScheduleError, solve

    cores = diagram.effective_cores()
    ops_by_core: dict[str, list[dict]] = {c.id: [] for c in cores}
    for op in diagram.ops:
        c = diagram.core_of_op(op)
        ops_by_core.setdefault(c.id, []).append(
            {"id": op.id, "name": op.name or op.id, "is_init": op.is_init}
        )

    # Per-op scheduled [start, end] = span of its blocks (for the NoC timeline).
    op_start: dict[str, int] = {}
    op_end: dict[str, int] = {}
    try:
        for b in solve(diagram).blocks:
            op_start[b.op_id] = min(op_start.get(b.op_id, b.start_clock), b.start_clock)
            op_end[b.op_id] = max(op_end.get(b.op_id, b.end_clock), b.end_clock)
    except ScheduleError:
        pass  # leave timing unknown if the graph doesn't schedule

    interactions: list[dict] = []
    for i, d in enumerate(diagram.dependencies):
        fo, to = _endpoint_op(diagram, d.from_), _endpoint_op(diagram, d.to)
        if fo is None or to is None:
            continue
        fc, tc = diagram.core_of_op(fo), diagram.core_of_op(to)
        if fc.id == tc.id:
            continue  # same core → not a NoC transfer
        interactions.append({
            "id": f"noc{i}",
            "from_op": fo.id, "from_core": fc.id,
            "to_op": to.id, "to_core": tc.id,
            "kind": d.kind,
            "label": d.label or f"{fo.name or fo.id} → {to.name or to.id}",
            "at_clock": op_end.get(fo.id),         # transfer fires when producer op ends
            "to_clock": op_start.get(to.id),       # consumer op starts
        })
    # order by when the transfer happens (None timings sort last, stably)
    interactions.sort(key=lambda it: (it["at_clock"] is None, it["at_clock"] or 0))

    return {
        "multicore": len(diagram.cores) > 1,
        "cores": [
            {"id": c.id, "x": c.x, "y": c.y, "name": c.display_name,
             "ops": ops_by_core.get(c.id, [])}
            for c in cores
        ],
        "interactions": interactions,
    }
