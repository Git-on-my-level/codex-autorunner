"""Tests for flow controller provisioning and corrupt-store recovery.

These pin the behavior that moved out of ``surfaces/web/routes/flow_routes/``:
an HTTP route module no longer decides when a user's durable flow database is
replaced.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from codex_autorunner.flows.controller_provider import (
    FlowControllerProvider,
    FlowControllerUnavailable,
    is_probably_corrupt_flow_db_error,
    recover_flow_store,
    rotate_corrupt_flow_db,
)


class _FakeController:
    """Stands in for FlowController; records initialize/shutdown calls."""

    def __init__(self, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self.init_calls = 0
        self.shutdown_calls = 0
        self._fail_times = fail_times
        self._error = error or sqlite3.DatabaseError("file is not a database")

    def initialize(self) -> None:
        self.init_calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _provider(tmp_path: Path, controllers: list[_FakeController]) -> tuple:
    """Provider whose _build hands out the supplied controllers in order."""
    built: list[_FakeController] = []
    cache: dict = {}

    provider = FlowControllerProvider(
        definition_factory=lambda root, ft: object(),
        paths_factory=lambda root: (tmp_path / "flows.db", tmp_path / "artifacts"),
        cache=cache,
        lock=threading.Lock(),
    )

    queue = list(controllers)

    def _build(repo_root: Path, flow_type: str) -> _FakeController:
        controller = queue.pop(0)
        built.append(controller)
        return controller

    provider._build = _build  # type: ignore[assignment]
    return provider, cache, built


def test_corrupt_db_classification_is_conservative(tmp_path: Path) -> None:
    db = tmp_path / "flows.db"
    assert is_probably_corrupt_flow_db_error(
        sqlite3.DatabaseError("file is not a database"), db
    )
    assert is_probably_corrupt_flow_db_error(
        sqlite3.DatabaseError("database disk image is malformed"), db
    )
    # A non-sqlite error must never trigger rotation of a user's database.
    assert not is_probably_corrupt_flow_db_error(ValueError("boom"), db)
    assert not is_probably_corrupt_flow_db_error(sqlite3.DatabaseError("locked"), db)


def test_rotate_corrupt_db_preserves_original_and_writes_notice(
    tmp_path: Path,
) -> None:
    db = tmp_path / "flows.db"
    db.write_bytes(b"not a sqlite file")

    backup = rotate_corrupt_flow_db(db, "file is not a database")

    assert backup is not None and backup.exists()
    assert backup.read_bytes() == b"not a sqlite file"
    assert not db.exists()
    notice = tmp_path / "flows.db.corrupt.json"
    assert notice.exists()
    assert "corrupt" in notice.read_text()


def test_recover_flow_store_declines_non_corruption(tmp_path: Path) -> None:
    db = tmp_path / "flows.db"
    db.write_bytes(b"payload")

    assert recover_flow_store(db, ValueError("unrelated")) is False
    # The database must be left completely alone.
    assert db.read_bytes() == b"payload"


def test_recover_flow_store_rotates_and_evicts(tmp_path: Path) -> None:
    db = tmp_path / "flows.db"
    db.write_bytes(b"not a sqlite file")
    evicted: list[bool] = []

    recovered = recover_flow_store(
        db,
        sqlite3.DatabaseError("file is not a database"),
        on_evict=lambda: evicted.append(True),
    )

    assert recovered is True
    assert evicted == [True], "stale controller must be dropped after rotation"
    assert db.exists(), "a fresh store should have been initialized in its place"


def test_provider_caches_controller_across_calls(tmp_path: Path) -> None:
    controller = _FakeController()
    provider, cache, built = _provider(tmp_path, [controller])

    first = provider.get(tmp_path, "ticket_flow")
    second = provider.get(tmp_path, "ticket_flow")

    assert first is second
    assert len(built) == 1, "second call must reuse the cached controller"
    assert len(cache) == 1


def test_provider_raises_typed_error_when_unrecoverable(tmp_path: Path) -> None:
    # A non-corruption failure is not recoverable, so it must surface as a
    # transport-agnostic domain error rather than an HTTP exception.
    failing = _FakeController(fail_times=1, error=RuntimeError("no store"))
    provider, _cache, _built = _provider(tmp_path, [failing])

    with pytest.raises(FlowControllerUnavailable) as excinfo:
        provider.get(tmp_path, "ticket_flow")

    assert excinfo.value.flow_type == "ticket_flow"
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_provider_retries_once_after_corrupt_store_recovery(tmp_path: Path) -> None:
    (tmp_path / "flows.db").write_bytes(b"not a sqlite file")
    first = _FakeController(fail_times=1)  # fails with a corruption error
    second = _FakeController()
    provider, _cache, built = _provider(tmp_path, [first, second])

    controller = provider.get(tmp_path, "ticket_flow")

    assert controller is second
    assert len(built) == 2, "recovery should rebuild against the fresh store"


def test_provider_evict_shuts_down_and_clears_cache(tmp_path: Path) -> None:
    controller = _FakeController()
    provider, cache, _built = _provider(tmp_path, [controller])
    provider.get(tmp_path, "ticket_flow")

    provider.evict(tmp_path, "ticket_flow")

    assert controller.shutdown_calls == 1
    assert cache == {}


def test_provider_shares_externally_owned_cache(tmp_path: Path) -> None:
    # Route modules address state.controller_cache directly; the provider must
    # write into that same mapping rather than a private one.
    controller = _FakeController()
    provider, cache, _built = _provider(tmp_path, [controller])

    provider.get(tmp_path, "ticket_flow")

    assert list(cache.values()) == [controller]
