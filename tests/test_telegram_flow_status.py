from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows import hub_overview as hub_overview_module
from codex_autorunner.core.flows.models import (
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
)
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.integrations.telegram.adapter import (
    FlowCallback,
    TelegramMessage,
    build_model_keyboard,
    parse_callback_data,
)
from codex_autorunner.integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands
from codex_autorunner.integrations.telegram.notifications import (
    TelegramNotificationHandlers,
)
from codex_autorunner.integrations.telegram.progress_stream import TurnProgressTracker


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
    assert texts == ["Resume", "Archive"]


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
    assert texts == ["Archive", "Refresh"]


def test_flow_status_keyboard_falls_back_when_repo_id_is_too_long(
    tmp_path: Path,
) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.PAUSED)
    long_repo_id = "codex-autorunner--architecture-boundary-refactors"

    keyboard = handler._build_flow_status_keyboard(
        record, health=_health(tmp_path), repo_id=long_repo_id
    )

    assert keyboard is not None
    callback_data = keyboard["inline_keyboard"][0][0]["callback_data"]
    assert parse_callback_data(callback_data) == FlowCallback(
        action="resume", run_id=record.id, repo_id=None
    )
    assert handler._flow_repo_context[record.id] == long_repo_id


def test_model_picker_keyboard_matches_golden_fixture() -> None:
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "telegram"
    expected_keyboard = json.loads(
        (fixture_dir / "model_picker_keyboard.json").read_text()
    )

    keyboard = build_model_keyboard(
        [("gpt-5", "GPT-5"), ("o3-mini", "o3-mini")],
        page_button=("More", "page:model:1"),
        include_cancel=True,
    )

    assert keyboard == expected_keyboard


class _ProgressCadenceHarness(TelegramNotificationHandlers):
    def __init__(self, min_interval: float) -> None:
        self._config = SimpleNamespace(
            progress_stream=SimpleNamespace(
                enabled=True,
                min_edit_interval_seconds=min_interval,
                max_actions=4,
                max_output_chars=300,
            )
        )
        self._turn_progress_locks: dict[tuple[str, str], Any] = {}
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {
            ("turn-1", "thread-1"): SimpleNamespace(finalized=False)
        }
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_tasks: dict[tuple[str, str], Any] = {}
        self._turn_contexts: dict[tuple[str, str], Any] = {
            ("turn-1", "thread-1"): SimpleNamespace(placeholder_message_id=100)
        }
        self.emitted: list[tuple[tuple[str, str], float]] = []
        self.delayed: list[tuple[tuple[str, str], float]] = []

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
    ) -> None:
        _ = (ctx, force)
        self.emitted.append((turn_key, now if now is not None else -1.0))

    async def _delayed_progress_edit(
        self, turn_key: tuple[str, str], delay: float
    ) -> None:
        self.delayed.append((turn_key, delay))

    def _spawn_task(self, coro: Any) -> Any:
        return asyncio.create_task(coro)


class _StartTurnProgressHarness(TelegramNotificationHandlers):
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            progress_stream=SimpleNamespace(
                enabled=True,
                min_edit_interval_seconds=1.0,
                max_actions=4,
                max_output_chars=TELEGRAM_MAX_MESSAGE_LENGTH,
            )
        )
        self._logger = logging.getLogger("test")
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {}
        self._turn_progress_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_tasks: dict[tuple[str, str], Any] = {}
        self._turn_progress_heartbeat_tasks: dict[tuple[str, str], Any] = {}
        self._turn_progress_locks: dict[tuple[str, str], Any] = {}
        self._turn_contexts: dict[tuple[str, str], Any] = {}
        self._pending_context_usage: dict[tuple[str, str], int] = {}
        self._cache_access: dict[str, dict[tuple[str, str], float]] = {}
        self.progress_edits: list[tuple[tuple[str, str], bool]] = []

    def _touch_cache_timestamp(self, cache_name: str, key: tuple[str, str]) -> None:
        self._cache_access.setdefault(cache_name, {})[key] = 0.0

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
    ) -> None:
        _ = (ctx, now)
        self.progress_edits.append((turn_key, force))

    def _spawn_task(self, coro: Any) -> Any:
        coro.close()
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)


class _AsyncNoopLock:
    async def __aenter__(self) -> "_AsyncNoopLock":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = (exc_type, exc, tb)
        return False


class _TurnCompletionProgressHarness(TelegramNotificationHandlers):
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            progress_stream=SimpleNamespace(
                enabled=True,
                min_edit_interval_seconds=0.5,
                max_actions=8,
                max_output_chars=400,
            )
        )
        self._turn_key = ("turn-1", "thread-1")
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {
            self._turn_key: TurnProgressTracker(
                started_at=0.0,
                agent="codex",
                model="mock-model",
                label="working",
                max_actions=8,
                max_output_chars=400,
            )
        }
        self._turn_contexts: dict[tuple[str, str], Any] = {self._turn_key: object()}
        self.edits: list[tuple[tuple[str, str], bool, str]] = []
        self.cleared: list[tuple[str, str]] = []

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[tuple[str, str]]:
        if turn_id == self._turn_key[0] and thread_id == self._turn_key[1]:
            return self._turn_key
        return None

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
        render_mode: str = "live",
    ) -> None:
        _ = (ctx, now)
        self.edits.append((turn_key, force, render_mode))

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        self.cleared.append(turn_key)


class _OutputDeltaProgressHarness(TelegramNotificationHandlers):
    def __init__(self) -> None:
        self._turn_key = ("turn-1", "thread-1")
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {
            self._turn_key: TurnProgressTracker(
                started_at=0.0,
                agent="opencode",
                model="mock-model",
                label="working",
                max_actions=8,
                max_output_chars=400,
            )
        }
        self._scheduled: list[tuple[str, str]] = []

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[tuple[str, str]]:
        if turn_id == self._turn_key[0] and thread_id == self._turn_key[1]:
            return self._turn_key
        return None

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        self._scheduled.append(turn_key)


class _ProgressMarkupHarness(TelegramNotificationHandlers):
    def __init__(self, *, label: str) -> None:
        self._turn_key = ("turn-1", "thread-1")
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {
            self._turn_key: TurnProgressTracker(
                started_at=0.0,
                agent="codex",
                model="mock-model",
                label=label,
                max_actions=4,
                max_output_chars=400,
            )
        }
        self._turn_progress_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_contexts: dict[tuple[str, str], Any] = {
            self._turn_key: SimpleNamespace(
                chat_id=1,
                thread_id=2,
                placeholder_message_id=99,
            )
        }
        self._cache_access: dict[str, dict[tuple[str, str], float]] = {}
        self.edits: list[dict[str, Any]] = []

    def _touch_cache_timestamp(self, cache_name: str, key: tuple[str, str]) -> None:
        self._cache_access.setdefault(cache_name, {})[key] = 0.0

    def _interrupt_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Cancel", "callback_data": "cancel:interrupt"}]
            ]
        }

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        _ = message_thread_id
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return True


@pytest.mark.anyio
async def test_progress_edit_cadence_emits_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ProgressCadenceHarness(min_interval=2.0)
    key = ("turn-1", "thread-1")
    harness._turn_progress_updated_at[key] = 10.0
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.notifications.time.monotonic",
        lambda: 12.2,
    )

    await harness._schedule_progress_edit(key)

    assert harness.emitted == [(key, 12.2)]
    assert key not in harness._turn_progress_tasks
    assert not harness.delayed


@pytest.mark.anyio
async def test_progress_edit_cadence_schedules_when_interval_not_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ProgressCadenceHarness(min_interval=2.0)
    key = ("turn-1", "thread-1")
    harness._turn_progress_updated_at[key] = 10.0
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.notifications.time.monotonic",
        lambda: 10.5,
    )

    await harness._schedule_progress_edit(key)
    await asyncio.sleep(0)

    assert not harness.emitted
    assert key in harness._turn_progress_tasks
    assert harness.delayed == [(key, 1.5)]


@pytest.mark.anyio
async def test_progress_edit_does_not_construct_lock_when_turn_lock_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ProgressCadenceHarness(min_interval=2.0)
    key = ("turn-1", "thread-1")
    harness._turn_progress_locks[key] = _AsyncNoopLock()
    harness._turn_progress_updated_at[key] = 10.0
    lock_ctor_calls = 0

    def _counting_lock() -> _AsyncNoopLock:
        nonlocal lock_ctor_calls
        lock_ctor_calls += 1
        return _AsyncNoopLock()

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.notifications.asyncio.Lock",
        _counting_lock,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.notifications.time.monotonic",
        lambda: 12.2,
    )

    await harness._schedule_progress_edit(key)
    assert lock_ctor_calls == 0


@pytest.mark.anyio
async def test_ensure_turn_progress_lock_returns_same_instance_for_concurrent_callers() -> (
    None
):
    harness = _ProgressCadenceHarness(min_interval=2.0)
    key = ("turn-1", "thread-1")

    locks = await asyncio.gather(
        *[harness._ensure_turn_progress_lock(key) for _ in range(10)]
    )
    first = locks[0]
    assert all(lock is first for lock in locks)
    assert harness._turn_progress_locks[key] is first


@pytest.mark.anyio
async def test_turn_completed_prunes_duplicate_terminal_output_from_progress() -> None:
    harness = _TurnCompletionProgressHarness()
    key = harness._turn_key
    tracker: TurnProgressTracker = harness._turn_progress_trackers[key]
    tracker.note_output("intermediate output one")
    tracker.end_output_segment()
    tracker.note_output("terminal final answer")

    await harness._note_progress_turn_completed(
        {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "status": "completed",
            "finalMessage": "terminal final answer",
        }
    )

    output_blocks = [
        action.text for action in tracker.actions if action.label == "output"
    ]
    assert output_blocks == ["intermediate output one"]
    assert tracker.label == "done"
    assert tracker.finalized is True
    assert harness.edits == [(key, True, "final")]
    assert harness.cleared == [key]


@pytest.mark.anyio
async def test_output_delta_log_line_updates_token_usage_in_place() -> None:
    harness = _OutputDeltaProgressHarness()
    key = harness._turn_key
    tracker: TurnProgressTracker = harness._turn_progress_trackers[key]

    await harness._note_progress_output_delta(
        "outputDelta",
        {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "deltaType": "log_line",
            "delta": "tokens used - input: 66, output: 158, reasoning: 0",
        },
    )
    await harness._note_progress_output_delta(
        "outputDelta",
        {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "deltaType": "log_line",
            "delta": "tokens used - input: 297, cached: 11853, output: 147, reasoning: 0",
        },
    )

    output_blocks = [
        action.text for action in tracker.actions if action.label == "output"
    ]
    assert output_blocks == [
        "tokens used - input: 297, cached: 11853, output: 147, reasoning: 0"
    ]
    assert harness._scheduled == [key, key]


@pytest.mark.anyio
async def test_output_delta_with_explicit_assistant_stream_does_not_use_log_fallback() -> (
    None
):
    harness = _OutputDeltaProgressHarness()
    key = harness._turn_key
    tracker: TurnProgressTracker = harness._turn_progress_trackers[key]

    await harness._note_progress_output_delta(
        "outputDelta",
        {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "deltaType": "assistant_stream",
            "delta": "tokens used - this is normal assistant text",
        },
    )

    output_actions = [action for action in tracker.actions if action.label == "output"]
    assert len(output_actions) == 1
    assert output_actions[0].item_id is None
    assert output_actions[0].text == "tokens used - this is normal assistant text"


@pytest.mark.anyio
async def test_start_turn_progress_uses_full_message_budget_for_persistent_output() -> (
    None
):
    harness = _StartTurnProgressHarness()
    turn_key = ("turn-1", "thread-1")
    ctx = SimpleNamespace(chat_id=1, thread_id=2, topic_key="topic-1")

    await harness._start_turn_progress(
        turn_key,
        ctx=ctx,
        agent="codex",
        model="gpt-5.3-codex",
    )

    tracker = harness._turn_progress_trackers[turn_key]
    assert tracker.max_output_chars == TELEGRAM_MAX_MESSAGE_LENGTH


@pytest.mark.anyio
async def test_emit_progress_edit_keeps_cancel_button_for_active_review_label() -> None:
    harness = _ProgressMarkupHarness(label="review")
    await harness._emit_progress_edit(harness._turn_key, force=True)

    assert harness.edits
    markup = harness.edits[-1]["reply_markup"]
    assert isinstance(markup, dict)
    assert markup["inline_keyboard"][0][0]["text"] == "Cancel"


@pytest.mark.anyio
async def test_emit_progress_edit_clears_keyboard_for_terminal_label() -> None:
    harness = _ProgressMarkupHarness(label="done")
    await harness._emit_progress_edit(harness._turn_key, force=True)

    assert harness.edits
    assert harness.edits[-1]["reply_markup"] == {"inline_keyboard": []}


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


class _FlowWorktreeTargetHandler(FlowCommands):
    def __init__(self, repo_root: Path) -> None:
        self._store = _TopicStoreStub(None)
        self._base_root = (repo_root / "base").resolve()
        self._repo_root = repo_root.resolve()
        self.status_calls: list[tuple[Path, list[str], str | None]] = []
        self.sent: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    def _resolve_workspace(self, arg: str) -> tuple[str, str] | None:
        if arg == "base":
            return str(self._base_root), "base"
        if arg == "base--wt-1":
            return str(self._repo_root), "base--wt-1"
        return None

    async def _handle_flow_status_action(
        self,
        _message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: str | None = None,
    ) -> None:
        self.status_calls.append((repo_root, argv, repo_id))

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


class _FlowManifestAliasHandler(FlowCommands):
    def __init__(self, repo_root: Path) -> None:
        self._store = _TopicStoreStub(None)
        self._repo_root = repo_root.resolve()
        self._manifest_path = repo_root / "manifest.yml"
        self._hub_root = repo_root
        self.status_calls: list[tuple[Path, list[str], str | None]] = []
        self.sent: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    def _resolve_workspace(self, arg: str) -> tuple[str, str] | None:
        if arg == "codex-autorunner--process-opencode-leak-remediation":
            return str(self._repo_root), arg
        if arg == "codex-autorunner--architecture-boundary-refactors":
            return str(self._repo_root.parent / "other"), arg
        return None

    async def _handle_flow_status_action(
        self,
        _message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: str | None = None,
    ) -> None:
        self.status_calls.append((repo_root, argv, repo_id))

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


class _FlowActionTokenPriorityHandler(FlowCommands):
    def __init__(self, repo_root: Path, workspace_root: Path) -> None:
        self._store = _TopicStoreStub(
            SimpleNamespace(workspace_path=str(workspace_root), pma_enabled=False)
        )
        self._repo_root = repo_root.resolve()
        self._manifest_path = repo_root / "manifest.yml"
        self._hub_root = repo_root
        self.runs_calls: list[tuple[Path, list[str], str | None]] = []
        self.status_calls: list[tuple[Path, list[str], str | None]] = []
        self.sent: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    def _resolve_workspace(self, arg: str) -> tuple[str, str] | None:
        if arg == "codex-autorunner--runs":
            return str(self._repo_root), arg
        return None

    async def _handle_flow_runs(
        self,
        _message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: str | None = None,
    ) -> None:
        self.runs_calls.append((repo_root, argv, repo_id))

    async def _handle_flow_status_action(
        self,
        _message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: str | None = None,
    ) -> None:
        self.status_calls.append((repo_root, argv, repo_id))

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
async def test_flow_runs_in_pma_topic_uses_hub_overview() -> None:
    record = SimpleNamespace(pma_enabled=True, workspace_path=None)
    handler = _PMAFlowStatusHandler(record)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow runs",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow(message, "runs")

    assert handler.hub_calls == 1
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_runs_unbound_topic_uses_hub_overview() -> None:
    handler = _PMAFlowStatusHandler(None)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow runs",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow(message, "runs")

    assert handler.hub_calls == 1
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_repo_and_worktree_default_to_status_when_both_resolve(
    tmp_path: Path,
) -> None:
    handler = _FlowWorktreeTargetHandler(tmp_path)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow base wt-1",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_flow(message, "base wt-1")

    assert handler.status_calls == [(tmp_path.resolve(), [], "base--wt-1")]
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_repo_and_worktree_branch_aliases_resolve_target_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handler = _FlowManifestAliasHandler(tmp_path)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow architecture-boundary-refactors process-opencode-leak-remediation",
        date=None,
        is_topic_message=True,
    )

    manifest = SimpleNamespace(
        repos=[
            SimpleNamespace(
                id="codex-autorunner",
                enabled=True,
                kind="base",
                branch=None,
                display_name="codex-autorunner",
                worktree_of=None,
                path=".",
            ),
            SimpleNamespace(
                id="codex-autorunner--architecture-boundary-refactors",
                enabled=True,
                kind="worktree",
                branch="architecture-boundary-refactors",
                display_name="codex-autorunner--architecture-boundary-refactors",
                worktree_of="codex-autorunner",
                path=".",
            ),
            SimpleNamespace(
                id="codex-autorunner--process-opencode-leak-remediation",
                enabled=True,
                kind="worktree",
                branch="process-opencode-leak-remediation",
                display_name="codex-autorunner--process-opencode-leak-remediation",
                worktree_of="codex-autorunner",
                path=".",
            ),
        ]
    )
    monkeypatch.setattr(flows_module, "load_manifest", lambda _path, _root: manifest)

    await handler._handle_flow(
        message, "architecture-boundary-refactors process-opencode-leak-remediation"
    )

    assert handler.status_calls == [
        (
            tmp_path.resolve(),
            [],
            "codex-autorunner--process-opencode-leak-remediation",
        )
    ]
    assert not handler.sent


@pytest.mark.anyio
async def test_flow_action_token_not_shadowed_by_manifest_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    handler = _FlowActionTokenPriorityHandler(tmp_path / "target-repo", workspace_root)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/flow runs 7",
        date=None,
        is_topic_message=True,
    )

    manifest = SimpleNamespace(
        repos=[
            SimpleNamespace(
                id="codex-autorunner",
                enabled=True,
                kind="base",
                branch=None,
                display_name="codex-autorunner",
                worktree_of=None,
                path=".",
            ),
            SimpleNamespace(
                id="codex-autorunner--runs",
                enabled=True,
                kind="worktree",
                branch="runs",
                display_name="codex-autorunner--runs",
                worktree_of="codex-autorunner",
                path=".",
            ),
        ]
    )
    monkeypatch.setattr(flows_module, "load_manifest", lambda _path, _root: manifest)

    await handler._handle_flow(message, "runs 7")

    assert handler.runs_calls == [(workspace_root.resolve(), ["7"], None)]
    assert not handler.status_calls
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
    assert "`/flow <repo-id> <worktree-id>`" in handler.sent[0][0]
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
            repos=[
                SimpleNamespace(
                    id="base--my-worktree",
                    enabled=True,
                    path=".",
                    kind="worktree",
                    worktree_of="base",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"base--my-worktree": 1},
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
async def test_flow_hub_overview_shows_only_active_chat_bound_worktrees(
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
        "load_manifest",
        lambda _path, _root: SimpleNamespace(
            repos=[
                SimpleNamespace(id="base", enabled=True, path=".", kind="base"),
                SimpleNamespace(
                    id="base--wt-visible",
                    enabled=True,
                    path=".",
                    kind="worktree",
                    worktree_of="base",
                ),
                SimpleNamespace(
                    id="base--wt-hidden",
                    enabled=True,
                    path=".",
                    kind="worktree",
                    worktree_of="base",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"base--wt-visible": 1},
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
    content = handler.sent[0][0]
    assert "`wt-visible`" in content
    assert "`wt-hidden`" not in content
    assert "\n  -> " in content
    assert "\n  - " not in content


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
                SimpleNamespace(
                    id="base--extension-refactor",
                    enabled=True,
                    path=".",
                    kind="worktree",
                    worktree_of="base",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"base--extension-refactor": 1},
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
