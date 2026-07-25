"""Tests for the flow lifecycle ownership guardrail.

A checker that cannot fail is worse than no checker, so these assert both arms:
the real tree is clean, and planted violations are actually caught.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_flow_lifecycle_ownership.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_flow_lifecycle_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_module()


def test_repo_surfaces_are_clean() -> None:
    assert checker.find_violations() == []
    assert checker.main([]) == 0


def test_rotate_corrupt_db_is_banned_anywhere_in_surfaces(tmp_path: Path) -> None:
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "thing.py").write_text(
        "def wipe(db, exc):\n    rotate_corrupt_flow_db(db, str(exc))\n"
    )

    violations = checker.find_violations(tmp_path)

    assert [v.symbol for v in violations] == ["rotate_corrupt_flow_db"]


def test_flow_controller_construction_is_banned_under_web(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "routes.py").write_text(
        "from x import FlowController\n"
        "def build():\n"
        "    return FlowController(definition=1, db_path=2, artifacts_root=3)\n"
    )

    violations = checker.find_violations(tmp_path)

    assert [v.symbol for v in violations] == ["FlowController"]
    assert violations[0].line == 3


def test_flow_controller_construction_is_allowed_outside_web(tmp_path: Path) -> None:
    # `car flow worker` hosts a flow run; constructing a controller there is a
    # composition root, not a surface reaching past its layer.
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "flow.py").write_text(
        "from x import FlowController\n"
        "def worker():\n"
        "    return FlowController(definition=1, db_path=2, artifacts_root=3)\n"
    )

    assert checker.find_violations(tmp_path) == []


def test_injected_dependency_is_not_a_violation(tmp_path: Path) -> None:
    # Receiving the callable as a parameter is the pattern we want, not a breach.
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "injected.py").write_text(
        "def run(rotate_corrupt_flow_db):\n"
        "    return rotate_corrupt_flow_db(1, '2')\n"
    )

    assert checker.find_violations(tmp_path) == []


def test_unparseable_file_does_not_silently_pass(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken.py").write_text("def (:\n")

    checker.find_violations(tmp_path)

    assert "could not parse" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["nonexistent-root"])
def test_missing_root_is_a_setup_error(tmp_path: Path, missing: str) -> None:
    assert checker.main(["--root", str(tmp_path / missing)]) == 2
