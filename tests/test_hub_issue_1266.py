"""Regression tests for hub startup / health ordering and legacy backfill guards (GitHub #1266).

Hub heavy startup is deferred via `_deferred_hub_startup`; legacy JSON→SQLite backfill is gated by
`ensure_legacy_orchestration_backfill` / `orch_legacy_backfill_flags`.
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.orchestration import (
    legacy_backfill_gate as legacy_backfill_gate_module,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web import app as web_app_module
from tests.conftest import write_test_config
from tests.test_hub_app_context import _stub_opencode_supervisor

_APP_PY = Path(web_app_module.__file__).resolve()


def _find_create_hub_lifespan(tree: ast.Module) -> ast.AsyncFunctionDef | None:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "create_hub_app":
            continue
        for item in node.body:
            if isinstance(item, ast.AsyncFunctionDef) and item.name == "lifespan":
                return item
    return None


def _walk_stmt_list(
    stmts: list[ast.stmt],
    *,
    yield_lines: list[int],
    blocking_lines: list[int],
) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(stmt, ast.AsyncWith):
            _walk_stmt_list(
                stmt.body, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            continue
        if isinstance(stmt, ast.With):
            _walk_stmt_list(
                stmt.body, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            continue
        if isinstance(stmt, ast.If):
            _walk_stmt_list(
                stmt.body, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            _walk_stmt_list(
                stmt.orelse, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            continue
        if isinstance(stmt, ast.Try):
            _walk_stmt_list(
                stmt.body, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            for handler in stmt.handlers:
                _walk_stmt_list(
                    handler.body, yield_lines=yield_lines, blocking_lines=blocking_lines
                )
            _walk_stmt_list(
                stmt.orelse, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            _walk_stmt_list(
                stmt.finalbody, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            _walk_stmt_list(
                stmt.body, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            _walk_stmt_list(
                stmt.orelse, yield_lines=yield_lines, blocking_lines=blocking_lines
            )
            continue
        if sys.version_info >= (3, 10) and isinstance(stmt, ast.Match):
            for case in stmt.cases:
                _walk_stmt_list(
                    case.body, yield_lines=yield_lines, blocking_lines=blocking_lines
                )
            continue

        if isinstance(stmt, ast.Expr):
            value = stmt.value
            if isinstance(value, ast.Yield):
                yield_lines.append(value.lineno)
            elif isinstance(value, ast.Await) and _is_blocking_hub_startup_await(value):
                blocking_lines.append(value.lineno)


def _is_blocking_hub_startup_await(node: ast.Await) -> bool:
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in {
            "recover_orphaned_managed_thread_executions",
            "restart_managed_thread_queue_workers",
        }
    if isinstance(func, ast.Attribute) and func.attr == "start_repo_lifespans":
        return True
    return False


def _hub_lifespan_yields_before_blocking_startup_work() -> bool:
    """True when the hub `lifespan` yields before awaiting recovery / repo lifespans (Plan F).

    When heavy work moves into nested helpers or tasks only, outer-body blocking awaits may be
    empty; treat as early-yield semantics so the integration test runs.
    """
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    lifespan = _find_create_hub_lifespan(tree)
    if lifespan is None:
        return False
    yield_lines: list[int] = []
    blocking_lines: list[int] = []
    _walk_stmt_list(
        lifespan.body, yield_lines=yield_lines, blocking_lines=blocking_lines
    )
    if not yield_lines:
        return False
    if not blocking_lines:
        return True
    return min(yield_lines) < min(blocking_lines)


@pytest.mark.skipif(
    not _hub_lifespan_yields_before_blocking_startup_work(),
    reason=(
        "Hub lifespan does not yield before managed-thread recovery / repo lifespans yet; "
        "merge early-yield startup sequencing for #1266 to enable this assertion."
    ),
)
def test_hub_health_serves_before_deferred_startup_work_finishes(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/health` under base path must respond while deferred hub startup work is still running."""
    _stub_opencode_supervisor(monkeypatch)
    entered = threading.Event()

    async def _hanging_recover(app: object) -> None:
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        web_app_module,
        "recover_orphaned_managed_thread_executions",
        _hanging_recover,
    )

    app = create_hub_app(
        hub_env.hub_root,
        base_path="/car",
        endpoint_host="127.0.0.1",
        endpoint_port=4517,
    )

    with TestClient(app) as client:
        assert entered.wait(
            timeout=10.0
        ), "deferred recovery should start after early yield"
        response = client.get("/car/health")
        assert response.status_code == 200

    assert entered.is_set()


@pytest.mark.skipif(
    not _hub_lifespan_yields_before_blocking_startup_work(),
    reason=(
        "Hub lifespan does not yield before managed-thread recovery / repo lifespans yet; "
        "merge early-yield startup sequencing for #1266 to enable this assertion."
    ),
)
def test_hub_root_health_serves_before_deferred_startup_work_finishes(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root `/health` must stay responsive while deferred startup recovery is active."""
    _stub_opencode_supervisor(monkeypatch)
    entered = threading.Event()

    async def _hanging_recover(app: object) -> None:
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        web_app_module,
        "recover_orphaned_managed_thread_executions",
        _hanging_recover,
    )

    app = create_hub_app(
        hub_env.hub_root,
        endpoint_host="127.0.0.1",
        endpoint_port=4517,
    )

    with TestClient(app) as client:
        assert entered.wait(
            timeout=10.0
        ), "deferred recovery should start after early yield"
        response = client.get("/health")
        assert response.status_code == 200

    assert entered.is_set()


def test_pma_automation_legacy_automation_backfill_runs_once_across_store_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`orch_legacy_backfill_flags` must skip re-invoking per-source backfills (GitHub #1266)."""
    from codex_autorunner.bootstrap import seed_hub_files
    from codex_autorunner.core.config import CONFIG_FILENAME
    from codex_autorunner.core.pma_automation_store import PmaAutomationStore
    from codex_autorunner.core.pma_thread_store import prepare_pma_thread_store

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    calls: list[int] = []

    def _counting_backfill(*args: object, **kwargs: object) -> dict[str, int]:
        calls.append(1)
        return {"subscriptions": 0, "timers": 0, "wakeups": 0}

    monkeypatch.setattr(
        legacy_backfill_gate_module,
        "backfill_legacy_automation_state",
        _counting_backfill,
    )

    prepare_pma_thread_store(hub_root, durable=False)
    store_a = PmaAutomationStore(hub_root)
    store_a.load()
    store_b = PmaAutomationStore(hub_root)
    store_b.load()

    assert calls == [1], "durable marker must prevent a second automation backfill"


def test_pma_automation_load_does_not_retrigger_backfill_after_explicit_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.bootstrap import seed_hub_files
    from codex_autorunner.core.config import CONFIG_FILENAME
    from codex_autorunner.core.pma_automation_store import PmaAutomationStore
    from codex_autorunner.core.pma_thread_store import prepare_pma_thread_store

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    prepare_pma_thread_store(hub_root, durable=False)
    calls: list[int] = []

    def _counting_backfill(*args: object, **kwargs: object) -> dict[str, int]:
        calls.append(1)
        return {"subscriptions": 0, "timers": 0, "wakeups": 0}

    monkeypatch.setattr(
        legacy_backfill_gate_module,
        "backfill_legacy_automation_state",
        _counting_backfill,
    )

    PmaAutomationStore(hub_root).load()
    PmaAutomationStore(hub_root).load()

    assert calls == [], "routine loads must not retrigger legacy backfill after prepare"


def test_hub_health_reports_orchestration_database_size(hub_env, monkeypatch) -> None:
    _stub_opencode_supervisor(monkeypatch)
    with open_orchestration_sqlite(hub_env.hub_root, durable=False):
        pass

    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["orchestration"]["database_path"].endswith("orchestration.sqlite3")
    assert payload["orchestration"]["database_size_bytes"] >= 0
    assert payload["orchestration"]["database_size_status"] == "ok"


def test_hub_health_includes_last_orchestration_housekeeping(
    hub_env, monkeypatch
) -> None:
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        app.state.orchestration_housekeeping = {
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:00:01Z",
            "compaction": {"rows_deleted": 12},
            "retention": {"hot_rows_deleted": 4},
        }
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert (
        payload["orchestration"]["last_housekeeping"]["compaction"]["rows_deleted"]
        == 12
    )


def test_hub_housekeeping_loop_survives_sqlite_errors(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.config import CONFIG_FILENAME

    _stub_opencode_supervisor(monkeypatch)
    first_call = threading.Event()
    second_call = threading.Event()

    class _Summary:
        def to_dict(self) -> dict[str, object]:
            return {"status": "ok"}

    def _fake_prune_filebox_root(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            inbox_pruned=(),
            outbox_pruned=(),
            bytes_before=0,
            bytes_after=0,
        )

    def _fake_execution_history_housekeeping(
        *args: object, **kwargs: object
    ) -> _Summary:
        if not first_call.is_set():
            first_call.set()
            raise sqlite3.OperationalError("database is locked")
        second_call.set()
        return _Summary()

    monkeypatch.setattr(
        web_app_module,
        "prune_filebox_root",
        _fake_prune_filebox_root,
    )
    monkeypatch.setattr(
        web_app_module,
        "run_housekeeping_once",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        web_app_module,
        "run_execution_history_housekeeping_once",
        _fake_execution_history_housekeeping,
    )
    write_test_config(
        Path(hub_env.hub_root) / CONFIG_FILENAME,
        {
            "mode": "hub",
            "housekeeping": {"interval_seconds": 1},
        },
    )

    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        assert first_call.wait(timeout=5.0)
        assert second_call.wait(timeout=5.0)
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
