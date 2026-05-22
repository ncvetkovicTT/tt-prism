from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tt_prism.models import Diagram


def load(path: str | Path) -> Diagram:
    data = yaml.safe_load(Path(path).read_text())
    if data is None:
        data = {}
    return Diagram.model_validate(data)


def loads(text: str) -> Diagram:
    data = yaml.safe_load(text) or {}
    return Diagram.model_validate(data)


def dump(diagram: Diagram, path: str | Path) -> None:
    Path(path).write_text(dumps(diagram))


def dumps(diagram: Diagram) -> str:
    data: dict[str, Any] = diagram.model_dump(by_alias=True, exclude_none=False)
    return yaml.safe_dump(data, sort_keys=False, width=120)
