from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from tt_prism import storage
from tt_prism.models import Diagram
from tt_prism.renderer.svg import render_svg
from tt_prism.schedule import ScheduleError, to_render_diagram


WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


def _view_of(diagram: Diagram) -> Diagram:
    """The diagram to *render*. Op-authored diagrams have no absolute starts, so
    we solve them into a flat scheduled diagram; flat diagrams render as-is."""
    if diagram.ops:
        return to_render_diagram(diagram)
    return diagram


class DiagramEnvelope(BaseModel):
    diagram: dict


def build_app(diagram_path: Path) -> FastAPI:
    diagram_path = Path(diagram_path).resolve()
    app = FastAPI(title="tt-prism editor")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = env.get_template("editor.html").render(
            diagram_path=str(diagram_path)
        )
        return HTMLResponse(html)

    @app.get("/api/diagram")
    def get_diagram() -> JSONResponse:
        d = storage.load(diagram_path)
        # `source` carries the authored diagram (incl. ops); `view` is what the
        # client renders. For flat diagrams the two are identical.
        return JSONResponse(
            {
                "path": str(diagram_path),
                "is_ops": bool(d.ops),
                "source": d.model_dump(by_alias=True, exclude_none=False),
                "view": _view_of(d).model_dump(by_alias=True, exclude_none=False),
            }
        )

    @app.put("/api/diagram")
    def put_diagram(env_: DiagramEnvelope) -> JSONResponse:
        try:
            d = Diagram.model_validate(env_.diagram)
            if d.ops:
                to_render_diagram(d)  # ensure it still schedules before saving
        except ScheduleError as e:
            raise HTTPException(status_code=400, detail=f"unschedulable: {e}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        storage.dump(d, diagram_path)
        return JSONResponse({"ok": True, "path": str(diagram_path)})

    @app.post("/api/solve")
    def solve_diagram(env_: DiagramEnvelope) -> JSONResponse:
        """Re-solve an in-memory (unsaved) diagram and return the scheduled view.
        Lets the client edit a block's duration/bank and see the reflow without
        writing to disk."""
        try:
            d = Diagram.model_validate(env_.diagram)
            view = _view_of(d)
        except ScheduleError as e:
            raise HTTPException(status_code=400, detail=f"unschedulable: {e}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(
            {"is_ops": bool(d.ops), "view": view.model_dump(by_alias=True, exclude_none=False)}
        )

    @app.get("/api/render.svg")
    def render() -> Response:
        d = storage.load(diagram_path)
        return Response(content=render_svg(_view_of(d)), media_type="image/svg+xml")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        icon = STATIC_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(str(icon), media_type="image/svg+xml")
        return Response(status_code=204)

    return app
