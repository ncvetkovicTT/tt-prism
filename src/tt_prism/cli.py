from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

import typer
from rich.console import Console

from tt_prism import storage
from tt_prism.models import Dependency, Diagram, Lane, Resource, WorkItem
from tt_prism.renderer.svg import RenderOptions, render_svg

app = typer.Typer(
    add_completion=False,
    help="tt-prism — pipeline swim-lane diagrams for TRISC/Tensix perf modeling",
    no_args_is_help=True,
)
console = Console()


def _starter_diagram() -> Diagram:
    return Diagram(
        title="New pipeline",
        clock_ghz=1.0,
        grid_clocks=32,
        lanes=[
            Lane(id="trisc0", name="TRISC 0", order=0),
            Lane(id="trisc1", name="TRISC 1", order=1),
            Lane(id="trisc2", name="TRISC 2", order=2),
        ],
        resources=[
            Resource(id="unpack", name="UNPACK", color="#ef9a9a"),
            Resource(id="fpu", name="FPU", color="#a5d6a7"),
            Resource(id="sfpu", name="SFPU", color="#90caf9"),
            Resource(id="pack", name="PACK", color="#ffcc80"),
            Resource(id="thcon", name="THCON", color="#ce93d8"),
        ],
        work_items=[
            WorkItem(
                id="a0",
                lane_id="trisc0",
                resource_id="unpack",
                label="UNPACK A0",
                start_clock=0,
                duration_clocks=52,
                tags=["init"],
            ),
            WorkItem(
                id="b0",
                lane_id="trisc1",
                resource_id="fpu",
                label="FPU B0 x A0",
                start_clock=52,
                duration_clocks=16,
                tags=["matmul"],
            ),
            WorkItem(
                id="c0",
                lane_id="trisc2",
                resource_id="pack",
                label="PACK C0",
                start_clock=68,
                duration_clocks=32,
                tags=["pack"],
            ),
        ],
        dependencies=[
            Dependency.model_validate({"from": "a0", "to": "b0", "kind": "fifo"}),
            Dependency.model_validate({"from": "b0", "to": "c0", "kind": "fifo"}),
        ],
    )


@app.command()
def new(
    path: Path = typer.Argument(..., help="Path to write the new diagram YAML"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite if file exists"),
) -> None:
    """Scaffold a minimal diagram YAML file."""
    if path.exists() and not force:
        console.print(f"[red]refusing to overwrite[/red] {path} (pass --force)")
        raise typer.Exit(code=1)
    storage.dump(_starter_diagram(), path)
    console.print(f"[green]wrote[/green] {path}")


@app.command()
def validate(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Diagram YAML"),
) -> None:
    """Validate a diagram file."""
    try:
        d = storage.load(path)
    except Exception as e:
        console.print(f"[red]invalid[/red] {path}:\n{e}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]ok[/green] {path}  "
        f"lanes={len(d.lanes)} items={len(d.work_items)} "
        f"deps={len(d.dependencies)} total={d.total_clocks()} clk"
    )


@app.command()
def render(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Diagram YAML"),
    output: Path = typer.Option(
        Path("diagram.svg"), "--output", "-o", help="Output file (.svg or .pdf)"
    ),
    px_per_clock: float = typer.Option(0.6, "--px-per-clock"),
    lane_height: int = typer.Option(72, "--lane-height"),
) -> None:
    """Render a diagram to SVG (or PDF if output ends in .pdf)."""
    d = storage.load(path)
    opts = RenderOptions(px_per_clock=px_per_clock, lane_height=lane_height)
    if output.suffix.lower() == ".pdf":
        from tt_prism.renderer.pdf import render_pdf

        render_pdf(d, output, opts)
    else:
        svg = render_svg(d, opts)
        output.write_text(svg)
    console.print(f"[green]rendered[/green] {output}")


@app.command()
def serve(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Diagram YAML"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Launch the local web editor on the given diagram."""
    import uvicorn

    from tt_prism.server import build_app

    web = build_app(path)
    url = f"http://{host}:{port}"
    console.print(f"[cyan]tt-prism editor[/cyan] → {url}  (editing {path})")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(web, host=host, port=port, log_level="warning")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
