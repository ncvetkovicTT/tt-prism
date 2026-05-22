from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from typing import Iterable

from tt_prism.grid import Layout
from tt_prism.models import Diagram, WorkItem


DEFAULT_PALETTE = [
    "#ef9a9a",
    "#90caf9",
    "#a5d6a7",
    "#ffcc80",
    "#ce93d8",
    "#80cbc4",
    "#fff59d",
    "#bcaaa4",
]


@dataclass
class RenderOptions:
    px_per_clock: float = 0.6
    lane_height: int = 72
    show_minor_grid: bool = False
    bar_radius: int = 6
    font_family: str = (
        "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    )


def _resource_color(diagram: Diagram, resource_id: str | None, fallback_idx: int) -> str:
    if resource_id is not None:
        r = diagram.resource_by_id(resource_id)
        if r is not None:
            return r.color
    return DEFAULT_PALETTE[fallback_idx % len(DEFAULT_PALETTE)]


def _canvas_size(diagram: Diagram, layout: Layout) -> tuple[int, int, int]:
    total_clocks = max(diagram.total_clocks(), diagram.grid_clocks)
    # round up to next grid for nicer ruler
    g = max(1, diagram.grid_clocks)
    total_clocks = ((total_clocks + g - 1) // g) * g
    width = int(layout.x(total_clocks) + layout.right_margin)
    if diagram.lanes:
        max_order = max(l.order for l in diagram.lanes)
    else:
        max_order = 0
    height = int(
        layout.y_lane_top(max_order) + layout.lane_height + layout.bottom_margin
    )
    return width, height, total_clocks


def render_svg(diagram: Diagram, options: RenderOptions | None = None) -> str:
    opts = options or RenderOptions()
    layout = Layout(px_per_clock=opts.px_per_clock, lane_height=opts.lane_height)
    width, height, total_clocks = _canvas_size(diagram, layout)

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'font-family="{escape(opts.font_family)}">'
    )
    out.append(_defs())
    out.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa"/>'
    )
    out.append(_title(diagram, width))
    out.extend(_grid(diagram, layout, total_clocks, height, opts))
    out.extend(_lane_rows(diagram, layout, width))
    out.extend(_work_items(diagram, layout, opts))
    out.extend(_dependencies(diagram, layout))
    out.append("</svg>")
    return "\n".join(out)


def _defs() -> str:
    return (
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#444"/>'
        "</marker>"
        "</defs>"
    )


def _title(diagram: Diagram, width: int) -> str:
    return (
        f'<text x="{width / 2}" y="28" font-size="16" font-weight="600" '
        f'text-anchor="middle" fill="#222">{escape(diagram.title)}</text>'
    )


def _grid(
    diagram: Diagram,
    layout: Layout,
    total_clocks: int,
    height: int,
    opts: RenderOptions,
) -> Iterable[str]:
    g = max(1, diagram.grid_clocks)
    top = layout.top_margin - 8
    bottom = height - layout.bottom_margin
    # minor lines
    if opts.show_minor_grid:
        minor = max(1, g // 4)
        for c in range(0, total_clocks + 1, minor):
            if c % g == 0:
                continue
            x = layout.x(c)
            yield (
                f'<line x1="{x}" y1="{top}" x2="{x}" y2="{bottom}" '
                f'stroke="#eee" stroke-width="1"/>'
            )
    # major lines
    for c in range(0, total_clocks + 1, g):
        x = layout.x(c)
        yield (
            f'<line x1="{x}" y1="{top}" x2="{x}" y2="{bottom}" '
            f'stroke="#d0d0d0" stroke-width="1"/>'
        )
    # labels at a stride that guarantees no overlap given current zoom
    max_chars = max(2, len(str(total_clocks)))
    min_px = max_chars * 7 + 12
    px_per_major = g * layout.px_per_clock
    stride_ticks = max(1, math.ceil(min_px / px_per_major)) if px_per_major > 0 else 1
    for c in range(0, total_clocks + 1, g * stride_ticks):
        x = layout.x(c)
        yield (
            f'<text x="{x}" y="{layout.top_margin - 20}" font-size="10" '
            f'text-anchor="middle" fill="#666">{c}</text>'
        )
        if diagram.clock_ghz is not None:
            ns = c / diagram.clock_ghz
            yield (
                f'<text x="{x}" y="{layout.top_margin - 8}" font-size="10" '
                f'text-anchor="middle" fill="#999">{ns:.1f} ns</text>'
            )


def _lane_rows(diagram: Diagram, layout: Layout, width: int) -> Iterable[str]:
    for lane in sorted(diagram.lanes, key=lambda l: l.order):
        y = layout.y_lane_top(lane.order)
        yield (
            f'<rect x="{layout.left_margin}" y="{y}" '
            f'width="{width - layout.left_margin - layout.right_margin}" '
            f'height="{layout.lane_height}" fill="#ffffff" '
            f'stroke="#d6d6d6" stroke-width="1"/>'
        )
        yield (
            f'<text x="{layout.left_margin - 12}" y="{y + layout.lane_height / 2 + 4}" '
            f'font-size="13" font-weight="600" text-anchor="end" fill="#333">'
            f'{escape(lane.name)}</text>'
        )


def _work_items(
    diagram: Diagram, layout: Layout, opts: RenderOptions
) -> Iterable[str]:
    for idx, item in enumerate(diagram.work_items):
        try:
            lane = diagram.lane_by_id(item.lane_id)
        except KeyError:
            continue
        x = layout.x(item.start_clock)
        w = max(2.0, item.duration_clocks * layout.px_per_clock)
        y = layout.y_lane_top(lane.order) + 8
        h = layout.lane_height - 16
        color = _resource_color(diagram, item.resource_id, idx)
        title = _item_title(diagram, item)
        label = escape(item.label or item.id)
        yield (
            f'<g data-item-id="{escape(item.id)}" class="work-item">'
            f"<title>{escape(title)}</title>"
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{opts.bar_radius}" '
            f'ry="{opts.bar_radius}" fill="{color}" stroke="#555" stroke-width="1"/>'
            f'<text x="{x + 6}" y="{y + h / 2 + 4}" font-size="11" fill="#222" '
            f'clip-path="inset(0 0 0 0)">{label}</text>'
            "</g>"
        )


def _item_title(diagram: Diagram, item: WorkItem) -> str:
    parts = [f"{item.id}"]
    if item.label and item.label != item.id:
        parts.append(item.label)
    parts.append(
        f"start={item.start_clock}clk  dur={item.duration_clocks}clk  "
        f"end={item.end_clock}clk"
    )
    ns = diagram.ns(item.duration_clocks)
    if ns is not None:
        parts.append(f"({ns:.2f} ns)")
    if item.resource_id:
        parts.append(f"resource={item.resource_id}")
    if item.tags:
        parts.append(f"tags={','.join(item.tags)}")
    return " | ".join(parts)


def _dependencies(diagram: Diagram, layout: Layout) -> Iterable[str]:
    offsets = _dep_offsets(diagram, layout.lane_height)
    for dep, (src_dy, tgt_dy) in zip(diagram.dependencies, offsets):
        try:
            a = diagram.item_by_id(dep.from_)
            b = diagram.item_by_id(dep.to)
            la = diagram.lane_by_id(a.lane_id)
            lb = diagram.lane_by_id(b.lane_id)
        except KeyError:
            continue
        forward = b.start_clock >= a.start_clock
        # Pick sensible anchor points depending on how the two bars overlap in time:
        # - gapless forward / overlap: connect via target.start column at edges
        #   facing each other (short vertical/diagonal, no lane crossings)
        # - clean forward with gap: connect source.end to target.start (canonical)
        # - back-edge: loop over the top
        x_src_end = layout.x(a.end_clock)
        x_tgt_start = layout.x(b.start_clock)
        y_src_c = layout.y_lane_center(la.order)
        y_tgt_c = layout.y_lane_center(lb.order)

        if forward and b.start_clock < a.end_clock and la.order != lb.order:
            # Overlap forward: anchor at target.start, source-edge-facing-target
            # → target-edge-facing-source. Bezier smooths the vertical run.
            half_h = layout.lane_height / 2 - 2
            if lb.order > la.order:
                y1 = y_src_c + half_h + src_dy
                y2 = y_tgt_c - half_h + tgt_dy
            else:
                y1 = y_src_c - half_h + src_dy
                y2 = y_tgt_c + half_h + tgt_dy
            x1 = x_tgt_start
            x2 = x_tgt_start
        else:
            x1 = x_src_end
            y1 = y_src_c + src_dy
            x2 = x_tgt_start
            y2 = y_tgt_c + tgt_dy

        path_d = _bezier_path(
            x1=x1, y1=y1, x2=x2, y2=y2,
            forward=forward,
            order_src=la.order, order_tgt=lb.order,
            layout=layout,
        )
        stroke = {"fifo": "#1976d2", "flow": "#2e7d32"}.get(dep.kind, "#444")
        dash = ' stroke-dasharray="4 3"' if dep.kind == "flow" else ""
        yield (
            f'<path class="dep-arrow" data-from="{escape(dep.from_)}" '
            f'data-to="{escape(dep.to)}" d="{path_d}" fill="none" stroke="{stroke}" '
            f'stroke-width="1.4"{dash} '
            f'marker-end="url(#arrow)"/>'
        )
        if dep.label:
            lx = (x1 + x2) / 2
            ly = (y1 + y2) / 2 - 4
            yield (
                f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="10" '
                f'fill="#555" text-anchor="middle">{escape(dep.label)}</text>'
            )


def _dep_offsets(diagram: Diagram, lane_height: int) -> list[tuple[float, float]]:
    """Per-item fan-out: edges sharing a source get distinct exit Ys along the
    bar's right edge; edges sharing a target get distinct entry Ys along the
    left edge. Bezier curves between them naturally diverge.

    To reduce crossings, we sort outgoing edges by target lane order (and then
    by target start), and incoming edges by source lane order. That way higher
    targets attach near the top and lower targets near the bottom.
    """
    bar_h = max(16, lane_height - 16)
    # Build per-source and per-target lists of edge indices with sort keys.
    out_lists: dict[str, list[tuple[tuple, int]]] = {}
    in_lists: dict[str, list[tuple[tuple, int]]] = {}
    for idx, d in enumerate(diagram.dependencies):
        try:
            a = diagram.item_by_id(d.from_)
            b = diagram.item_by_id(d.to)
            la = diagram.lane_by_id(a.lane_id)
            lb = diagram.lane_by_id(b.lane_id)
        except KeyError:
            continue
        out_lists.setdefault(d.from_, []).append(((lb.order, b.start_clock), idx))
        in_lists.setdefault(d.to, []).append(((la.order, a.start_clock), idx))
    out_rank: dict[int, tuple[int, int]] = {}
    for items in out_lists.values():
        items.sort()
        n = len(items)
        for rank, (_, idx) in enumerate(items):
            out_rank[idx] = (rank, n)
    in_rank: dict[int, tuple[int, int]] = {}
    for items in in_lists.values():
        items.sort()
        n = len(items)
        for rank, (_, idx) in enumerate(items):
            in_rank[idx] = (rank, n)
    offsets: list[tuple[float, float]] = []
    for idx in range(len(diagram.dependencies)):
        ro, no = out_rank.get(idx, (0, 1))
        ri, ni = in_rank.get(idx, (0, 1))
        step_o = min(12.0, bar_h / (no + 1))
        step_i = min(12.0, bar_h / (ni + 1))
        offsets.append((
            (ro - (no - 1) / 2.0) * step_o,
            (ri - (ni - 1) / 2.0) * step_i,
        ))
    return offsets


def _bezier_path(
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    forward: bool,
    order_src: int,
    order_tgt: int,
    layout: Layout,
) -> str:
    """Cubic Bezier path from (x1,y1) to (x2,y2).

    Tangent strategy:
    - Forward with positive horizontal gap: horizontal tangents at both ends
      (classic "flow" curve, arrow enters target from the left).
    - Overlap / same-column anchors (x1 == x2): vertical tangents so the curve
      is a smooth vertical drop between lanes.
    - Back-edge: control points driven high above the topmost lane to force a
      graceful loop.
    """
    dx = x2 - x1
    dy = y2 - y1
    if not forward:
        top_order = min(order_src, order_tgt)
        top_y = layout.y_lane_top(top_order) - 28
        lift = max(30.0, abs(x1 - x2) * 0.25 + 24)
        c1x, c1y = x1 + lift, top_y
        c2x, c2y = x2 - lift, top_y
    elif abs(dx) < 1e-3:
        # Vertical drop (overlap case): vertical tangents both ends.
        t = max(10.0, abs(dy) * 0.35)
        sign = 1 if dy >= 0 else -1
        c1x, c1y = x1, y1 + t * sign
        c2x, c2y = x2, y2 - t * sign
    else:
        # Horizontal tangents.
        t = max(18.0, dx * 0.45) if dx >= 0 else max(14.0, -dx * 0.2 + 14)
        c1x, c1y = x1 + t, y1
        c2x, c2y = x2 - t, y2
    return (
        f"M {x1:.1f} {y1:.1f} "
        f"C {c1x:.1f} {c1y:.1f}, {c2x:.1f} {c2y:.1f}, {x2:.1f} {y2:.1f}"
    )
