from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Cosmetic kinds (legacy, control arrow color only) plus the semantic kinds the
# scheduler understands. See schedule.py for how each semantic kind becomes a
# precedence edge.
#   src_valid   unpack -> math  (SrcA/SrcB valid-bit handshake)
#   dst_valid   math   -> pack  (Dst row valid-bit handshake)
#   l1_data     pack   -> unpack of a *later* op (RAW through L1; e.g. c=a+b then e=d*c)
#   issue_order program-order edge between two units on the same TRISC
DependencyKind = Literal[
    "fifo", "dep", "flow",          # legacy / cosmetic
    "src_valid", "dst_valid", "l1_data", "issue_order",  # semantic
]

# The four execution resources tt-prism models. THCON / sync time is folded into
# whichever resource wraps it (coarse attribution is intentional).
ResourceKind = Literal["unpack", "fpu", "sfpu", "pack"]


class Lane(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    order: int = 0


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    color: str = "#9ecae1"


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    lane_id: str
    resource_id: str | None = None
    label: str = ""
    start_clock: int = 0
    duration_clocks: int = 1
    tags: list[str] = Field(default_factory=list)

    @property
    def end_clock(self) -> int:
        return self.start_clock + self.duration_clocks


class Block(BaseModel):
    """One unit of work inside an Op: a single resource (UNPACK/FPU/SFPU/PACK)
    held for ``duration_clocks``, dispatched by one TRISC lane.

    A block has no ``start_clock`` — its start is *derived* by the scheduler
    (schedule.py) from the dependency/resource constraints. Durations are
    user-supplied (we do not model sub-LLK timing)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    lane_id: str
    resource_id: str
    label: str = ""
    duration_clocks: int = 1
    tags: list[str] = Field(default_factory=list)


class Op(BaseModel):
    """One Compute API call (e.g. mm_init, reduce_tile, pack_untilize_dest),
    which wraps one or more LLK calls. This is the lowest granularity tt-prism
    models. ``blocks`` are listed in pipeline order: consecutive blocks get an
    intra-op precedence edge (the unpack->math->pack handshake)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    kind: str = ""           # free-form: "matmul", "reduce", ... (drives nothing yet)
    tiles: int = 1           # number of tiles operated on (metadata for now)
    blocks: list[Block] = Field(default_factory=list)

    @property
    def first_block(self) -> Block | None:
        return self.blocks[0] if self.blocks else None

    @property
    def last_block(self) -> Block | None:
        return self.blocks[-1] if self.blocks else None


class Dependency(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    kind: DependencyKind = "dep"
    label: str = ""
    # Extra clocks the consumer must wait beyond the producer's end (e.g.
    # semaphore / sync latency). 0 means "starts the cycle the producer ends".
    min_gap_clocks: int = 0


class Diagram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "Untitled pipeline"
    clock_ghz: float | None = None
    grid_clocks: int = 8
    lanes: list[Lane] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    work_items: list[WorkItem] = Field(default_factory=list)
    # Higher-level authoring layer. A diagram is authored *either* as flat
    # work_items (legacy) *or* as ops (scheduler-driven). Dependencies may
    # reference work-item ids, block ids, or op ids.
    ops: list[Op] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_refs(self) -> "Diagram":
        lane_ids = {l.id for l in self.lanes}
        resource_ids = {r.id for r in self.resources}
        item_ids: set[str] = set()
        for w in self.work_items:
            if w.id in item_ids:
                raise ValueError(f"duplicate work item id: {w.id}")
            item_ids.add(w.id)
            if w.lane_id not in lane_ids:
                raise ValueError(f"work item {w.id} references unknown lane {w.lane_id}")
            if w.resource_id is not None and w.resource_id not in resource_ids:
                raise ValueError(
                    f"work item {w.id} references unknown resource {w.resource_id}"
                )
        # ops + their blocks
        op_ids: set[str] = set()
        block_ids: set[str] = set()
        for op in self.ops:
            if op.id in op_ids:
                raise ValueError(f"duplicate op id: {op.id}")
            op_ids.add(op.id)
            for b in op.blocks:
                if b.id in block_ids or b.id in item_ids:
                    raise ValueError(f"duplicate block id: {b.id}")
                block_ids.add(b.id)
                if b.lane_id not in lane_ids:
                    raise ValueError(
                        f"block {b.id} (op {op.id}) references unknown lane {b.lane_id}"
                    )
                if b.resource_id not in resource_ids:
                    raise ValueError(
                        f"block {b.id} (op {op.id}) references unknown resource {b.resource_id}"
                    )
        # A dependency endpoint may name a work-item, a block, or an op.
        ref_ids = item_ids | block_ids | op_ids
        for d in self.dependencies:
            if d.from_ not in ref_ids:
                raise ValueError(f"dependency references unknown id {d.from_}")
            if d.to not in ref_ids:
                raise ValueError(f"dependency references unknown id {d.to}")
        return self

    def lane_by_id(self, lane_id: str) -> Lane:
        for l in self.lanes:
            if l.id == lane_id:
                return l
        raise KeyError(lane_id)

    def resource_by_id(self, resource_id: str) -> Resource | None:
        for r in self.resources:
            if r.id == resource_id:
                return r
        return None

    def item_by_id(self, item_id: str) -> WorkItem:
        for w in self.work_items:
            if w.id == item_id:
                return w
        raise KeyError(item_id)

    def op_by_id(self, op_id: str) -> Op | None:
        for op in self.ops:
            if op.id == op_id:
                return op
        return None

    def block_by_id(self, block_id: str) -> Block | None:
        for op in self.ops:
            for b in op.blocks:
                if b.id == block_id:
                    return b
        return None

    def op_of_block(self, block_id: str) -> Op | None:
        for op in self.ops:
            for b in op.blocks:
                if b.id == block_id:
                    return op
        return None

    def total_clocks(self) -> int:
        if not self.work_items:
            return self.grid_clocks
        return max(w.end_clock for w in self.work_items)

    def ns(self, clocks: int) -> float | None:
        if self.clock_ghz is None:
            return None
        return clocks / self.clock_ghz
