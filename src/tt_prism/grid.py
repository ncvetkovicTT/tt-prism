from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    """Maps clocks and lane indices to pixel coordinates.

    The grid in clock-space is the user-facing snap unit; lane height is fixed.
    """

    px_per_clock: float = 0.6
    lane_height: int = 72
    lane_gutter: int = 8
    top_margin: int = 56
    left_margin: int = 140
    right_margin: int = 24
    bottom_margin: int = 24

    def x(self, clock: int | float) -> float:
        return self.left_margin + clock * self.px_per_clock

    def y_lane_top(self, order: int) -> float:
        return self.top_margin + order * (self.lane_height + self.lane_gutter)

    def y_lane_center(self, order: int) -> float:
        return self.y_lane_top(order) + self.lane_height / 2
