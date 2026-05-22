from __future__ import annotations

from tt_prism.models import Dependency, Diagram, Lane, Resource, WorkItem
from tt_prism.renderer.svg import render_svg


def _diagram() -> Diagram:
    return Diagram(
        title="Test",
        clock_ghz=1.0,
        grid_clocks=8,
        lanes=[Lane(id="l0", name="TRISC 0", order=0), Lane(id="l1", name="TRISC 1", order=1)],
        resources=[Resource(id="fpu", name="FPU", color="#a5d6a7")],
        work_items=[
            WorkItem(id="a", lane_id="l0", resource_id="fpu", label="A",
                     start_clock=0, duration_clocks=16, tags=["m"]),
            WorkItem(id="b", lane_id="l1", resource_id="fpu", label="B",
                     start_clock=16, duration_clocks=32, tags=["m"]),
        ],
        dependencies=[Dependency.model_validate({"from": "a", "to": "b", "kind": "fifo"})],
    )


def test_renders_svg_with_expected_content():
    svg = render_svg(_diagram())
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "TRISC 0" in svg
    assert "TRISC 1" in svg
    assert 'data-item-id="a"' in svg
    assert 'data-item-id="b"' in svg
    # dependency path present
    assert 'marker-end="url(#arrow)"' in svg
    # ns label because clock_ghz is set
    assert "ns" in svg


def test_empty_diagram_renders():
    d = Diagram(title="Empty", grid_clocks=8)
    svg = render_svg(d)
    assert svg.startswith("<svg")
    assert "Empty" in svg
