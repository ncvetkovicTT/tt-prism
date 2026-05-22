from __future__ import annotations

import pytest

from tt_prism import storage
from tt_prism.models import Dependency, Diagram, Lane, Resource, WorkItem


def _sample() -> Diagram:
    return Diagram(
        title="t",
        grid_clocks=8,
        lanes=[Lane(id="l0", name="TRISC 0", order=0), Lane(id="l1", name="TRISC 1", order=1)],
        resources=[Resource(id="fpu", name="FPU", color="#a5d6a7")],
        work_items=[
            WorkItem(id="a", lane_id="l0", resource_id="fpu", start_clock=0, duration_clocks=16),
            WorkItem(id="b", lane_id="l1", resource_id="fpu", start_clock=16, duration_clocks=32),
        ],
        dependencies=[Dependency.model_validate({"from": "a", "to": "b", "kind": "fifo"})],
    )


def test_refs_validated():
    with pytest.raises(Exception):
        Diagram(
            lanes=[Lane(id="l0", name="L0")],
            work_items=[WorkItem(id="a", lane_id="missing")],
        )


def test_duplicate_ids_rejected():
    with pytest.raises(Exception):
        Diagram(
            lanes=[Lane(id="l0", name="L0")],
            work_items=[
                WorkItem(id="x", lane_id="l0"),
                WorkItem(id="x", lane_id="l0"),
            ],
        )


def test_dependency_ref_validated():
    with pytest.raises(Exception):
        Diagram(
            lanes=[Lane(id="l0", name="L0")],
            work_items=[WorkItem(id="a", lane_id="l0")],
            dependencies=[Dependency.model_validate({"from": "a", "to": "b"})],
        )


def test_roundtrip_yaml(tmp_path):
    d = _sample()
    p = tmp_path / "diag.yaml"
    storage.dump(d, p)
    d2 = storage.load(p)
    assert d2.title == d.title
    assert [w.id for w in d2.work_items] == [w.id for w in d.work_items]
    assert d2.dependencies[0].from_ == "a"
    assert d2.dependencies[0].to == "b"


def test_total_clocks_and_ns():
    d = _sample()
    d.clock_ghz = 1.0
    assert d.total_clocks() == 48
    assert d.ns(16) == pytest.approx(16.0)
