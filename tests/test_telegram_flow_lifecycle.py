from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


class _NowSeq:
    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> str:
        self._counter += 1
        return f"2026-01-30T00:00:0{self._counter}Z"


class _ControllerStub:
    def __init__(self) -> None:
        self.resume_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def resume_flow(self, run_id: str, *, force: bool = False) -> SimpleNamespace:
        self.resume_calls.append(run_id)
        return SimpleNamespace(id=run_id)

    async def stop_flow(self, run_id: str) -> SimpleNamespace:
        self.stop_calls.append(run_id)
        return SimpleNamespace(id=run_id, status=FlowRunStatus.STOPPED)


class _FlowLifecycleHandler(FlowCommands):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.stopped_workers: list[str] = []

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup, parse_mode)
        self.sent.append(text)

    def _stop_flow_worker(self, _repo_root: Path, run_id: str) -> None:
        self.stopped_workers.append(run_id)


def _message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=999,
        thread_id=123,
        from_user_id=1,
        text="/flow",
        date=None,
        is_topic_message=True,
    )


def _init_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlowStore:
    from codex_autorunner.core.flows import store as store_module

    monkeypatch.setattr(store_module, "now_iso", _NowSeq())
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    return store


def _create_run(store: FlowStore, run_id: str, status: FlowRunStatus) -> None:
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, status)


@pytest.mark.anyio
async def test_flow_resume_defaults_latest_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path, monkeypatch)
    run_old = str(uuid.uuid4())
    run_new = str(uuid.uuid4())
    _create_run(store, run_old, FlowRunStatus.PAUSED)
    _create_run(store, run_new, FlowRunStatus.PAUSED)
    store.close()

    controller = _ControllerStub()
    spawned: list[str] = []
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(
        flows_module, "_spawn_flow_worker", lambda _root, run: spawned.append(run)
    )

    handler = _FlowLifecycleHandler()
    await handler._handle_flow_resume(_message(), tmp_path, argv=[])

    assert controller.resume_calls == [run_new]
    assert spawned == [run_new]
    assert any(f"Resumed run `{run_new}`" in text for text in handler.sent)


@pytest.mark.anyio
async def test_flow_stop_defaults_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path, monkeypatch)
    run_running = str(uuid.uuid4())
    run_completed = str(uuid.uuid4())
    _create_run(store, run_running, FlowRunStatus.RUNNING)
    _create_run(store, run_completed, FlowRunStatus.COMPLETED)
    store.close()

    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )

    handler = _FlowLifecycleHandler()
    await handler._handle_flow_stop(_message(), tmp_path, argv=[])

    assert controller.stop_calls == [run_running]
    assert any(f"Stopped run `{run_running}`" in text for text in handler.sent)


@pytest.mark.anyio
async def test_flow_recover_defaults_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path, monkeypatch)
    run_running = str(uuid.uuid4())
    run_completed = str(uuid.uuid4())
    _create_run(store, run_running, FlowRunStatus.RUNNING)
    _create_run(store, run_completed, FlowRunStatus.COMPLETED)
    store.close()

    recovered: list[str] = []

    def _reconcile(repo_root: Path, record: object, _store: FlowStore):
        _ = repo_root
        recovered.append(record.id)
        return record, True, False

    monkeypatch.setattr(flows_module, "reconcile_flow_run", _reconcile)

    handler = _FlowLifecycleHandler()
    await handler._handle_flow_recover(_message(), tmp_path, argv=[])

    assert recovered == [run_running]
    assert any("Recovered" in text for text in handler.sent)


@pytest.mark.anyio
async def test_flow_archive_defaults_latest_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path, monkeypatch)
    run_terminal = str(uuid.uuid4())
    run_paused = str(uuid.uuid4())
    _create_run(store, run_terminal, FlowRunStatus.COMPLETED)
    _create_run(store, run_paused, FlowRunStatus.PAUSED)
    store.close()

    tickets_dir = tmp_path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text("ticket", encoding="utf-8")

    run_dir = tmp_path / ".codex-autorunner" / "runs" / run_paused
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "DISPATCH.md").write_text("dispatch", encoding="utf-8")

    handler = _FlowLifecycleHandler()
    await handler._handle_flow_archive(_message(), tmp_path, argv=[])

    archive_dir = (
        tmp_path / ".codex-autorunner" / "flows" / run_paused / "archived_tickets"
    )
    assert (archive_dir / "TICKET-001.md").exists()
    archived_runs = (
        tmp_path / ".codex-autorunner" / "flows" / run_paused / "archived_runs"
    )
    assert archived_runs.exists()
    assert handler.stopped_workers == [run_paused]

    store = FlowStore(tmp_path / ".codex-autorunner" / "flows.db")
    store.initialize()
    try:
        assert store.get_flow_run(run_paused) is None
    finally:
        store.close()
