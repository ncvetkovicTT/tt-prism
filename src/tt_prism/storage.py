from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tt_prism.models import Diagram


class YamlSyntaxError(ValueError):
    """The file is not well-formed YAML (a parse error, before any schema check)."""


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        # Surface the location/context YAML gives us, but as a clean ValueError
        # (not a raw traceback) so callers can show a friendly message.
        raise YamlSyntaxError(str(e)) from e


def load(path: str | Path) -> Diagram:
    data = _parse_yaml(Path(path).read_text())
    if data is None:
        data = {}
    return Diagram.model_validate(data)


def loads(text: str) -> Diagram:
    data = _parse_yaml(text) or {}
    return Diagram.model_validate(data)


def dump(diagram: Diagram, path: str | Path) -> None:
    Path(path).write_text(dumps(diagram))


def dumps(diagram: Diagram) -> str:
    data: dict[str, Any] = diagram.model_dump(by_alias=True, exclude_none=False)
    return yaml.safe_dump(data, sort_keys=False, width=120)
