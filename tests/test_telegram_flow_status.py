from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import (
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
)
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


def _health(tmp_path: Path, status: str = "alive") -> FlowWorkerHealth:
    return FlowWorkerHealth(
        status=status,
        pid=123,
        cmdline=[],
        artifact_path=tmp_path / "artifacts" / "worker.json",
        message=None,
    )


def _record(status: FlowRunStatus, *, state: dict | None = None) -> FlowRunRecord:
    return FlowRunRecord(
        id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        current_step=None,
        stop_requested=False,
        created_at="2026-01-30T00:00:00Z",
        started_at=None,
        finished_at=None,
        error_message=None,
        metadata={},
    )


def test_flow_status_includes_effective_current_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = str(uuid.uuid4())
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
    store.create_event("e1", run_id, FlowEventType.STEP_STARTED, data={})
    store.create_event(
        "e2", run_id, FlowEventType.STEP_PROGRESS, data={"current_ticket": "TICKET-002"}
    )
    record = store.get_flow_run(run_id)
    assert record is not None

    monkeypatch.setattr(
        flows_module,
        "check_worker_health",
        lambda _root, _run_id: _health(tmp_path),
    )

    handler = FlowCommands()
    lines = handler._format_flow_status_lines(tmp_path, record, store)

    assert any(line == "Current: TICKET-002" for line in lines)
    store.close()


def test_flow_status_includes_reason_summary_and_error(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(
        FlowRunStatus.FAILED,
        state={
            "reason_summary": "agent error",
            "ticket_engine": {"reason": "failed to parse"},
        },
    )
    record.error_message = "Traceback"
    lines = handler._format_flow_status_lines(
        tmp_path, record, store=None, health=_health(tmp_path)
    )

    assert any(line == "Summary: agent error" for line in lines)
    assert any(line == "Reason: failed to parse" for line in lines)
    assert any(line == "Error: Traceback" for line in lines)


def test_flow_status_keyboard_paused(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.PAUSED)
    keyboard = handler._build_flow_status_keyboard(record, health=_health(tmp_path))

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Resume", "Restart", "Archive"]


def test_flow_status_keyboard_dead_worker(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.RUNNING)
    keyboard = handler._build_flow_status_keyboard(
        record, health=_health(tmp_path, status="dead")
    )

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Recover", "Refresh"]


def test_flow_status_keyboard_terminal(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.COMPLETED)
    keyboard = handler._build_flow_status_keyboard(record, health=_health(tmp_path))

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Restart", "Archive", "Refresh"]


class _FlowStatusHandler(FlowCommands):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.markups: list[dict[str, object] | None] = []

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
        _ = (thread_id, reply_to, parse_mode)
        self.sent.append(text)
        self.markups.append(reply_markup)


@pytest.mark.anyio
async def test_flow_status_action_sends_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FlowStore(tmp_path / ".codex-autorunner" / "flows.db")
    store.initialize()
    run_id = str(uuid.uuid4())
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)
    record = store.get_flow_run(run_id)
    assert record is not None
    store.close()

    snapshot = {
        "worker_health": _health(tmp_path),
        "effective_current_ticket": None,
        "last_event_seq": None,
        "last_event_at": None,
    }
    monkeypatch.setattr(
        flows_module,
        "build_flow_status_snapshot",
        lambda _root, _record, _store: snapshot,
    )

    handler = _FlowStatusHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow status",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow_status_action(message, tmp_path, argv=[])

    assert handler.sent
    assert any("Run:" in line for line in handler.sent[0].splitlines())
    assert handler.markups[0] is not None


@pytest.mark.anyio
async def test_flow_status_action_callback_keeps_repo_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FlowStore(tmp_path / ".codex-autorunner" / "flows.db")
    store.initialize()
    run_id = str(uuid.uuid4())
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
    store.close()

    snapshot = {
        "worker_health": _health(tmp_path),
        "effective_current_ticket": None,
        "last_event_seq": None,
        "last_event_at": None,
    }
    monkeypatch.setattr(
        flows_module,
        "build_flow_status_snapshot",
        lambda _root, _record, _store: snapshot,
    )

    handler = _FlowStatusHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow repo status",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow_status_action(
        message, tmp_path, argv=[], repo_id="car-wt-3"
    )

    keyboard = handler.markups[0]
    assert keyboard is not None
    callback = keyboard["inline_keyboard"][0][1]["callback_data"]
    assert callback.endswith(":car-wt-3")


class _TopicStoreStub:
    def __init__(self, record: object | None) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> object | None:
        return self._record


class _PMAFlowStatusHandler(FlowCommands):
    def __init__(self, record: object | None) -> None:
        self._store = _TopicStoreStub(record)
        self.hub_calls = 0
        self.sent: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    def _resolve_workspace(self, _arg: str) -> tuple[str, Path] | None:
        return None

    async def _send_flow_hub_overview(self, _message: TelegramMessage) -> None:
        self.hub_calls += 1

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


@pytest.mark.anyio
async def test_flow_default_in_pma_topic_uses_hub_overview() -> None:
    record = SimpleNamespace(pma_enabled=True, workspace_path=None)
    handler = _PMAFlowStatusHandler(record)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow(message, "")

    assert handler.hub_calls == 1
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_default_unbound_topic_uses_hub_overview() -> None:
    handler = _PMAFlowStatusHandler(None)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow(message, "")

    assert handler.hub_calls == 1
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_hub_overview_allows_parse_mode_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Regression: /flow hub overview uses explicit parse_mode override.
    class _StubStore:
        def initialize(self) -> None: ...

        def list_flow_runs(self, *args, **kwargs):
            return []

        def close(self) -> None: ...

    class _HubOverviewHandler(FlowCommands):
        def __init__(self) -> None:
            self._manifest_path = tmp_path / "manifest.yml"
            self._hub_root = tmp_path
            self._store = _TopicStoreStub(None)
            self.sent: list[tuple[str, Optional[str]]] = []

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: int | None
        ) -> str:
            return "topic"

        def _resolve_workspace(self, _arg: str) -> tuple[str, Path] | None:
            return None

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
            _ = (thread_id, reply_to, reply_markup)
            self.sent.append((text, parse_mode))

    monkeypatch.setattr(flows_module, "_load_flow_store", lambda _root: _StubStore())
    monkeypatch.setattr(
        flows_module,
        "load_manifest",
        lambda _path, _root: SimpleNamespace(
            repos=[SimpleNamespace(id="r1", enabled=True, path=".")]
        ),
    )

    handler = _HubOverviewHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow",
        date=None,
        is_topic_message=True,
    )

    await handler._send_flow_hub_overview(message)

    assert handler.sent
    assert "`r1`" in handler.sent[0][0]
    assert "\n\n" not in handler.sent[0][0]
    assert "`/flow <repo-id> status`" in handler.sent[0][0]
    assert handler.sent[0][1] == "Markdown"


@pytest.mark.anyio
async def test_flow_hub_overview_marks_completed_idle_as_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _StubStore:
        def initialize(self) -> None: ...

        def list_flow_runs(self, *args, **kwargs):
            return []

        def close(self) -> None: ...

    class _HubOverviewHandler(FlowCommands):
        def __init__(self) -> None:
            self._manifest_path = tmp_path / "manifest.yml"
            self._hub_root = tmp_path
            self._store = _TopicStoreStub(None)
            self.sent: list[tuple[str, Optional[str]]] = []

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: int | None
        ) -> str:
            return "topic"

        def _resolve_workspace(self, _arg: str) -> tuple[str, Path] | None:
            return None

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
            self.sent.append((text, parse_mode))

    monkeypatch.setattr(flows_module, "_load_flow_store", lambda _root: _StubStore())
    monkeypatch.setattr(
        flows_module,
        "ticket_progress",
        lambda _root: {"done": 3, "total": 3},
    )
    monkeypatch.setattr(
        flows_module,
        "load_manifest",
        lambda _path, _root: SimpleNamespace(
            repos=[SimpleNamespace(id="base--my-worktree", enabled=True, path=".")]
        ),
    )

    handler = _HubOverviewHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow",
        date=None,
        is_topic_message=True,
    )

    await handler._send_flow_hub_overview(message)

    assert handler.sent
    assert "🔵 `my-worktree`: Done 3/3" in handler.sent[0][0]


@pytest.mark.anyio
async def test_flow_hub_overview_uses_latest_run_not_stale_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest = _record(FlowRunStatus.COMPLETED)
    stale_active = _record(FlowRunStatus.PENDING)

    class _StubStore:
        def initialize(self) -> None: ...

        def list_flow_runs(self, *args, **kwargs):
            return [latest, stale_active]

        def close(self) -> None: ...

    class _HubOverviewHandler(FlowCommands):
        def __init__(self) -> None:
            self._manifest_path = tmp_path / "manifest.yml"
            self._hub_root = tmp_path
            self._store = _TopicStoreStub(None)
            self.sent: list[tuple[str, Optional[str]]] = []

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: int | None
        ) -> str:
            return "topic"

        def _resolve_workspace(self, _arg: str) -> tuple[str, Path] | None:
            return None

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
            self.sent.append((text, parse_mode))

    monkeypatch.setattr(flows_module, "_load_flow_store", lambda _root: _StubStore())
    monkeypatch.setattr(
        flows_module,
        "ticket_progress",
        lambda _root: {"done": 13, "total": 13},
    )
    monkeypatch.setattr(
        flows_module,
        "load_manifest",
        lambda _path, _root: SimpleNamespace(
            repos=[
                SimpleNamespace(id="base--extension-refactor", enabled=True, path=".")
            ]
        ),
    )

    handler = _HubOverviewHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow",
        date=None,
        is_topic_message=True,
    )

    await handler._send_flow_hub_overview(message)

    assert handler.sent
    text = handler.sent[0][0]
    assert f"🔵 `extension-refactor`: completed 13/13 run `{latest.id}`" in text
    assert stale_active.id not in text
