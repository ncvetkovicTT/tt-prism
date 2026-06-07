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


class Core(BaseModel):
    """One Tensix core on the chip, addressed by grid coordinates (x, y). Each
    core has its own TRISC lanes and its own UNPACK/FPU/SFPU/PACK engines and
    DEST banks — so resource/lane/bank serialization is scoped per core."""

    model_config = ConfigDict(extra="forbid")

    id: str
    x: int = 0
    y: int = 0
    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or f"Core ({self.x},{self.y})"


class Lane(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    order: int = 0
    # Set by the flattener for multi-core diagrams: a group key (the core id) and
    # a header label drawn above the first lane of each group. None for
    # single-core / flat diagrams (no grouping chrome).
    group: str | None = None
    group_label: str = ""


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
    # Which virtual DEST bank this block writes (MATH: FPU/SFPU) or reads (PACK).
    # DEST is split in software into `Diagram.dest_banks` banks so PACK can drain
    # one bank while MATH fills another. Blocks sharing a bank are serialized:
    # MATH cannot reuse a bank until the PACK that last read it has finished.
    # None for blocks that don't touch DEST (e.g. UNPACK).
    dest_bank: int | None = None
    tags: list[str] = Field(default_factory=list)


# Canonical Tensix storage stages a tile passes through, used by the flow view.
FlowStageKind = Literal["l1_in", "srca", "srcb", "dest", "sfpu", "l1_out"]

OpCategory = Literal["init", "execute"]


class FlowStep(BaseModel):
    """One dataflow step inside an *execute* op, for the flow view: a tile (or
    face) moves from the ``reads`` stages to the ``writes`` stages (e.g. unpack
    L1→SrcA, math SrcA/SrcB→DEST, reuse DEST→SrcB via MOVD2B, pack DEST→L1).
    ``expr`` optionally records what now lives in DEST (e.g. ``DEST += A·B``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    label: str
    reads: list[FlowStageKind] = Field(default_factory=list)
    writes: list[FlowStageKind] = Field(default_factory=list)
    expr: str = ""
    note: str = ""


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
    core_id: str | None = None  # which Core this op runs on (None → the single/default core)
    # Op category. None → inferred: an op whose name/kind mentions "init"
    # (init, reinit, mm_init, …) is an init op; everything else is execute.
    # The flow view only applies to execute ops.
    category: OpCategory | None = None
    # Optional explicit dataflow for the flow view. If empty, a generic
    # unpack→math→pack flow is derived from the op's blocks (see flow.py).
    flow: list[FlowStep] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)

    @property
    def is_init(self) -> bool:
        if self.category is not None:
            return self.category == "init"
        return "init" in f"{self.name} {self.kind}".lower()

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
    dest_banks: int = 2      # number of virtual DEST banks software splits DEST into
    cores: list[Core] = Field(default_factory=list)   # Tensix cores; empty → single implicit core
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
        # cores
        core_ids: set[str] = set()
        for c in self.cores:
            if c.id in core_ids:
                raise ValueError(f"duplicate core id: {c.id}")
            core_ids.add(c.id)
        # ops + their blocks
        op_ids: set[str] = set()
        block_ids: set[str] = set()
        for op in self.ops:
            if op.id in op_ids:
                raise ValueError(f"duplicate op id: {op.id}")
            op_ids.add(op.id)
            if op.core_id is not None and op.core_id not in core_ids:
                raise ValueError(f"op {op.id} references unknown core {op.core_id}")
            if op.core_id is None and len(self.cores) > 1:
                raise ValueError(
                    f"op {op.id} must set core_id (diagram declares {len(self.cores)} cores)"
                )
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
                if b.dest_bank is not None and not (0 <= b.dest_bank < self.dest_banks):
                    raise ValueError(
                        f"block {b.id} (op {op.id}) uses dest_bank {b.dest_bank} "
                        f"but dest_banks={self.dest_banks} (valid 0..{self.dest_banks - 1})"
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

    def core_by_id(self, core_id: str) -> Core | None:
        for c in self.cores:
            if c.id == core_id:
                return c
        return None

    def effective_cores(self) -> list[Core]:
        """The cores to lay out: the declared cores, or a single implicit core
        (id ``"__core__"``) for single-core / legacy diagrams."""
        return self.cores or [Core(id="__core__", x=0, y=0, name="")]

    def core_of_op(self, op: Op) -> Core:
        """Resolve the core an op runs on, defaulting to the first effective core."""
        if op.core_id is not None:
            c = self.core_by_id(op.core_id)
            if c is not None:
                return c
        return self.effective_cores()[0]

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
        # Operates on flat work_items only. For op-authored diagrams (no
        # work_items) the makespan comes from the scheduler — call
        # schedule.solve(...).total_clocks() or flatten via to_render_diagram first.
        if not self.work_items:
            return self.grid_clocks
        return max(w.end_clock for w in self.work_items)

    def ns(self, clocks: int) -> float | None:
        if self.clock_ghz is None:
            return None
        return clocks / self.clock_ghz
