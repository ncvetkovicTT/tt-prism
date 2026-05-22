from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DependencyKind = Literal["fifo", "dep", "flow"]


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


class Dependency(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    kind: DependencyKind = "dep"
    label: str = ""


class Diagram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "Untitled pipeline"
    clock_ghz: float | None = None
    grid_clocks: int = 8
    lanes: list[Lane] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    work_items: list[WorkItem] = Field(default_factory=list)
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
        for d in self.dependencies:
            if d.from_ not in item_ids:
                raise ValueError(f"dependency references unknown work item {d.from_}")
            if d.to not in item_ids:
                raise ValueError(f"dependency references unknown work item {d.to}")
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

    def total_clocks(self) -> int:
        if not self.work_items:
            return self.grid_clocks
        return max(w.end_clock for w in self.work_items)

    def ns(self, clocks: int) -> float | None:
        if self.clock_ghz is None:
            return None
        return clocks / self.clock_ghz
