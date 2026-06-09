"""Tests for the user-facing YAML checker.

The checker is ``tt-prism validate <file>`` (cli.py). Its job: parse the YAML,
validate the schema + references, and (for op-diagrams) confirm it schedules —
emitting a clear, non-traceback message + a non-zero exit for each common
failure mode.

Typer is an optional dependency in some environments. The underlying
``storage.load`` path (the actual checker logic) is always exercised; the
Typer-command tests are skipped if Typer isn't importable.
"""

from __future__ import annotations

import pytest

from tt_prism import storage
from tt_prism.storage import YamlSyntaxError

# A schema-valid op diagram for the happy path.
_GOOD = """
title: ok
grid_clocks: 32
lanes:
  - {id: t0, name: T0, order: 0}
  - {id: t1, name: T1, order: 1}
resources:
  - {id: unpack, name: U}
  - {id: pack, name: P}
ops:
  - id: a
    name: a
    blocks:
      - {id: a_u, lane_id: t0, resource_id: unpack, duration_clocks: 10}
      - {id: a_p, lane_id: t1, resource_id: pack, duration_clocks: 10}
"""

# ---- the failure modes, as YAML strings ----

_MALFORMED = "lanes: [unterminated\n  oops: : :"

_UNKNOWN_LANE = """
title: t
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
work_items: [{id: w1, lane_id: nope, resource_id: u}]
"""

_DUP_ID = """
title: t
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
work_items:
  - {id: w1, lane_id: t0, resource_id: u}
  - {id: w1, lane_id: t0, resource_id: u}
"""

_DEP_UNKNOWN = """
title: t
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
work_items: [{id: w1, lane_id: t0, resource_id: u}]
dependencies: [{from: w1, to: ghost}]
"""

_DEST_OOR = """
title: t
dest_banks: 2
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
ops:
  - id: a
    blocks: [{id: a_u, lane_id: t0, resource_id: u, dest_bank: 5}]
"""

_MISSING_CORE_ID = """
title: t
cores: [{id: c0, x: 0, y: 0}, {id: c1, x: 1, y: 0}]
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
ops:
  - id: a
    blocks: [{id: a_u, lane_id: t0, resource_id: u}]
"""

_UNKNOWN_CORE = """
title: t
cores: [{id: c0, x: 0, y: 0}, {id: c1, x: 1, y: 0}]
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
ops:
  - id: a
    core_id: nope
    blocks: [{id: a_u, lane_id: t0, resource_id: u}]
"""


# ==========================================================================
# Underlying checker path (always runs — no Typer needed).
# storage.loads raises YamlSyntaxError for parse errors, ValidationError for
# schema/reference problems.  cli.validate maps these to friendly messages.
# ==========================================================================
from pydantic import ValidationError  # noqa: E402


def test_malformed_yaml_raises_clean_syntax_error():
    with pytest.raises(YamlSyntaxError):
        storage.loads(_MALFORMED)


@pytest.mark.parametrize(
    "text, needle",
    [
        (_UNKNOWN_LANE, "unknown lane"),
        (_DUP_ID, "duplicate work item id"),
        (_DEP_UNKNOWN, "dependency references unknown id"),
        (_DEST_OOR, "dest_bank"),
        (_MISSING_CORE_ID, "must set core_id"),
        (_UNKNOWN_CORE, "unknown core"),
    ],
)
def test_schema_invalid_raises_validation_error(text, needle):
    with pytest.raises(ValidationError) as ei:
        storage.loads(text)
    assert needle in str(ei.value)


def test_good_yaml_loads_and_schedules():
    d = storage.loads(_GOOD)
    from tt_prism.schedule import solve

    sched = solve(d)
    assert sched.total_clocks() > 0


# ==========================================================================
# CLI command tests (Typer optional). Invoke the Typer command function via
# CliRunner so we exercise the actual user-facing surface, including exit codes.
# These are skipped (per-test) if Typer isn't importable, so the underlying
# checker tests above still run.
# ==========================================================================
try:
    from typer.testing import CliRunner

    from tt_prism.cli import app

    runner = CliRunner()
    _HAVE_TYPER = True
except ImportError:
    _HAVE_TYPER = False

requires_typer = pytest.mark.skipif(not _HAVE_TYPER, reason="typer not installed")


def _write(tmp_path, text):
    p = tmp_path / "diagram.yaml"
    p.write_text(text)
    return str(p)


@requires_typer
def test_cli_validate_ok(tmp_path):
    res = runner.invoke(app, ["validate", _write(tmp_path, _GOOD)])
    assert res.exit_code == 0
    assert "ok" in res.stdout


@requires_typer
def test_cli_validate_malformed_yaml_is_friendly(tmp_path):
    res = runner.invoke(app, ["validate", _write(tmp_path, _MALFORMED)])
    assert res.exit_code == 1
    assert "invalid YAML" in res.stdout
    # no leaked Python traceback
    assert "Traceback" not in res.stdout


@pytest.mark.parametrize(
    "text, needle",
    [
        (_UNKNOWN_LANE, "unknown lane"),
        (_DUP_ID, "duplicate work item id"),
        (_DEP_UNKNOWN, "unknown id"),
        (_DEST_OOR, "dest_bank"),
        (_MISSING_CORE_ID, "core_id"),
        (_UNKNOWN_CORE, "unknown core"),
    ],
)
@requires_typer
def test_cli_validate_schema_errors_are_friendly(tmp_path, text, needle):
    res = runner.invoke(app, ["validate", _write(tmp_path, text)])
    assert res.exit_code == 1
    assert "invalid schema" in res.stdout
    assert needle in res.stdout
    assert "Traceback" not in res.stdout


@requires_typer
def test_cli_validate_unschedulable(tmp_path):
    cyclic = """
title: t
lanes: [{id: t0, name: T0}]
resources: [{id: u, name: U}]
ops:
  - id: a
    blocks: [{id: a_b, lane_id: t0, resource_id: u, duration_clocks: 5}]
  - id: b
    blocks: [{id: b_b, lane_id: t0, resource_id: u, duration_clocks: 5}]
dependencies:
  - {from: a, to: b}
  - {from: b, to: a}
"""
    res = runner.invoke(app, ["validate", _write(tmp_path, cyclic)])
    assert res.exit_code == 1
    assert "unschedulable" in res.stdout
    assert "Traceback" not in res.stdout
