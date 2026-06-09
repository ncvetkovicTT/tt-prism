"""Regression: every shipped example YAML must load, schedule, flatten, and
render to well-formed SVG.

This is the regression test for a real bug we fixed: the multi-core flattener
could emit a dependency arrow referencing a renamed/replicated work-item id that
no longer existed in the flattened diagram. The CRITICAL sub-check below
(``every flattened dep references an existing work-item id``) guards exactly
that. Everything here runs in pure Python (no fastapi / cairosvg / browser).
"""

from __future__ import annotations

import glob
import os
from xml.dom import minidom

import pytest

from tt_prism import storage
from tt_prism.renderer.svg import render_svg
from tt_prism.schedule import solve, to_render_diagram

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = sorted(
    glob.glob(os.path.join(_REPO_ROOT, "examples", "**", "*.yaml"), recursive=True)
)


def _rel(path: str) -> str:
    return os.path.relpath(path, _REPO_ROOT)


def test_examples_glob_is_nonempty():
    # Guard against a broken glob silently parametrizing zero files (which would
    # make every example test "pass" by never running).
    assert EXAMPLES, "no example YAMLs found — glob/path is wrong"
    assert any("deepseek" in p for p in EXAMPLES), "deepseek examples not picked up"


@pytest.mark.parametrize("path", EXAMPLES, ids=_rel)
def test_example_loads(path):
    d = storage.load(path)
    assert d is not None


@pytest.mark.parametrize("path", EXAMPLES, ids=_rel)
def test_example_solves_flattens_and_renders(path):
    d = storage.load(path)
    if not d.ops:
        pytest.skip("flat work-item diagram (no ops to schedule)")

    # schedules
    sched = solve(d)
    assert sched.total_clocks() >= 0

    # flattens
    flat = to_render_diagram(d)
    assert flat.work_items, "flattening produced no work items"

    # CRITICAL: every dependency in the flattened render diagram references an
    # existing work-item id (the bug was a dep referencing a renamed/replicated id).
    ids = {w.id for w in flat.work_items}
    for dep in flat.dependencies:
        assert dep.from_ in ids, f"{_rel(path)}: dep.from_ {dep.from_!r} missing"
        assert dep.to in ids, f"{_rel(path)}: dep.to {dep.to!r} missing"

    # every work-item lands on a lane that actually exists
    lane_ids = {l.id for l in flat.lanes}
    for w in flat.work_items:
        assert w.lane_id in lane_ids, f"{_rel(path)}: {w.id} on missing lane {w.lane_id}"

    # renders to non-empty, well-formed SVG
    svg = render_svg(flat)
    assert svg.strip(), "empty SVG"
    doc = minidom.parseString(svg)  # raises if not well-formed XML
    assert doc.documentElement.tagName == "svg"


@pytest.mark.parametrize("path", EXAMPLES, ids=_rel)
def test_flat_example_renders(path):
    # Diagrams authored as flat work_items (no ops) render directly.
    d = storage.load(path)
    if d.ops:
        pytest.skip("op-authored diagram (covered by the solve/flatten test)")
    if not d.work_items:
        pytest.skip("empty diagram")
    svg = render_svg(d)
    doc = minidom.parseString(svg)
    assert doc.documentElement.tagName == "svg"
