from __future__ import annotations

import pytest

from codex_autorunner.core import optional_dependencies
from codex_autorunner.core.config import ConfigError


def test_missing_optional_dependencies_reports_missing(monkeypatch) -> None:
    def fake_find_spec(name: str) -> object | None:
        return None if name == "missing" else object()

    monkeypatch.setattr(
        optional_dependencies.importlib.util, "find_spec", fake_find_spec
    )
    missing = optional_dependencies.missing_optional_dependencies(
        [("missing", "Missing Dep"), ("ok", "Ok Dep")]
    )
    assert missing == ["Missing Dep"]


def test_require_optional_dependencies_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        optional_dependencies.importlib.util,
        "find_spec",
        lambda _name: None,
    )
    with pytest.raises(ConfigError) as exc:
        optional_dependencies.require_optional_dependencies(
            feature="Voice",
            deps=[("missing", "Missing Dep")],
            extra="voice",
        )
    assert "pip install codex-autorunner[voice]" in str(exc.value)


def test_missing_optional_dependencies_accepts_alternatives(monkeypatch) -> None:
    def fake_find_spec(name: str) -> object | None:
        return object() if name == "alternate" else None

    monkeypatch.setattr(
        optional_dependencies.importlib.util,
        "find_spec",
        fake_find_spec,
    )
    missing = optional_dependencies.missing_optional_dependencies(
        [(("missing", "alternate"), "Alt Dep")]
    )
    assert missing == []
