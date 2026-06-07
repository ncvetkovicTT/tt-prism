"""Constraint-aware scheduler for op-authored diagrams.

A diagram authored with ``ops`` (see models.py) does not store absolute start
clocks. Instead each block's start is *derived* here, so the picture is always a
physically legal Tensix schedule. The constraints we encode are deliberately
coarse — one Compute API call ("op") is the smallest unit, attributed wholly to
the resources its blocks name; we do not model sub-LLK / individual Tensix
instructions.

Constraint edges (each edge ``u -> v`` with ``gap`` means
``start[v] >= start[u] + duration[u] + gap``):

* **intra-op precedence** — consecutive blocks of an op (the unpack->math->pack
  valid-bit handshake). Listed order is pipeline order.
* **resource serialization** — a resource (UNPACK/FPU/SFPU/PACK) is a singleton:
  blocks sharing a resource cannot overlap. Ordered by program order.
* **lane serialization** — a TRISC dispatches in order: blocks on the same lane
  cannot overlap. Ordered by program order.
* **explicit dependencies** — the diagram's ``dependencies`` list, e.g. an
  ``l1_data`` edge from a producer op's pack to a consumer op's unpack
  (``c = a + b`` then ``e = d * c``: can't unpack ``c`` until it is packed).

Starts are the longest path from the start of time (ASAP scheduling), computed
in O(V + E). Moving/resizing any block re-runs this and everything downstream
slides to stay legal.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from tt_prism.models import Block, Diagram, Op


@dataclass
class ScheduledBlock:
    block_id: str
    op_id: str
    lane_id: str
    resource_id: str
    label: str
    start_clock: int
    duration_clocks: int
    tags: list[str] = field(default_factory=list)

    @property
    def end_clock(self) -> int:
        return self.start_clock + self.duration_clocks


@dataclass
class Edge:
    """A precedence constraint ``frm -> to`` requiring
    ``start[to] >= start[frm] + duration[frm] + gap``."""

    frm: str
    to: str
    gap: int
    reason: str  # "intra_op" | "resource" | "lane" | dependency kind


@dataclass
class Schedule:
    blocks: list[ScheduledBlock]
    edges: list[Edge]

    def start_of(self, block_id: str) -> int:
        for b in self.blocks:
            if b.block_id == block_id:
                return b.start_clock
        raise KeyError(block_id)

    def total_clocks(self) -> int:
        return max((b.end_clock for b in self.blocks), default=0)


class ScheduleError(ValueError):
    """Raised when the constraints cannot be satisfied (e.g. a cyclic
    dependency, usually a cross-op edge that runs backwards in program order)."""


def _program_order(diagram: Diagram) -> dict[str, int]:
    """Global program-order sequence number per block: ops in declared order,
    blocks within an op in listed (pipeline) order."""
    seq: dict[str, int] = {}
    n = 0
    for op in diagram.ops:
        for b in op.blocks:
            seq[b.id] = n
            n += 1
    return seq


def _resolve_endpoint(diagram: Diagram, ref: str, *, as_source: bool) -> str | None:
    """Resolve a dependency endpoint to a block id. An op resolves to its last
    block when used as a source (producer end) and its first block when used as
    a target (consumer start). Returns None for ids that aren't part of the op
    graph (e.g. legacy work-items), which the scheduler ignores."""
    if diagram.block_by_id(ref) is not None:
        return ref
    op = diagram.op_by_id(ref)
    if op is not None:
        edge_block = op.last_block if as_source else op.first_block
        return edge_block.id if edge_block is not None else None
    return None


def build_edges(diagram: Diagram) -> list[Edge]:
    seq = _program_order(diagram)
    edges: list[Edge] = []

    # intra-op precedence: consecutive blocks of each op
    for op in diagram.ops:
        for prev, nxt in zip(op.blocks, op.blocks[1:]):
            edges.append(Edge(prev.id, nxt.id, gap=0, reason="intra_op"))

    # resource + lane + dest-bank serialization: chain same-key blocks in program
    # order. dest_bank skips blocks that don't touch DEST (dest_bank is None).
    def _serialize(key: str, reason: str, skip_none: bool = False) -> None:
        groups: dict[object, list[str]] = defaultdict(list)
        for op in diagram.ops:
            for b in op.blocks:
                v = getattr(b, key)
                if skip_none and v is None:
                    continue
                groups[v].append(b.id)
        for ids in groups.values():
            ids.sort(key=lambda bid: seq[bid])
            for a, b in zip(ids, ids[1:]):
                edges.append(Edge(a, b, gap=0, reason=reason))

    _serialize("resource_id", "resource")
    _serialize("lane_id", "lane")
    _serialize("dest_bank", "dest_bank", skip_none=True)

    # explicit dependencies (cross-op data deps etc.)
    for d in diagram.dependencies:
        frm = _resolve_endpoint(diagram, d.from_, as_source=True)
        to = _resolve_endpoint(diagram, d.to, as_source=False)
        if frm is None or to is None:
            continue  # endpoint not in the op graph; ignore
        edges.append(Edge(frm, to, gap=d.min_gap_clocks, reason=d.kind))

    return edges


def solve(diagram: Diagram) -> Schedule:
    """Compute ASAP start clocks for every block in the diagram's ops."""
    blocks: dict[str, tuple[Op, Block]] = {}
    for op in diagram.ops:
        for b in op.blocks:
            blocks[b.id] = (op, b)

    edges = build_edges(diagram)
    dur = {bid: b.duration_clocks for bid, (_, b) in blocks.items()}

    adj: dict[str, list[tuple[str, int]]] = defaultdict(list)
    indeg: dict[str, int] = {bid: 0 for bid in blocks}
    for e in edges:
        adj[e.frm].append((e.to, e.gap))
        indeg[e.to] += 1

    start = {bid: 0 for bid in blocks}
    queue = [bid for bid in blocks if indeg[bid] == 0]
    processed = 0
    while queue:
        u = queue.pop()
        processed += 1
        for v, gap in adj[u]:
            cand = start[u] + dur[u] + gap
            if cand > start[v]:
                start[v] = cand
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)

    if processed != len(blocks):
        cyclic = sorted(bid for bid in blocks if indeg[bid] > 0)
        raise ScheduleError(
            "cyclic / unsatisfiable constraints involving blocks: "
            + ", ".join(cyclic)
        )

    scheduled = [
        ScheduledBlock(
            block_id=b.id,
            op_id=op.id,
            lane_id=b.lane_id,
            resource_id=b.resource_id,
            label=b.label or f"{op.name or op.id} · {b.resource_id}",
            start_clock=start[b.id],
            duration_clocks=b.duration_clocks,
            tags=list(b.tags),
        )
        for op, b in (blocks[bid] for bid in sorted(blocks, key=lambda x: start[x]))
    ]
    return Schedule(blocks=scheduled, edges=edges)


def to_render_diagram(diagram: Diagram) -> Diagram:
    """Flatten an op-authored diagram into a flat work-item diagram with resolved
    start clocks, so the existing SVG renderer can draw it. Intra-op and explicit
    cross-op edges are emitted as dependency arrows; serialization edges are not
    (they would clutter the picture)."""
    from tt_prism.models import Dependency, WorkItem

    sched = solve(diagram)
    bank_of = {b.id: b.dest_bank for op in diagram.ops for b in op.blocks}
    work_items = [
        WorkItem(
            id=b.block_id,
            lane_id=b.lane_id,
            resource_id=b.resource_id,
            label=b.label,
            start_clock=b.start_clock,
            duration_clocks=b.duration_clocks,
            tags=[b.op_id, *( [f"dest{bank_of[b.block_id]}"] if bank_of.get(b.block_id) is not None else [] ), *b.tags],
        )
        for b in sched.blocks
    ]

    deps: list[Dependency] = []
    for e in sched.edges:
        if e.reason in ("resource", "lane"):
            continue
        kind = "dep" if e.reason == "intra_op" else e.reason
        if kind not in ("fifo", "dep", "flow", "src_valid", "dst_valid", "l1_data", "issue_order"):
            kind = "dep"
        deps.append(Dependency.model_validate({"from": e.frm, "to": e.to, "kind": kind}))

    return Diagram(
        title=diagram.title,
        clock_ghz=diagram.clock_ghz,
        grid_clocks=diagram.grid_clocks,
        lanes=list(diagram.lanes),
        resources=list(diagram.resources),
        work_items=work_items,
        dependencies=deps,
    )
