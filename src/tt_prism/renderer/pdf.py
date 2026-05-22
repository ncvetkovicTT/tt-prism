from __future__ import annotations

from pathlib import Path

from tt_prism.models import Diagram
from tt_prism.renderer.svg import RenderOptions, render_svg


def render_pdf(diagram: Diagram, path: str | Path, options: RenderOptions | None = None) -> None:
    try:
        import cairosvg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PDF export requires the 'pdf' extra. Install with: pip install 'tt-prism[pdf]'"
        ) from exc
    svg = render_svg(diagram, options)
    cairosvg.svg2pdf(bytestring=svg.encode("utf-8"), write_to=str(path))
