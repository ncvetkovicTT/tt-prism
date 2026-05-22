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


WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


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
        return JSONResponse(
            {
                "path": str(diagram_path),
                "diagram": d.model_dump(by_alias=True, exclude_none=False),
            }
        )

    @app.put("/api/diagram")
    def put_diagram(env_: DiagramEnvelope) -> JSONResponse:
        try:
            d = Diagram.model_validate(env_.diagram)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        storage.dump(d, diagram_path)
        return JSONResponse({"ok": True, "path": str(diagram_path)})

    @app.get("/api/render.svg")
    def render() -> Response:
        d = storage.load(diagram_path)
        return Response(content=render_svg(d), media_type="image/svg+xml")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        icon = STATIC_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(str(icon), media_type="image/svg+xml")
        return Response(status_code=204)

    return app
