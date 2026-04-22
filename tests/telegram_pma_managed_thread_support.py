import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import anyio
import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.hub_control_plane import HubSharedStateService
from codex_autorunner.core.orchestration import SQLiteManagedThreadDeliveryEngine
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryState,
)
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_context import (
    build_hub_snapshot,
)
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)
from codex_autorunner.core.sse import format_sse
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerDisconnected,
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.app_server.threads import (
    PMA_KEY,
    AppServerThreadRegistry,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramMessage,
)
from codex_autorunner.integrations.telegram.handlers import (
    messages as telegram_messages_module,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    _TurnRunResult,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.notifications import (
    TelegramNotificationHandlers,
)
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
)


class _NoopSupervisor:
    def list_agent_workspaces(self, *, use_cache: bool = True) -> list[object]:
        _ = use_cache
        return []

    def get_agent_workspace_snapshot(self, workspace_id: str) -> object:
        raise ValueError(f"Unknown workspace id: {workspace_id}")

    def run_setup_commands_for_workspace(
        self, workspace_root: Path, *, repo_id_hint: Optional[str] = None
    ) -> int:
        _ = workspace_root, repo_id_hint
        return 0

    def process_pma_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        return {
            "timers_processed": 1 if include_timers else 0,
            "wakeups_dispatched": limit,
        }


class _InProcessHubControlPlaneClient:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)
        prepare_orchestration_sqlite(self._hub_root, durable=False)
        prepare_pma_thread_store(self._hub_root, durable=False)
        self._service = HubSharedStateService(
            hub_root=self._hub_root,
            supervisor=_NoopSupervisor(),
            durable_writes=False,
        )

    async def get_surface_binding(self, request: Any) -> Any:
        return self._service.get_surface_binding(request)

    async def upsert_surface_binding(self, request: Any) -> Any:
        return self._service.upsert_surface_binding(request)

    async def list_surface_bindings(self, request: Any) -> Any:
        return self._service.list_surface_bindings(request)

    async def get_thread_target(self, request: Any) -> Any:
        return self._service.get_thread_target(request)

    async def list_thread_targets(self, request: Any) -> Any:
        return self._service.list_thread_targets(request)

    async def create_thread_target(self, request: Any) -> Any:
        return self._service.create_thread_target(request)

    async def create_execution(self, request: Any) -> Any:
        return self._service.create_execution(request)

    async def get_execution(self, request: Any) -> Any:
        return self._service.get_execution(request)

    async def get_running_execution(self, request: Any) -> Any:
        return self._service.get_running_execution(request)

    async def list_thread_target_ids_with_running_executions(self, request: Any) -> Any:
        return self._service.list_thread_target_ids_with_running_executions(request)

    async def get_latest_execution(self, request: Any) -> Any:
        return self._service.get_latest_execution(request)

    async def list_queued_executions(self, request: Any) -> Any:
        return self._service.list_queued_executions(request)

    async def get_queue_depth(self, request: Any) -> Any:
        return self._service.get_queue_depth(request)

    async def cancel_queued_execution(self, request: Any) -> Any:
        return self._service.cancel_queued_execution(request)

    async def promote_queued_execution(self, request: Any) -> Any:
        return self._service.promote_queued_execution(request)

    async def record_execution_result(self, request: Any) -> Any:
        return self._service.record_execution_result(request)

    async def record_execution_interrupted(self, request: Any) -> Any:
        return self._service.record_execution_interrupted(request)

    async def cancel_queued_executions(self, request: Any) -> Any:
        return self._service.cancel_queued_executions(request)

    async def set_execution_backend_id(self, request: Any) -> None:
        self._service.set_execution_backend_id(request)

    async def claim_next_queued_execution(self, request: Any) -> Any:
        return self._service.claim_next_queued_execution(request)

    async def persist_execution_timeline(self, request: Any) -> Any:
        return self._service.persist_execution_timeline(request)

    async def finalize_execution_cold_trace(self, request: Any) -> Any:
        return self._service.finalize_execution_cold_trace(request)

    async def resume_thread_target(self, request: Any) -> Any:
        return self._service.resume_thread_target(request)

    async def archive_thread_target(self, request: Any) -> Any:
        return self._service.archive_thread_target(request)

    async def set_thread_backend_id(self, request: Any) -> None:
        self._service.set_thread_backend_id(request)

    async def record_thread_activity(self, request: Any) -> None:
        self._service.record_thread_activity(request)

    async def update_thread_compact_seed(self, request: Any) -> Any:
        return self._service.update_thread_compact_seed(request)

    async def get_transcript_history(self, request: Any) -> Any:
        return self._service.get_transcript_history(request)

    async def write_transcript(self, request: Any) -> Any:
        return self._service.write_transcript(request)

    async def get_pma_snapshot(self) -> Any:
        return type(
            "PmaSnapshotResponse",
            (),
            {"snapshot": await build_hub_snapshot(None, hub_root=self._hub_root)},
        )()

    async def get_agent_workspace(self, request: Any) -> Any:
        return self._service.get_agent_workspace(request)

    async def list_agent_workspaces(self, request: Any) -> Any:
        return self._service.list_agent_workspaces(request)

    async def run_workspace_setup_commands(self, request: Any) -> Any:
        return self._service.run_workspace_setup_commands(request)

    async def request_automation(self, request: Any) -> Any:
        return self._service.request_automation(request)

    async def aclose(self) -> None:
        return None


class _TurnResult:
    def __init__(self) -> None:
        self.agent_messages = ["ok"]
        self.errors: list[str] = []
        self.status = "completed"
        self.token_usage = None


class _TurnHandle:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id

    async def wait(self, *_args: object, **_kwargs: object) -> _TurnResult:
        return _TurnResult()


class _PMARouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def set_active_thread(
        self, _chat_id: int, _thread_id: Optional[int], _active_thread_id: Optional[str]
    ) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> None:
        if callable(apply):
            apply(self._record)


class _PMAClientStub:
    def __init__(self) -> None:
        self.thread_start_calls: list[tuple[str, str]] = []
        self.turn_start_calls: list[str] = []

    async def thread_start(self, cwd: str, *, agent: str, **_kwargs: object) -> dict:
        self.thread_start_calls.append((cwd, agent))
        return {"thread_id": f"fresh-{len(self.thread_start_calls)}"}

    async def turn_start(
        self, thread_id: str, _prompt_text: str, **_kwargs: object
    ) -> _TurnHandle:
        self.turn_start_calls.append(thread_id)
        if thread_id == "stale":
            raise CodexAppServerResponseError(
                method="turn/start",
                code=-32600,
                message="thread not found: stale",
                data=None,
            )
        return _TurnHandle("turn-1")


class _PMAHandler(TelegramCommandHandlers):
    def __init__(
        self,
        record: TelegramTopicRecord,
        client: _PMAClientStub,
        hub_root: Path,
        registry: AppServerThreadRegistry,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            agent_turn_timeout_seconds={"codex": None, "opencode": None},
        )
        self._router = _PMARouterStub(record)
        self._turn_semaphore = asyncio.Semaphore(1)
        self._turn_contexts: dict[tuple[str, str], object] = {}
        self._turn_preview_text: dict[tuple[str, str], str] = {}
        self._turn_preview_updated_at: dict[tuple[str, str], float] = {}
        self._token_usage_by_thread: dict[str, dict[str, object]] = {}
        self._token_usage_by_turn: dict[str, dict[str, object]] = {}
        self._voice_config = None
        self._turn_progress_by_turn: dict[tuple[str, str], object] = {}
        self._turn_progress_by_topic: dict[str, object] = {}
        self._turn_progress_last_update: dict[tuple[str, str], float] = {}
        self._client = client
        self._hub_root = hub_root
        self._hub_thread_registry = registry
        self._bot_username = None

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    async def _client_for_workspace(self, _workspace_path: str) -> _PMAClientStub:
        return self._client

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        return None

    async def _refresh_workspace_id(
        self, _key: str, _record: TelegramTopicRecord
    ) -> Optional[str]:
        return None

    def _effective_policies(
        self, _record: TelegramTopicRecord
    ) -> tuple[Optional[str], Optional[Any]]:
        return None, None

    async def _handle_thread_conflict(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _verify_active_thread(
        self, _message: TelegramMessage, record: TelegramTopicRecord
    ) -> TelegramTopicRecord:
        return record

    def _maybe_append_whisper_disclaimer(
        self, prompt_text: str, *, transcript_text: Optional[str]
    ) -> str:
        return prompt_text

    async def _maybe_inject_github_context(
        self,
        prompt_text: str,
        _record: object,
        *,
        link_source_text: Optional[str] = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = link_source_text, allow_cross_repo
        return prompt_text, False

    def _maybe_inject_car_context(self, prompt_text: str) -> tuple[str, bool]:
        return prompt_text, False

    def _maybe_inject_prompt_context(
        self,
        prompt_text: str,
        *,
        trigger_text: Optional[str] = None,
    ) -> tuple[str, bool]:
        _ = trigger_text
        return prompt_text, False

    def _maybe_inject_outbox_context(
        self,
        prompt_text: str,
        *,
        record: object,
        topic_key: str,
        has_file_context: bool = False,
        user_input_text: Optional[str] = None,
    ) -> tuple[str, bool]:
        _ = record, topic_key, has_file_context, user_input_text
        return prompt_text, False

    async def _prepare_turn_placeholder(
        self,
        _message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        return None

    async def _send_message(
        self,
        _chat_id: int,
        _text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        return None

    async def _edit_message_text(
        self,
        _chat_id: int,
        _message_id: int,
        _text: str,
        *,
        thread_id: Optional[int] = None,
        reply_markup: Optional[object] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        return None

    async def _delete_message(
        self,
        _chat_id: int,
        _message_id: int,
        *,
        thread_id: Optional[int] = None,
    ) -> None:
        return None

    async def _finalize_voice_transcript(
        self,
        _chat_id: int,
        _transcript_message_id: Optional[int],
        _transcript_text: Optional[str],
    ) -> None:
        return None

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
        delete_placeholder_on_delivery: bool = True,
    ) -> bool:
        _ = (
            chat_id,
            thread_id,
            reply_to,
            placeholder_id,
            response,
            delete_placeholder_on_delivery,
        )
        return True

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: object,
        agent: Optional[str],
        model: Optional[str],
        label: str,
    ) -> None:
        return None

    def _clear_turn_progress(self, _turn_key: tuple[str, str]) -> None:
        return None

    def _turn_key(
        self, thread_id: Optional[str], turn_id: Optional[str]
    ) -> Optional[tuple[str, str]]:
        if thread_id and turn_id:
            return (thread_id, turn_id)
        return None

    def _register_turn_context(
        self, turn_key: tuple[str, str], turn_id: str, ctx: object
    ) -> bool:
        self._turn_contexts[turn_key] = ctx
        return True

    def _clear_thinking_preview(self, _turn_key: tuple[str, str]) -> None:
        return None

    async def _require_thread_workspace(
        self,
        _message: TelegramMessage,
        _workspace_path: str,
        _thread: object,
        *,
        action: str,
    ) -> bool:
        return True

    def _format_turn_metrics(self, *_args: object, **_kwargs: object) -> Optional[str]:
        return None


class _ManagedThreadPMAHandler(_PMAHandler):
    def __init__(
        self,
        record: TelegramTopicRecord,
        hub_root: Path,
    ) -> None:
        super().__init__(
            record,
            client=_PMAClientStub(),
            hub_root=hub_root,
            registry=AppServerThreadRegistry(hub_root / "threads.json"),
        )
        self._config = SimpleNamespace(
            root=hub_root,
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            agent_turn_timeout_seconds={"codex": None, "opencode": None},
            metrics_mode="separate",
            progress_stream=SimpleNamespace(
                enabled=True,
                max_actions=10,
                max_output_chars=120,
                min_edit_interval_seconds=0.0,
            ),
        )
        self._sent: list[str] = []
        self._edited: list[dict[str, Any]] = []
        self._deleted: list[dict[str, Any]] = []
        self._placeholders: list[dict[str, Any]] = []
        self._next_placeholder_id = 900
        self._spawned_tasks: set[asyncio.Task[object]] = set()
        self._turn_progress_trackers: dict[tuple[str, str], object] = {}
        self._turn_progress_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_tasks: dict[tuple[str, str], asyncio.Task[object]] = {}
        self._turn_progress_heartbeat_tasks: dict[
            tuple[str, str], asyncio.Task[object]
        ] = {}
        self._turn_progress_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pending_context_usage: dict[tuple[str, str], int] = {}
        self._hub_client = _InProcessHubControlPlaneClient(hub_root)
        self._hub_handshake_compatibility = SimpleNamespace(compatible=True)

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        _ = thread_id, reply_to
        self._sent.append(text)

    async def _prepare_turn_placeholder(
        self,
        _message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        if placeholder_id is None and send_placeholder:
            placeholder_id = self._next_placeholder_id
            self._next_placeholder_id += 1
            self._placeholders.append({"message_id": placeholder_id, "queued": queued})
        return placeholder_id

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_markup: Optional[object] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        _ = parse_mode, disable_web_page_preview
        self._edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "thread_id": thread_id,
                "reply_markup": reply_markup,
            }
        )

    async def _delete_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        thread_id: Optional[int] = None,
    ) -> None:
        self._deleted.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "thread_id": thread_id,
            }
        )

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
        delete_placeholder_on_delivery: bool = True,
    ) -> bool:
        _ = (
            chat_id,
            thread_id,
            reply_to,
            placeholder_id,
            delete_placeholder_on_delivery,
        )
        self._sent.append(response)
        return True

    def _touch_cache_timestamp(self, _cache_name: str, _key: object) -> None:
        return None

    async def _flush_outbox_files(
        self,
        _record: TelegramTopicRecord,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        _ = chat_id, thread_id, reply_to
        return None

    def _format_turn_metrics_text(
        self,
        token_usage: Optional[dict[str, Any]],
        elapsed_seconds: Optional[float],
    ) -> Optional[str]:
        _ = token_usage, elapsed_seconds
        return None

    def _metrics_mode(self) -> str:
        return "separate"

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: object,
        agent: Optional[str],
        model: Optional[str],
        label: str,
    ) -> None:
        await TelegramNotificationHandlers._start_turn_progress(
            self,
            turn_key,
            ctx=ctx,
            agent=agent,
            model=model,
            label=label,
        )

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        TelegramNotificationHandlers._clear_turn_progress(self, turn_key)

    def _clear_thinking_preview(self, turn_key: tuple[str, str]) -> None:
        self._turn_preview_text.pop(turn_key, None)
        self._turn_preview_updated_at.pop(turn_key, None)
        self._clear_turn_progress(turn_key)

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[object] = None,
        now: Optional[float] = None,
        force: bool = False,
        render_mode: str = "live",
    ) -> None:
        await TelegramNotificationHandlers._emit_progress_edit(
            self,
            turn_key,
            ctx=ctx,
            now=now,
            force=force,
            render_mode=render_mode,
        )

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        await TelegramNotificationHandlers._schedule_progress_edit(self, turn_key)

    async def _ensure_turn_progress_lock(
        self, turn_key: tuple[str, str]
    ) -> asyncio.Lock:
        return await TelegramNotificationHandlers._ensure_turn_progress_lock(
            self,
            turn_key,
        )

    async def _delayed_progress_edit(
        self, turn_key: tuple[str, str], delay: float
    ) -> None:
        await TelegramNotificationHandlers._delayed_progress_edit(
            self,
            turn_key,
            delay,
        )

    async def _turn_progress_heartbeat(self, turn_key: tuple[str, str]) -> None:
        await TelegramNotificationHandlers._turn_progress_heartbeat(self, turn_key)

    async def _apply_run_event_to_progress(
        self,
        turn_key: tuple[str, str],
        run_event: object,
    ) -> None:
        await TelegramNotificationHandlers._apply_run_event_to_progress(
            self,
            turn_key,
            run_event,
        )

    def _spawn_task(self, coro):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)
        return task


@pytest.mark.anyio
async def test_managed_thread_queue_worker_wraps_execution_with_typing_indicator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    queued_started = object()
    begin_calls = 0

    class _Handler:
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._spawned_tasks: set[asyncio.Task[object]] = set()

        def _spawn_task(self, coro):  # type: ignore[no-untyped-def]
            task = asyncio.create_task(coro)
            self._spawned_tasks.add(task)
            task.add_done_callback(self._spawned_tasks.discard)
            return task

        async def _begin_typing_indicator(
            self, chat_id: int, thread_id: Optional[int]
        ) -> None:
            events.append(("begin", chat_id, thread_id))

        async def _end_typing_indicator(
            self, chat_id: int, thread_id: Optional[int]
        ) -> None:
            events.append(("end", chat_id, thread_id))

        async def _send_message(
            self,
            chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            _ = reply_to
            events.append(("send", chat_id, thread_id, text))

        async def _flush_outbox_files(
            self,
            record: TelegramTopicRecord,
            *,
            chat_id: int,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            _ = record, reply_to
            events.append(("flush", chat_id, thread_id))

    class _OrchestrationServiceStub:
        def get_running_execution(self, managed_thread_id: str) -> None:
            _ = managed_thread_id
            return None

    async def _fake_begin_next(
        orchestration_service: object,
        managed_thread_id: str,
    ) -> Optional[object]:
        nonlocal begin_calls
        _ = orchestration_service, managed_thread_id
        if begin_calls == 0:
            begin_calls += 1
            return queued_started
        return None

    async def _fake_run_started_execution(
        self: object,
        started: object,
        *,
        hooks: object = None,
        runtime_event_state: object = None,
    ) -> dict[str, object]:
        _ = self, hooks, runtime_event_state
        assert started is queued_started
        events.append("finalize")
        return {"status": "ok", "assistant_text": "queued telegram reply"}

    monkeypatch.setattr(
        execution_commands_module.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    handler = _Handler()
    coordinator = execution_commands_module._build_telegram_managed_thread_coordinator(
        handler,
        orchestration_service=_OrchestrationServiceStub(),
        surface_key="telegram:-1001:101",
        chat_id=-1001,
        thread_id=101,
        public_execution_error=(
            execution_commands_module.TELEGRAM_PMA_PUBLIC_EXECUTION_ERROR
        ),
        timeout_error=execution_commands_module.TELEGRAM_PMA_TIMEOUT_ERROR,
        interrupted_error=execution_commands_module.TELEGRAM_PMA_INTERRUPTED_ERROR,
        pma_enabled=True,
    )

    async def _run_with_telegram_typing_indicator(work: Any) -> None:
        await handler._begin_typing_indicator(-1001, 101)
        try:
            await work()
        finally:
            await handler._end_typing_indicator(-1001, 101)

    async def _deliver_result(
        finalized: execution_commands_module.ManagedThreadFinalizationResult,
    ) -> None:
        await handler._send_message(
            -1001,
            finalized.assistant_text,
            thread_id=101,
            reply_to=None,
        )
        await handler._flush_outbox_files(
            TelegramTopicRecord(workspace_path=str(tmp_path), pma_enabled=True),
            chat_id=-1001,
            thread_id=101,
            reply_to=None,
        )

    coordinator.ensure_queue_worker(
        task_map={},
        managed_thread_id="managed-thread-1",
        spawn_task=lambda coro: (
            execution_commands_module._spawn_telegram_background_task(
                handler,
                coro,
            )
        ),
        hooks=execution_commands_module.ManagedThreadCoordinatorHooks(
            deliver_result=_deliver_result,
            run_with_indicator=_run_with_telegram_typing_indicator,
        ),
        begin_next_execution=_fake_begin_next,
    )

    spawned = list(handler._spawned_tasks)
    assert len(spawned) == 1
    await asyncio.gather(*spawned)

    assert events == [
        ("begin", -1001, 101),
        "finalize",
        ("end", -1001, 101),
        ("send", -1001, 101, "queued telegram reply"),
        ("flush", -1001, 101),
    ]


@pytest.mark.anyio
async def test_pma_managed_thread_turn_edits_placeholder_with_live_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    stream_finished = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            await stream_finished.wait()
            return SimpleNamespace(
                status="ok",
                assistant_text="telegram managed final reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {
                            "itemId": "reason-1",
                            "delta": "thinking through telegram pma",
                        },
                    }
                },
            )
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "item/agentMessage/delta",
                        "params": {"delta": "partial telegram managed reply"},
                    }
                },
            )
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert handler._placeholders
    assert handler._edited
    edited_texts = [str(edit["text"]) for edit in handler._edited]
    assert len(edited_texts) >= 2
    assert len(set(edited_texts)) >= 2
    assert "telegram managed final reply" in handler._sent

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_pma_managed_opencode_turn_edits_placeholder_with_thinking_and_tool_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="opencode",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    stream_finished = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            await stream_finished.wait()
            return SimpleNamespace(
                status="ok",
                assistant_text="telegram opencode managed final reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "telegram-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "reason-1",
                                "type": "reasoning",
                                "messageID": "assistant-1",
                                "sessionID": "telegram-backend-thread-1",
                                "text": "thinking through telegram opencode progress",
                            },
                        },
                    },
                }
            }
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "telegram-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "messageID": "assistant-1",
                                "sessionID": "telegram-backend-thread-1",
                                "tool": "shell",
                                "input": "rg AGENTS.md",
                                "state": {"status": "running"},
                            },
                        },
                    },
                }
            }
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "opencode": AgentDescriptor(
                id="opencode",
                name="OpenCode",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert handler._placeholders
    assert handler._edited
    edited_texts = [str(edit["text"]) for edit in handler._edited]
    assert len(edited_texts) >= 2
    assert any(
        "thinking through telegram opencode progress" in text for text in edited_texts
    )
    assert any("shell" in text for text in edited_texts)
    assert "telegram opencode managed final reply" in handler._sent

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_pma_managed_thread_turn_recovers_if_wait_disconnects_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    stream_finished = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            await stream_finished.wait()
            raise CodexAppServerDisconnected("Reconnecting... 2/5")

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.completed",
                        "params": {
                            "text": "telegram completed reply survives",
                            "info": {"id": "msg-1", "role": "assistant"},
                        },
                    }
                },
            )
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "turn/completed",
                        "params": {"status": "completed"},
                    }
                },
            )
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert "telegram completed reply survives" in handler._sent
    assert not any("turn failed" in sent.lower() for sent in handler._sent)
    assert not any("disconnected" in sent.lower() for sent in handler._sent)

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_handle_normal_message_abandons_pending_durable_delivery_after_direct_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    engine = SQLiteManagedThreadDeliveryEngine(tmp_path)
    finalized = execution_commands_module.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="telegram managed final reply",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-1",
    )
    intent = execution_commands_module.build_managed_thread_delivery_intent(
        finalized,
        surface=execution_commands_module.ManagedThreadSurfaceInfo(
            log_label="Telegram",
            surface_kind="telegram",
            surface_key="-1001:101",
        ),
        transport_target={"chat_id": -1001, "thread_id": 101},
    )
    record_entry = engine.create_intent(intent).record
    patched = engine._ledger.patch_delivery(
        record_entry.delivery_id,
        state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
    )
    assert patched is not None

    async def _fake_run_turn_and_collect_result(
        *args: Any, **kwargs: Any
    ) -> _TurnRunResult:
        _ = args, kwargs
        return _TurnRunResult(
            record=record,
            thread_id="backend-1",
            turn_id="exec-1",
            response="telegram managed final reply",
            placeholder_id=None,
            elapsed_seconds=None,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
            durable_delivery_handled=False,
            durable_delivery_id=record_entry.delivery_id,
        )

    monkeypatch.setattr(
        handler,
        "_run_turn_and_collect_result",
        _fake_run_turn_and_collect_result,
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    abandoned = engine._ledger.get_delivery(record_entry.delivery_id)
    assert abandoned is not None
    assert abandoned.state is ManagedThreadDeliveryState.ABANDONED
    assert "telegram managed final reply" in handler._sent


@pytest.mark.anyio
async def test_pma_text_messages_route_repeated_messages_through_managed_thread_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.waited_turns: list[str] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = f"telegram-backend-turn-{len(self.turn_prompts) + 1}"
            self.turn_prompts.append(prompt)
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            self.waited_turns.append(turn_id)
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="ok",
                    assistant_text="first telegram orchestration reply",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="second telegram orchestration reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(second_message, runtime=_RuntimeStub())
        release_first.set()
        await first_task
        with anyio.fail_after(5):
            while "second telegram orchestration reply" not in handler._sent:
                await anyio.sleep(0.05)

        assert "Queued (waiting for available worker...)" in handler._sent
        assert "first telegram orchestration reply" in handler._sent
        assert "second telegram orchestration reply" in handler._sent

        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["ok", "ok"]
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_followup_turn_without_new_thread_reuses_managed_thread_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.start_calls: list[tuple[str, str]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            self.start_calls.append((conversation_id, prompt))
            turn_id = f"telegram-backend-turn-{len(self.start_calls)}"
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id=turn_id,
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, timeout
            assert conversation_id == "telegram-backend-thread-1"
            assert isinstance(turn_id, str)
            return SimpleNamespace(
                status="ok",
                assistant_text=f"reply for {turn_id}",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first pma orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=_RuntimeStub())

    assert handler._hub_thread_registry.get_thread_id(PMA_KEY) == (
        "telegram-backend-thread-1"
    )

    followup_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="/compact",
        date=None,
        is_topic_message=True,
    )

    outcome = await handler._run_turn_and_collect_result(
        followup_message,
        runtime=_RuntimeStub(),
        text_override="compact summary prompt",
        record=record,
        allow_new_thread=False,
        missing_thread_message="No active thread to compact. Use /new to start one.",
        send_placeholder=False,
    )

    assert isinstance(outcome, _TurnRunResult)
    assert outcome.thread_id == "telegram-backend-thread-1"
    assert handler._hub_thread_registry.get_thread_id(PMA_KEY) == (
        "telegram-backend-thread-1"
    )
    assert [call[0] for call in harness.start_calls] == [
        "telegram-backend-thread-1",
        "telegram-backend-thread-1",
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_reuses_archived_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=str(workspace),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=frozenset({"durable_threads"}),
                make_harness=lambda _ctx: object(),
            )
        },
    )

    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        repo_id="repo-1",
        display_name="telegram:test-topic",
    )
    orchestration_service.upsert_binding(
        surface_kind="telegram",
        surface_key="telegram:-1001:101",
        thread_target_id=current_thread.thread_target_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="pma",
    )
    orchestration_service.archive_thread_target(current_thread.thread_target_id)

    (
        _service,
        resolved,
    ) = await execution_commands_module._resolve_telegram_managed_thread(
        handler,
        surface_key="telegram:-1001:101",
        workspace_root=workspace.resolve(),
        agent="codex",
        repo_id="repo-1",
        mode="pma",
        pma_enabled=True,
        allow_new_thread=False,
    )
    assert resolved is not None
    assert resolved.thread_target_id == current_thread.thread_target_id
    assert resolved.lifecycle_status == "active"


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_ignores_backend_thread_id_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            _ = agent_id, workspace_root
            return "runtime-test-1"

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append((thread_target_id, dict(kwargs)))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="codex",
                agent_profile=None,
                workspace_root=str(workspace),
                lifecycle_status="active",
                backend_thread_id=None,
            )

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create should not be used in this test")

        def upsert_binding(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                agent_profile=None,
                workspace_root=str(workspace),
                lifecycle_status="archived",
                backend_thread_id="legacy-backend",
            ),
        ),
    )

    (
        _service,
        resolved,
    ) = await execution_commands_module._resolve_telegram_managed_thread(
        SimpleNamespace(_logger=logging.getLogger("test")),
        surface_key="telegram:-1001:101",
        workspace_root=workspace.resolve(),
        agent="codex",
        repo_id="repo-1",
        mode="pma",
        pma_enabled=True,
        backend_thread_id="stale-backend",
        allow_new_thread=False,
    )

    assert resolved is not None
    assert resolved.thread_target_id == "thread-1"
    assert calls == [
        (
            "thread-1",
            {
                "backend_thread_id": "legacy-backend",
                "backend_runtime_instance_id": "runtime-test-1",
            },
        )
    ]


@pytest.mark.anyio
async def test_pma_native_input_items_route_through_managed_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")
    captured_input_items: list[Optional[list[dict[str, Any]]]] = []

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
            )
            captured_input_items.append(input_items)
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="telegram managed attachment reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="review this image",
        date=None,
        is_topic_message=True,
    )
    input_items = [
        {"type": "text", "text": "review this image"},
        {"type": "localImage", "path": str(image_path)},
    ]

    await handler._handle_normal_message(
        message,
        runtime=_RuntimeStub(),
        input_items=input_items,
    )

    assert captured_input_items and isinstance(captured_input_items[0], list)
    items = captured_input_items[0] or []
    assert items and items[0].get("type") == "text"
    assert "<user_message>" in str(items[0].get("text") or "")
    assert any(item.get("type") == "localImage" for item in items[1:])
    assert "telegram managed attachment reply" in handler._sent

    thread_store = PmaThreadStore(tmp_path)
    threads = thread_store.list_threads(limit=10)
    assert len(threads) == 1
    turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
    assert len(turns) == 1
    assert turns[0]["status"] == "ok"


@pytest.mark.anyio
async def test_pma_interrupt_uses_managed_thread_orchestration_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = "telegram-backend-turn-1"
            if release_first.is_set():
                turn_id = "telegram-backend-turn-2"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active PMA turn. Cancelled 1 queued PMA turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        release_first.set()
        await first_task

        assert len(harness.interrupt_calls) == 1
        assert harness.interrupt_calls[0][1:] == (
            "telegram-backend-thread-1",
            "telegram-backend-turn-1",
        )
        assert (
            "Interrupted active PMA turn. Cancelled 1 queued PMA turn(s)."
            in handler._sent
        )
        assert "unexpected queued reply" not in handler._sent

        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["interrupted", "interrupted"]
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_interrupt_recovers_missing_backend_thread_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = "telegram-backend-turn-1"
            if release_first.is_set():
                turn_id = "telegram-backend-turn-2"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="error",
                    assistant_text="",
                    errors=["stale backend thread"],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            raise CodexAppServerResponseError(
                method="turn/interrupt",
                code=-32600,
                message="thread not found: telegram-backend-thread-1",
                data=None,
            )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._process_interrupt(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while not any(
                "Recovered stale PMA session" in sent for sent in handler._sent
            ):
                await anyio.sleep(0.05)
        release_first.set()
        await first_task

        assert harness.interrupt_calls == [
            (tmp_path, "telegram-backend-thread-1", "telegram-backend-turn-1")
        ]
        assert any(
            "Recovered stale PMA session after backend thread was lost." in sent
            for sent in handler._sent
        )
        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert turns[0]["status"] == "interrupted"
        assert turns[0]["error"] is None
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_repo_text_turns_use_orchestration_binding_and_preserve_thread_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
        active_thread_id="stale-active-thread",
        thread_ids=["stale-active-thread"],
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.start_calls: list[tuple[str, str]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            assert workspace_root == tmp_path

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            assert workspace_root == tmp_path
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = model, reasoning, approval_mode, sandbox_policy, input_items
            assert workspace_root == tmp_path
            self.start_calls.append((conversation_id, prompt))
            turn_id = f"repo-backend-turn-{len(self.start_calls)}"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, timeout
            assert conversation_id == "repo-backend-thread-1"
            assert isinstance(turn_id, str)
            return SimpleNamespace(
                status="ok",
                assistant_text=f"reply for {turn_id}",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    await handler._handle_normal_message(second_message, runtime=_RuntimeStub())

    assert harness.start_calls == [
        ("repo-backend-thread-1", "first repo orchestration prompt"),
        ("repo-backend-thread-1", "second repo orchestration prompt"),
    ]
    assert handler._client.thread_start_calls == []
    assert record.active_thread_id == "repo-backend-thread-1"
    assert record.thread_ids[0] == "repo-backend-thread-1"
    assert "reply for repo-backend-turn-1" in handler._sent
    assert "reply for repo-backend-turn-2" in handler._sent

    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="-1001:101",
    )
    assert binding is not None
    assert binding.mode == "repo"
    thread = orchestration_service.get_thread_target(binding.thread_target_id)
    assert thread is not None
    assert thread.backend_thread_id == "repo-backend-thread-1"


@pytest.mark.anyio
async def test_repo_media_turns_preserve_input_items_via_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.input_items: Optional[list[dict[str, Any]]] = None

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-media-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
            )
            self.input_items = input_items
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="repo-media-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="repo media orchestration reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="caption text",
        date=None,
        is_topic_message=True,
    )
    input_items = [
        {"type": "text", "text": "caption text"},
        {"type": "localImage", "path": str(image_path)},
    ]

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=_RuntimeStub(),
        text_override="caption text",
        input_items=input_items,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert result.response in ("repo media orchestration reply", "")
    assert harness.input_items == input_items
    assert handler._client.thread_start_calls == []
    assert record.active_thread_id == "repo-media-thread-1"


@pytest.mark.anyio
async def test_repo_interrupt_uses_orchestration_binding_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = "repo-backend-turn-1"
            if release_first.is_set():
                turn_id = "repo-backend-turn-2"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            if turn_id == "repo-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued repo reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            reply_to=first_message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active turn. Cancelled 1 queued turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        release_first.set()
        await first_task

        assert handler._client.thread_start_calls == []
        assert len(harness.interrupt_calls) == 1
        assert harness.interrupt_calls[0][1:] == (
            "repo-backend-thread-1",
            "repo-backend-turn-1",
        )
        assert "Interrupted active turn. Cancelled 1 queued turn(s)." in handler._sent
        assert "unexpected queued repo reply" not in handler._sent
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_repo_message_ingress_callback_reaches_orchestrated_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )

    class _RepoIngressHandler(_ManagedThreadPMAHandler):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            super().__init__(record, hub_root)
            self._router.runtime_for = lambda _key: _RuntimeStub()  # type: ignore[attr-defined]
            self._pending_questions = {}
            self._resume_options = {}
            self._bind_options = {}
            self._flow_run_options = {}
            self._agent_options = {}
            self._model_options = {}
            self._model_pending = {}
            self._review_commit_options = {}
            self._review_commit_subjects = {}
            self._pending_review_custom = {}
            self._ticket_flow_pause_targets = {}
            self._bot_username = None
            self._command_specs = {}
            self._config.trigger_mode = "all"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            _ = preferred_run_id
            return None

        def _enqueue_topic_work(self, _key: str, work):  # type: ignore[no-untyped-def]
            asyncio.get_running_loop().create_task(work())

        def _wrap_placeholder_work(self, **kwargs):  # type: ignore[no-untyped-def]
            return kwargs["work"]

        def _handle_pending_resume(self, *_args, **_kwargs) -> bool:
            return False

        def _handle_pending_bind(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_commit(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_custom(self, *_args, **_kwargs) -> bool:
            return False

        async def _dismiss_review_custom_prompt(self, *_args, **_kwargs) -> None:
            return None

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-ingress-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="repo-ingress-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="repo ingress orchestration reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    handler = _RepoIngressHandler(record, tmp_path)
    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            await kwargs["submit_thread_message"](request)
            return SimpleNamespace(route="thread", thread_result=None)

    import codex_autorunner.integrations.telegram.handlers.surface_ingress as _si_mod2

    monkeypatch.setattr(
        _si_mod2, "build_surface_orchestration_ingress", lambda **_: _IngressStub()
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text="hello from ingress",
        date=None,
        is_topic_message=True,
    )
    await telegram_messages_module.handle_message_inner(handler, message)
    with anyio.fail_after(5):
        while "repo ingress orchestration reply" not in handler._sent:
            await anyio.sleep(0.05)

    assert "repo ingress orchestration reply" in handler._sent
    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="111:222",
    )
    assert binding is not None
    assert binding.mode == "repo"


@pytest.mark.anyio
async def test_repo_message_ingress_callback_reaches_hermes_orchestrated_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="hermes",
    )

    class _RepoIngressHandler(_ManagedThreadPMAHandler):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            super().__init__(record, hub_root)
            self._router.runtime_for = lambda _key: _RuntimeStub()  # type: ignore[attr-defined]
            self._pending_questions = {}
            self._resume_options = {}
            self._bind_options = {}
            self._flow_run_options = {}
            self._agent_options = {}
            self._model_options = {}
            self._model_pending = {}
            self._review_commit_options = {}
            self._review_commit_subjects = {}
            self._pending_review_custom = {}
            self._ticket_flow_pause_targets = {}
            self._bot_username = None
            self._command_specs = {}
            self._config.trigger_mode = "all"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            _ = preferred_run_id
            return None

        def _enqueue_topic_work(self, _key: str, work):  # type: ignore[no-untyped-def]
            asyncio.get_running_loop().create_task(work())

        def _wrap_placeholder_work(self, **kwargs):  # type: ignore[no-untyped-def]
            return kwargs["work"]

        def _handle_pending_resume(self, *_args, **_kwargs) -> bool:
            return False

        def _handle_pending_bind(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_commit(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_custom(self, *_args, **_kwargs) -> bool:
            return False

        async def _dismiss_review_custom_prompt(self, *_args, **_kwargs) -> None:
            return None

    class _FakeHarness:
        display_name = "Hermes"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
                "approvals",
                "active_thread_discovery",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-hermes-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="hermes-repo-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="hermes-repo-turn-1",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="hermes repo ingress reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    handler = _RepoIngressHandler(record, tmp_path)
    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "hermes": AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            await kwargs["submit_thread_message"](request)
            return SimpleNamespace(route="thread", thread_result=None)

    import codex_autorunner.integrations.telegram.handlers.surface_ingress as _si_mod3

    monkeypatch.setattr(
        _si_mod3, "build_surface_orchestration_ingress", lambda **_: _IngressStub()
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text="hello from hermes ingress",
        date=None,
        is_topic_message=True,
    )

    await telegram_messages_module.handle_message_inner(handler, message)
    with anyio.fail_after(5):
        while "hermes repo ingress reply" not in handler._sent:
            await anyio.sleep(0.05)

    assert "hermes repo ingress reply" in handler._sent
    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="111:222",
    )
    assert binding is not None
    assert binding.agent_id == "hermes"
    assert binding.mode == "repo"


@pytest.mark.anyio
async def test_repo_interrupt_uses_orchestration_binding_for_hermes_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="hermes",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Hermes"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
                "approvals",
                "active_thread_discovery",
            }
        )

        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-hermes-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="hermes-fresh-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = "hermes-turn-1"
            if release_first.is_set():
                turn_id = "hermes-turn-2"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            if turn_id == "hermes-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected hermes queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "hermes": AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible hermes prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued hermes prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            reply_to=first_message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active turn. Cancelled 1 queued turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        release_first.set()
        await first_task

        assert handler._client.thread_start_calls == []
        assert len(harness.interrupt_calls) == 1
        assert harness.interrupt_calls[0][1:] == ("hermes-fresh-1", "hermes-turn-1")
        assert "Interrupted active turn. Cancelled 1 queued turn(s)." in handler._sent
        assert "unexpected hermes queued reply" not in handler._sent
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_missing_thread_resets_registry_and_recovers(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    registry.set_thread_id(PMA_KEY, "stale")
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        model="gpt-5.1-codex-max",
    )
    client = _PMAClientStub()
    handler = _PMAHandler(record, client, tmp_path, registry)
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=10587,
        from_user_id=42,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=_RuntimeStub(),
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert client.turn_start_calls[0] == "stale"
    assert client.turn_start_calls[-1] != "stale"
    assert registry.get_thread_id(PMA_KEY) != "stale"
