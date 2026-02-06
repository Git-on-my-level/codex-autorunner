from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.telegram.adapter import (
    FlowCallback,
    FlowRunCallback,
    TelegramCallbackQuery,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands
from codex_autorunner.integrations.telegram.handlers.selections import (
    TelegramSelectionHandlers,
)


class _TopicStoreStub:
    def __init__(self, repo_root: Path | None) -> None:
        self._record = (
            SimpleNamespace(workspace_path=str(repo_root)) if repo_root else None
        )

    async def get_topic(self, _key: str) -> SimpleNamespace | None:
        return self._record


class _ControllerStub:
    def __init__(self) -> None:
        self.resume_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def resume_flow(self, run_id: str) -> SimpleNamespace:
        self.resume_calls.append(run_id)
        return SimpleNamespace(id=run_id)

    async def stop_flow(self, run_id: str) -> SimpleNamespace:
        self.stop_calls.append(run_id)
        return SimpleNamespace(id=run_id, status=FlowRunStatus.STOPPED)


class _FlowCallbackHandler(FlowCommands):
    def __init__(
        self,
        repo_root: Path | None,
        *,
        resolved_repo_root: Path | None = None,
    ) -> None:
        self._repo_root = resolved_repo_root
        self._store = _TopicStoreStub(repo_root)
        self.answers: list[str] = []
        self.rendered: list[tuple[Path, str | None, str | None]] = []
        self.stopped_workers: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.answers.append(text)

    async def _render_flow_status_callback(
        self,
        _callback: TelegramCallbackQuery,
        repo_root: Path,
        run_id_raw: str | None,
        *,
        repo_id: str | None = None,
    ) -> None:
        self.rendered.append((repo_root, run_id_raw, repo_id))

    def _stop_flow_worker(self, _repo_root: Path, run_id: str) -> None:
        self.stopped_workers.append(run_id)

    def _resolve_workspace(self, arg: str) -> tuple[str, str] | None:
        if self._repo_root and arg == "car-wt-3":
            return str(self._repo_root), "car-wt-3"
        return None


def _init_store(repo_root: Path) -> FlowStore:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    return store


def _create_run(store: FlowStore, run_id: str, status: FlowRunStatus) -> None:
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, status)


def _callback() -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        update_id=1,
        callback_id="cb1",
        from_user_id=2,
        data=None,
        message_id=3,
        chat_id=10,
        thread_id=11,
    )


@pytest.mark.anyio
async def test_flow_callback_resume_latest_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.PAUSED)
    store.close()

    controller = _ControllerStub()
    spawned: list[str] = []
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(
        flows_module, "_spawn_flow_worker", lambda _root, run: spawned.append(run)
    )

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="resume"))

    assert controller.resume_calls == [run_id]
    assert spawned == [run_id]
    assert "Resumed." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_stop_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.RUNNING)
    store.close()

    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="stop"))

    assert controller.stop_calls == [run_id]
    assert handler.stopped_workers == [run_id]
    assert "Stopped." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_recover_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.RUNNING)
    store.close()

    recovered: list[str] = []

    def _reconcile(_repo_root: Path, record: object, _store: FlowStore):
        recovered.append(record.id)
        return record, True, False

    monkeypatch.setattr(flows_module, "reconcile_flow_run", _reconcile)

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="recover"))

    assert recovered == [run_id]
    assert "Recovered." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_refresh_uses_repo_id_when_topic_unbound(
    tmp_path: Path,
) -> None:
    handler = _FlowCallbackHandler(None, resolved_repo_root=tmp_path)
    await handler._handle_flow_callback(
        _callback(),
        FlowCallback(action="refresh", run_id="run-1", repo_id="car-wt-3"),
    )

    assert "Refreshing..." in handler.answers
    assert handler.rendered == [(tmp_path, "run-1", "car-wt-3")]


class _SelectionFlowRunHandler(TelegramSelectionHandlers):
    def __init__(self) -> None:
        self._flow_run_options = {
            "topic": SimpleNamespace(
                items=[("run-1", "Run 1")], repo_id="car-wt-3", page=0
            )
        }
        self.forwarded: list[FlowCallback] = []
        self.answers: list[str] = []

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.answers.append(text)

    async def _handle_flow_callback(
        self, _callback: TelegramCallbackQuery, parsed: FlowCallback
    ) -> None:
        self.forwarded.append(parsed)


@pytest.mark.anyio
async def test_flow_run_callback_forwards_repo_id() -> None:
    handler = _SelectionFlowRunHandler()
    await handler._handle_flow_run_callback(
        "topic", _callback(), FlowRunCallback(run_id="run-1")
    )

    assert not handler.answers
    assert handler.forwarded == [
        FlowCallback(action="status", run_id="run-1", repo_id="car-wt-3")
    ]
