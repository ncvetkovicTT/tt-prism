"""Render-geometry invariants — the Python-side analogue of the GUI "regions
don't overlap" check.

These use ``tt_prism.grid.Layout`` (the exact math the SVG renderer uses) plus
the renderer's own ``_canvas_size`` / bar-band constants, so what we assert here
is what gets drawn. We flatten a real multi-core example and check that:

* lane rows never vertically overlap (each lane's [top, top+H] band is disjoint);
* the inter-core gap rows the flattener inserts are actually empty (no lane
  occupies a gap ``order``), giving the visual break between cores;
* each work-item's vertical band sits *inside* its lane's row;
* each work-item's horizontal x-span [x(start), x(end)] is within the canvas.

GUI behaviors that can only be checked in a browser (arrows actually drawn,
panels not visually overlapping, highlight classes toggling) are listed in
tests/MANUAL_GUI_CHECKLIST.md.
"""

from __future__ import annotations

from tt_prism.grid import Layout
from tt_prism.models import Block, Core, Diagram, Lane, Op, Resource
from tt_prism.renderer.svg import RenderOptions, _canvas_size
from tt_prism.schedule import to_render_diagram

# The renderer insets each bar by these amounts inside its lane row (see
# svg.py:_work_items): y = y_lane_top + BAR_INSET_TOP, h = lane_height - 2*BAR_INSET_TOP.
BAR_INSET_TOP = 8


def _layout(opts: RenderOptions) -> Layout:
    return Layout(px_per_clock=opts.px_per_clock, lane_height=opts.lane_height)


def _bar_band(layout: Layout, order: int) -> tuple[float, float]:
    top = layout.y_lane_top(order) + BAR_INSET_TOP
    h = layout.lane_height - 2 * BAR_INSET_TOP
    return top, top + h


def _mc_diagram() -> Diagram:
    cores = [Core(id="c00", x=0, y=0), Core(id="c10", x=1, y=0), Core(id="c20", x=2, y=0)]
    lanes = [
        Lane(id="t0", name="UNPACK", order=0),
        Lane(id="t1", name="MATH", order=1),
        Lane(id="t2", name="PACK", order=2),
    ]
    resources = [Resource(id="unpack", name="U"), Resource(id="fpu", name="F"),
                 Resource(id="pack", name="P")]

    def mm(op_id, core):
        return Op(id=op_id, name=op_id, core_id=core, blocks=[
            Block(id=f"{op_id}_u", lane_id="t0", resource_id="unpack", duration_clocks=20),
            Block(id=f"{op_id}_m", lane_id="t1", resource_id="fpu", duration_clocks=12),
            Block(id=f"{op_id}_p", lane_id="t2", resource_id="pack", duration_clocks=20)])

    return Diagram(title="geom", grid_clocks=32, cores=cores, lanes=lanes,
                   resources=resources,
                   ops=[mm("a", "c00"), mm("b", "c10"), mm("c", "c20")])


def test_lane_rows_do_not_vertically_overlap():
    flat = to_render_diagram(_mc_diagram())
    layout = _layout(RenderOptions())
    lanes = sorted(flat.lanes, key=lambda l: l.order)
    bands = [(l.order,
              layout.y_lane_top(l.order),
              layout.y_lane_top(l.order) + layout.lane_height)
             for l in lanes]
    # adjacent lanes are disjoint (a lower lane starts at or below the previous bottom)
    for (_, _, prev_bottom), (_, nxt_top, _) in zip(bands, bands[1:]):
        assert nxt_top >= prev_bottom, "lane rows overlap vertically"


def test_intercore_gap_rows_are_respected():
    flat = to_render_diagram(_mc_diagram())
    occupied = {l.order for l in flat.lanes}
    # 3 cores x 3 lanes, stride 4: lanes at orders {1,2,3, 5,6,7, 9,10,11};
    # gap (header) rows at {0,4,8} stay empty so each core gets a visual break.
    assert occupied == {1, 2, 3, 5, 6, 7, 9, 10, 11}
    for gap in (0, 4, 8):
        assert gap not in occupied, f"gap row {gap} should be empty"
    # cores carry group chrome (header label drawn in the gap row above each)
    assert all(l.group is not None for l in flat.lanes)


def test_each_work_item_band_inside_its_lane():
    flat = to_render_diagram(_mc_diagram())
    layout = _layout(RenderOptions())
    order_of = {l.id: l.order for l in flat.lanes}
    for w in flat.work_items:
        order = order_of[w.lane_id]
        lane_top = layout.y_lane_top(order)
        lane_bottom = lane_top + layout.lane_height
        bar_top, bar_bottom = _bar_band(layout, order)
        assert lane_top <= bar_top < bar_bottom <= lane_bottom, (
            f"{w.id} band [{bar_top},{bar_bottom}] escapes lane "
            f"[{lane_top},{lane_bottom}]"
        )


def test_work_item_x_span_within_canvas():
    flat = to_render_diagram(_mc_diagram())
    opts = RenderOptions()
    layout = _layout(opts)
    width, height, _total = _canvas_size(flat, layout)
    for w in flat.work_items:
        x0 = layout.x(w.start_clock)
        # the renderer floors bar width at 2px; mirror that here
        bar_w = max(2.0, w.duration_clocks * layout.px_per_clock)
        x1 = x0 + bar_w
        assert layout.left_margin <= x0, f"{w.id} starts left of the lane area"
        assert x1 <= width - layout.right_margin + 1e-6, (
            f"{w.id} x-span end {x1} exceeds canvas width {width}"
        )
        # and vertically the lane sits within the canvas height
        _, bar_bottom = _bar_band(layout, {l.id: l.order for l in flat.lanes}[w.lane_id])
        assert bar_bottom <= height


def test_no_two_bars_on_same_lane_overlap_in_time_and_space():
    # If two bars share a lane row their x-spans must not overlap (the scheduler's
    # lane serialization guarantees this; here we confirm the geometry reflects it).
    flat = to_render_diagram(_mc_diagram())
    layout = _layout(RenderOptions())
    by_lane: dict[str, list] = {}
    for w in flat.work_items:
        by_lane.setdefault(w.lane_id, []).append(w)
    for lane_id, items in by_lane.items():
        items.sort(key=lambda w: w.start_clock)
        for a, b in zip(items, items[1:]):
            ax1 = layout.x(a.start_clock) + max(2.0, a.duration_clocks * layout.px_per_clock)
            bx0 = layout.x(b.start_clock)
            assert bx0 >= ax1 - 1e-6, (
                f"bars {a.id} and {b.id} overlap horizontally on lane {lane_id}"
            )


def test_x_mapping_is_monotone():
    # Sanity: the clock→pixel mapping is strictly increasing, so a later clock is
    # always drawn to the right (the whole geometry above relies on this).
    layout = _layout(RenderOptions())
    assert layout.x(100) > layout.x(50) > layout.x(0)
