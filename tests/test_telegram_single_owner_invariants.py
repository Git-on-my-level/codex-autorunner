"""
Characterization tests that freeze Telegram ingress, queue, collaboration,
handshake, pause-notice, and session-recovery invariants before deeper
ownership changes in the 1600 band.

These tests exist to detect regressions in the shipped behavior of:
- personal DM versus collaborative-topic admission
- ordinary-turn routing through shared orchestration ingress
- topic-queue bypass rules for interrupts and allow_during_turn commands
- queued placeholder and progress delivery behavior
- ticket-flow pause notice delivery and best-effort auto-resume
- hub handshake compatibility versus unavailability
- session-recovery notice behavior after runtime restart

Treat this as the guardrail module for the 1600 ticket band.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramForwardOrigin,
    TelegramMessage,
    TelegramPhotoSize,
)
from codex_autorunner.integrations.telegram.config import PauseDispatchNotifications
from codex_autorunner.integrations.telegram.handlers import (
    messages as telegram_messages_module,
)
from codex_autorunner.integrations.telegram.handlers.messages import (
    should_bypass_topic_queue,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord
from codex_autorunner.integrations.telegram.ticket_flow_bridge import (
    TelegramTicketFlowBridge,
)
from tests.fixtures.telegram_command_helpers import (
    bot_command_entity,
    make_command_spec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message(**kwargs: object) -> TelegramMessage:
    text = kwargs.pop("text", None)
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text=text,
        date=0,
        is_topic_message=False,
        **kwargs,
    )


class _DummyRecord:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = str(workspace_path)
        self.last_ticket_dispatch_seq = None
        self.last_terminal_run_id = None
        self.last_terminal_finished_at = None
        self.last_active_at = None


class _DummyStore:
    def __init__(self, topics: dict[str, _DummyRecord]) -> None:
        self._topics = topics

    async def list_topics(self) -> dict[str, _DummyRecord]:
        return self._topics

    async def update_topic(self, key: str, fn: Any) -> None:
        fn(self._topics[key])


def _make_bridge(
    tmp_path: Path,
    *,
    topics: dict[str, _DummyRecord] | None = None,
    default_chat_id: Optional[int] = None,
    pause_config: PauseDispatchNotifications | None = None,
    hub_root: Path | None = None,
    hub_raw_config: dict[str, Any] | None = None,
) -> tuple[TelegramTicketFlowBridge, list[tuple[int, str, int | None]], list[str]]:
    calls: list[tuple[int, str, int | None]] = []
    docs: list[str] = []

    async def send_message_with_outbox(
        chat_id: int,
        text: str,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
    ) -> bool:
        calls.append((chat_id, text, thread_id))
        return True

    async def send_document(
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        caption: Optional[str] = None,
    ) -> bool:
        docs.append(filename)
        return True

    bridge = TelegramTicketFlowBridge(
        logger=__import__("logging").getLogger("test"),
        store=_DummyStore(topics or {}),
        pause_targets={},
        send_message_with_outbox=send_message_with_outbox,
        send_document=send_document,
        pause_config=pause_config
        or PauseDispatchNotifications(
            enabled=True,
            send_attachments=True,
            max_file_size_bytes=50 * 1024 * 1024,
            chunk_long_messages=False,
        ),
        default_notification_chat_id=default_chat_id,
        hub_root=hub_root,
        manifest_path=None,
        config_root=tmp_path,
        hub_raw_config=hub_raw_config,
    )
    return bridge, calls, docs


# ===================================================================
# 1. Collaboration / admission invariants
# ===================================================================


class TestCollaborationAdmissionInvariants:
    """Pin down the collaboration policy evaluation outcomes for DM vs topic paths."""

    def test_dm_message_always_passes_collaboration_as_non_command(
        self, tmp_path: Path
    ) -> None:
        """
        A private chat message without any collaboration policy overlay must
        evaluate as a non-command with should_start_turn=True (the DM easy path).
        """
        from codex_autorunner.integrations.telegram.handlers.messages import (
            _evaluate_message_policy,
        )

        policy_flags: list[bool] = []

        def _eval_policy(
            _msg: TelegramMessage, *, text: str, is_explicit_command: bool
        ) -> SimpleNamespace:
            policy_flags.append(is_explicit_command)
            return SimpleNamespace(command_allowed=True, should_start_turn=True)

        handlers = types.SimpleNamespace(
            _evaluate_collaboration_message_policy=_eval_policy,
            _config=SimpleNamespace(trigger_mode="all"),
        )
        message = _message(text="hello dm", chat_type="private")
        result = _evaluate_message_policy(
            handlers, message, text="hello dm", is_explicit_command=False
        )

        assert policy_flags == [False]
        assert result.should_start_turn is True
        assert result.command_allowed is True

    def test_supergroup_text_evaluates_collaboration_for_non_command(self) -> None:
        from codex_autorunner.integrations.telegram.handlers.messages import (
            _evaluate_message_policy,
        )

        policy_flags: list[bool] = []

        def _eval_policy(
            _msg: TelegramMessage, *, text: str, is_explicit_command: bool
        ) -> SimpleNamespace:
            policy_flags.append(is_explicit_command)
            return SimpleNamespace(command_allowed=False, should_start_turn=False)

        handlers = types.SimpleNamespace(
            _evaluate_collaboration_message_policy=_eval_policy,
            _config=SimpleNamespace(trigger_mode="all"),
        )
        message = _message(text="hello team", chat_type="supergroup")
        result = _evaluate_message_policy(
            handlers, message, text="hello team", is_explicit_command=False
        )

        assert policy_flags == [False]
        assert result.should_start_turn is False
        assert result.command_allowed is False

    def test_explicit_command_evaluates_collaboration_with_is_command_flag(
        self,
    ) -> None:
        from codex_autorunner.integrations.telegram.handlers.messages import (
            _evaluate_message_policy,
        )

        policy_flags: list[bool] = []

        def _eval_policy(
            _msg: TelegramMessage, *, text: str, is_explicit_command: bool
        ) -> SimpleNamespace:
            policy_flags.append(is_explicit_command)
            return SimpleNamespace(command_allowed=True, should_start_turn=False)

        handlers = types.SimpleNamespace(
            _evaluate_collaboration_message_policy=_eval_policy,
            _config=SimpleNamespace(trigger_mode="all"),
        )
        message = _message(
            text="/status",
            entities=(bot_command_entity("/status"),),
            chat_type="supergroup",
        )
        result = _evaluate_message_policy(
            handlers, message, text="/status", is_explicit_command=True
        )

        assert policy_flags == [True]
        assert result.should_start_turn is False
        assert result.command_allowed is True

    def test_fallback_when_evaluator_not_callable_allows_commands(self) -> None:
        """
        When the collaboration evaluator is not callable, commands are allowed
        but should_start_turn stays False.
        """
        from codex_autorunner.integrations.telegram.handlers.messages import (
            _evaluate_message_policy,
        )

        handlers = types.SimpleNamespace(
            _evaluate_collaboration_message_policy=None,
            _config=SimpleNamespace(trigger_mode="all"),
        )
        message = _message(
            text="/status",
            entities=(bot_command_entity("/status"),),
        )
        result = _evaluate_message_policy(
            handlers, message, text="/status", is_explicit_command=True
        )
        assert result.command_allowed is True
        assert result.should_start_turn is False


# ===================================================================
# 2. Ordinary-turn routing through shared orchestration ingress
# ===================================================================


class TestOrdinaryTurnRoutingInvariants:
    """Pin that ordinary text and media turns route through shared orchestration ingress."""

    @pytest.mark.anyio
    async def test_non_pma_repo_text_routes_via_ingress(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        captured: dict[str, object] = {}

        class _RouterStub:
            async def get_topic(self, _key: str) -> TelegramTopicRecord:
                return TelegramTopicRecord(
                    workspace_path=str(workspace),
                    pma_enabled=False,
                    agent="codex",
                )

            def runtime_for(self, _key: str) -> object:
                return object()

        class _HandlerStub:
            def __init__(self) -> None:
                self._router = _RouterStub()
                self._logger = __import__("logging").getLogger("test")
                self._config = SimpleNamespace(trigger_mode="all")
                self._pending_questions: dict[str, object] = {}
                self._resume_options: dict[str, object] = {}
                self._bind_options: dict[str, object] = {}
                self._flow_run_options: dict[str, object] = {}
                self._agent_options: dict[str, object] = {}
                self._model_options: dict[str, object] = {}
                self._model_pending: dict[str, object] = {}
                self._review_commit_options: dict[str, object] = {}
                self._review_commit_subjects: dict[str, object] = {}
                self._pending_review_custom: dict[str, object] = {}
                self._ticket_flow_pause_targets: dict[str, str] = {}
                self._ticket_flow_bridge = SimpleNamespace(
                    auto_resume_run=lambda *a, **kw: None
                )
                self._bot_username = None
                self._command_specs: dict[str, object] = {}
                self._last_task: Optional[asyncio.Task[None]] = None

            async def _resolve_topic_key(
                self, chat_id: int, thread_id: Optional[int]
            ) -> str:
                return f"{chat_id}:{thread_id}"

            def _get_paused_ticket_flow(
                self, _workspace_root: Path, *, preferred_run_id: Optional[str]
            ) -> Optional[tuple[str, object]]:
                return None

            def _handle_pending_resume(self, *a: object, **kw: object) -> bool:
                return False

            def _handle_pending_bind(self, *a: object, **kw: object) -> bool:
                return False

            async def _handle_pending_review_commit(
                self, *a: object, **kw: object
            ) -> bool:
                return False

            async def _handle_pending_review_custom(
                self, *a: object, **kw: object
            ) -> bool:
                return False

            async def _dismiss_review_custom_prompt(
                self, *a: object, **kw: object
            ) -> None:
                return None

            def _enqueue_topic_work(self, key: str, work: Any) -> None:
                self._last_task = asyncio.create_task(work())

            def _wrap_placeholder_work(self, **kwargs: Any) -> Any:
                return kwargs["work"]

            async def _send_message(self, *a: object, **kw: object) -> None:
                return None

            async def _handle_normal_message(
                self,
                message: TelegramMessage,
                runtime: object,
                *,
                text_override: Optional[str] = None,
                placeholder_id: Optional[int] = None,
            ) -> None:
                captured["handled_text"] = text_override
                captured["message_id"] = message.message_id

        class _IngressStub:
            async def submit_message(
                self, request: Any, **kwargs: Any
            ) -> SimpleNamespace:
                captured["surface_kind"] = request.surface_kind
                captured["workspace_root"] = request.workspace_root
                captured["prompt_text"] = request.prompt_text
                await kwargs["submit_thread_message"](request)
                return SimpleNamespace(route="thread", thread_result=None)

        import codex_autorunner.integrations.telegram.handlers.surface_ingress as _si_mod

        monkeypatch.setattr(
            _si_mod,
            "build_surface_orchestration_ingress",
            lambda **_: _IngressStub(),
        )

        handler = _HandlerStub()
        message = TelegramMessage(
            update_id=1,
            message_id=10,
            chat_id=123,
            thread_id=456,
            from_user_id=789,
            text="route through ingress",
            date=None,
            is_topic_message=True,
        )
        await telegram_messages_module.handle_message_inner(handler, message)
        assert handler._last_task is not None
        await handler._last_task

        assert captured["surface_kind"] == "telegram"
        assert captured["prompt_text"] == "route through ingress"
        assert captured["workspace_root"] == workspace

    @pytest.mark.anyio
    async def test_non_pma_media_routes_via_ingress(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        captured: dict[str, object] = {}

        class _RouterStub:
            async def get_topic(self, _key: str) -> TelegramTopicRecord:
                return TelegramTopicRecord(
                    workspace_path=str(workspace),
                    pma_enabled=False,
                    agent="codex",
                )

        class _HandlerStub:
            def __init__(self) -> None:
                self._router = _RouterStub()
                self._logger = __import__("logging").getLogger("test")
                self._config = SimpleNamespace(
                    media=SimpleNamespace(
                        enabled=True,
                        images=True,
                        voice=True,
                        files=True,
                        max_image_bytes=10_000_000,
                        max_voice_bytes=10_000_000,
                        max_file_bytes=10_000_000,
                    )
                )
                self._ticket_flow_pause_targets: dict[str, str] = {}
                self._bot_username = None

            async def _resolve_topic_key(
                self, chat_id: int, thread_id: Optional[int]
            ) -> str:
                return f"{chat_id}:{thread_id}"

            def _get_paused_ticket_flow(
                self, _workspace_root: Path, *, preferred_run_id: Optional[str]
            ) -> Optional[tuple[str, object]]:
                return None

            async def _send_message(self, *a: object, **kw: object) -> None:
                return None

        class _IngressStub:
            async def submit_message(
                self, request: Any, **kwargs: Any
            ) -> SimpleNamespace:
                captured["surface_kind"] = request.surface_kind
                captured["prompt_text"] = request.prompt_text
                captured["workspace_root"] = request.workspace_root
                return SimpleNamespace(route="thread", thread_result=None)

        import codex_autorunner.integrations.telegram.handlers.media_ingress as _mi_mod

        monkeypatch.setattr(
            _mi_mod,
            "build_surface_orchestration_ingress",
            lambda **_: _IngressStub(),
        )

        handler = _HandlerStub()
        message = TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=111,
            thread_id=222,
            from_user_id=333,
            text=None,
            date=None,
            is_topic_message=True,
            document=TelegramDocument(
                file_id="file-1",
                file_unique_id=None,
                file_name="report.txt",
                mime_type="text/plain",
                file_size=10,
            ),
            caption="see attached",
        )
        await telegram_messages_module.handle_media_message(
            handler, message, runtime=object(), caption_text="see attached"
        )

        assert captured["surface_kind"] == "telegram"
        assert captured["prompt_text"] == "see attached"
        assert captured["workspace_root"] == workspace


# ===================================================================
# 3. Topic-queue bypass rules
# ===================================================================


class TestTopicQueueBypassInvariants:
    """Pin the exact bypass conditions for the per-topic queue."""

    def test_interrupt_alias_bypasses_queue(self) -> None:
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={},
        )
        for alias in ("^C", "^c", "/stop", "ESC", "escape"):
            msg = _message(text=alias)
            assert (
                should_bypass_topic_queue(handlers, msg) is True
            ), f"alias {alias!r} should bypass"

    def test_allow_during_turn_command_bypasses_queue(self) -> None:
        spec = make_command_spec("status", "status", allow_during_turn=True)
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={"status": spec},
            _pending_questions={},
        )
        msg = _message(
            text="/status",
            entities=(bot_command_entity("/status"),),
        )
        assert should_bypass_topic_queue(handlers, msg) is True

    def test_command_without_allow_during_turn_does_not_bypass(self) -> None:
        spec = make_command_spec("new", "new", allow_during_turn=False)
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={"new": spec},
            _pending_questions={},
        )
        msg = _message(
            text="/new",
            entities=(bot_command_entity("/new"),),
        )
        assert should_bypass_topic_queue(handlers, msg) is False

    def test_pending_custom_question_answer_bypasses_queue(self) -> None:
        pending = types.SimpleNamespace(
            awaiting_custom_input=True,
            chat_id=3,
            thread_id=4,
        )
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={"req-1": pending},
        )
        msg = _message(text="my custom answer")
        assert should_bypass_topic_queue(handlers, msg) is True

    def test_pending_question_mismatched_chat_does_not_bypass(self) -> None:
        pending = types.SimpleNamespace(
            awaiting_custom_input=True,
            chat_id=99,
            thread_id=4,
        )
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={"req-1": pending},
        )
        msg = _message(text="my answer")
        assert should_bypass_topic_queue(handlers, msg) is False

    def test_forwarded_interrupt_never_bypasses_queue(self) -> None:
        spec = make_command_spec("status", "status", allow_during_turn=True)
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={"status": spec},
            _pending_questions={},
        )
        msg = _message(
            text="/status",
            entities=(bot_command_entity("/status"),),
            forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
        )
        assert should_bypass_topic_queue(handlers, msg) is False

    def test_forwarded_interrupt_alias_never_bypasses_queue(self) -> None:
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={},
        )
        msg = _message(
            text="^C",
            forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
        )
        assert should_bypass_topic_queue(handlers, msg) is False

    def test_plain_text_without_pending_questions_does_not_bypass(self) -> None:
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={},
        )
        msg = _message(text="hello team")
        assert should_bypass_topic_queue(handlers, msg) is False

    def test_media_without_text_does_not_bypass(self) -> None:
        handlers = types.SimpleNamespace(
            _bot_username="Bot",
            _command_specs={},
            _pending_questions={},
        )
        msg = _message(
            photos=(TelegramPhotoSize("p1", None, 800, 600, 3),),
        )
        assert should_bypass_topic_queue(handlers, msg) is False


# ===================================================================
# 4. Queued placeholder and progress delivery
# ===================================================================


class TestPlaceholderDeliveryInvariants:
    """Pin that queued placeholders are created, promoted, and deleted correctly."""

    @pytest.mark.anyio
    async def test_queued_placeholder_text_is_promoted_to_working(self) -> None:
        from codex_autorunner.integrations.telegram.constants import (
            PLACEHOLDER_TEXT,
            QUEUED_PLACEHOLDER_TEXT,
        )
        from tests.test_telegram_turn_queue import (
            _ClientStub,
            _HandlerStub,
            _record,
        )

        first_wait = asyncio.Event()
        second_wait = asyncio.Event()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        second_placeholder = asyncio.Event()

        client = _ClientStub(
            turn_wait_events=[first_wait, second_wait],
            turn_start_events=[first_started, second_started],
        )
        records = {
            "10:11": _record("thread-1"),
            "10:12": _record("thread-2"),
        }
        handler = _HandlerStub(
            client=client,
            max_parallel_turns=1,
            records=records,
            placeholder_events={2: second_placeholder},
        )

        from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
            _RuntimeStub,
        )
        from tests.test_telegram_turn_queue import _message as _qmsg

        runtime_one = _RuntimeStub()
        runtime_two = _RuntimeStub()
        task_one = asyncio.create_task(
            handler._run_turn_and_collect_result(
                _qmsg(message_id=1, thread_id=11),
                runtime_one,
                record=records["10:11"],
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        task_two = asyncio.create_task(
            handler._run_turn_and_collect_result(
                _qmsg(message_id=2, thread_id=12),
                runtime_two,
                record=records["10:12"],
            )
        )
        await asyncio.wait_for(second_placeholder.wait(), timeout=1.0)

        second_call = next(
            call for call in handler._placeholder_calls if call["reply_to"] == 2
        )
        assert second_call["text"] == QUEUED_PLACEHOLDER_TEXT

        first_wait.set()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        second_wait.set()
        await asyncio.gather(task_one, task_two)

        placeholder_id = handler._placeholder_ids[2]
        assert (placeholder_id, PLACEHOLDER_TEXT) in handler._edit_calls

    @pytest.mark.anyio
    async def test_successful_delivery_deletes_placeholder(self) -> None:
        from tests.test_telegram_turn_queue import _ClientStub, _HandlerStub, _record

        wait = asyncio.Event()
        wait.set()
        client = _ClientStub(turn_wait_events=[wait])
        records = {"10:11": _record("thread-1")}
        handler = _HandlerStub(
            client=client,
            max_parallel_turns=1,
            records=records,
        )

        from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
            _RuntimeStub,
        )
        from tests.test_telegram_turn_queue import _message as _qmsg

        message = _qmsg(message_id=1, thread_id=11)
        await handler._handle_normal_message(
            message, _RuntimeStub(), record=records["10:11"]
        )

        assert handler._deliver_calls
        assert handler._deliver_calls[-1]["delete_placeholder_on_delivery"] is True
        assert handler._delete_calls == []


# ===================================================================
# 5. Ticket-flow pause notice delivery and best-effort auto-resume
# ===================================================================


class TestPauseNoticeInvariants:
    """Pin that pause notices are delivered to the right target with rollback."""

    @pytest.mark.asyncio
    async def test_pause_notice_selects_matching_topic(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws-topic"
        workspace.mkdir()
        record_active = _DummyRecord(workspace)
        record_active.last_active_at = "2026-04-15T12:00:00Z"
        record_other = _DummyRecord(workspace)
        record_other.last_active_at = "2026-04-10T12:00:00Z"

        bridge, calls, docs = _make_bridge(
            tmp_path,
            topics={"123:456": record_active, "123:789": record_other},
        )
        bridge._load_ticket_flow_pause = lambda path: (  # type: ignore[assignment]
            "run1",
            "0001",
            "body text",
            None,
        )

        await bridge._notify_ticket_flow_pause(
            workspace,
            [("123:456", record_active), ("123:789", record_other)],
        )

        assert len(calls) >= 1
        sent_chat_id = calls[0][0]
        sent_thread_id = calls[0][2]
        assert sent_chat_id == 123
        assert sent_thread_id == 456

    @pytest.mark.asyncio
    async def test_pause_notice_marker_persists_on_send_false_return(
        self, tmp_path: Path
    ) -> None:
        """
        The rollback in _notify_ticket_flow_pause only triggers on exceptions
        (RuntimeError, OSError, ConnectionError), not on send_message_with_outbox
        returning False. The marker is optimistically set before the send attempt.
        """
        workspace = tmp_path / "ws-marker-persist"
        workspace.mkdir()
        record = _DummyRecord(workspace)

        send_count = 0

        async def send_returning_false(
            chat_id: int,
            text: str,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> bool:
            nonlocal send_count
            send_count += 1
            return False

        async def send_doc(**kw: object) -> bool:
            return True

        bridge = TelegramTicketFlowBridge(
            logger=__import__("logging").getLogger("test"),
            store=_DummyStore({"123:456": record}),
            pause_targets={},
            send_message_with_outbox=send_returning_false,
            send_document=send_doc,
            pause_config=PauseDispatchNotifications(
                enabled=True,
                send_attachments=False,
                max_file_size_bytes=50 * 1024 * 1024,
                chunk_long_messages=False,
            ),
            default_notification_chat_id=None,
            hub_root=None,
            manifest_path=None,
            config_root=workspace,
        )
        bridge._load_ticket_flow_pause = lambda path: (  # type: ignore[assignment]
            "run1",
            "0001",
            "body text",
            None,
        )

        await bridge._notify_ticket_flow_pause(workspace, [("123:456", record)])

        assert send_count >= 1
        assert record.last_ticket_dispatch_seq == "run1:0001"

    @pytest.mark.asyncio
    async def test_auto_resume_propagates_value_error_for_missing_run(
        self, tmp_path: Path
    ) -> None:
        """
        auto_resume_run catches ConfigError, OSError, RuntimeError, and
        ConnectionError, but ValueError (from a nonexistent run) propagates
        to the caller. This is a known compatibility behavior: callers
        should handle ValueError if the run might not exist.
        """
        workspace = tmp_path / "ws-resume"
        workspace.mkdir()
        seed_hub_files(workspace, force=True)
        (workspace / ".git").mkdir()
        seed_repo_files(workspace, git_required=False)
        bridge, _, _ = _make_bridge(tmp_path, topics={})

        bridge._load_ticket_flow_pause = lambda path: None  # type: ignore[assignment]

        with pytest.raises(ValueError, match="not found"):
            await bridge.auto_resume_run(workspace, "nonexistent-run")

    @pytest.mark.asyncio
    async def test_default_chat_dedup_across_scans(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws-dedup"
        workspace.mkdir()
        bridge, calls, _ = _make_bridge(
            tmp_path,
            topics={},
            default_chat_id=999,
        )
        bridge._load_ticket_flow_pause = lambda path: (  # type: ignore[assignment]
            "run2",
            "0002",
            "body",
            None,
        )

        await bridge._notify_via_default_chat(workspace)
        await bridge._notify_via_default_chat(workspace)

        assert len(calls) == 1


# ===================================================================
# 7. Session-recovery notice behavior
# ===================================================================


class TestSessionRecoveryInvariants:
    """Pin that session recovery notices are emitted when runtime bindings restart."""

    @pytest.mark.anyio
    async def test_repo_turn_emits_recovery_notice_on_binding_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When the runtime thread binding is cleared between turns (simulating a
        runtime restart), the second turn must emit a session-recovery notice
        prepended to the response.
        """
        from codex_autorunner.agents.registry import AgentDescriptor
        from codex_autorunner.core.orchestration.runtime_bindings import (
            clear_runtime_thread_binding,
        )
        from tests import telegram_pma_routing_support as support

        _SESSION_NOTICE = (
            "Notice: the previous live session was unavailable, "
            "so I started a new session."
        )

        record = support.TelegramTopicRecord(
            pma_enabled=False,
            workspace_path=str(tmp_path),
            repo_id="repo-inv-1",
            agent="codex",
        )
        handler = support._ManagedThreadPMAHandler(record, tmp_path)

        class _FakeHarness:
            display_name = "Fake"
            capabilities = frozenset(
                {"durable_threads", "message_turns", "interrupt", "event_streaming"}
            )

            def __init__(self) -> None:
                self.start_calls: list[tuple[str, str]] = []
                self._conversation_count = 0

            async def ensure_ready(self, workspace_root: Path) -> None:
                assert workspace_root == tmp_path

            async def backend_runtime_instance_id(
                self, workspace_root: Path
            ) -> Optional[str]:
                return "runtime-inv-1"

            def supports(self, capability: str) -> bool:
                return capability in self.capabilities

            async def new_conversation(
                self, workspace_root: Path, title: Optional[str] = None
            ) -> SimpleNamespace:
                self._conversation_count += 1
                return SimpleNamespace(
                    id=f"inv-backend-thread-{self._conversation_count}"
                )

            async def resume_conversation(
                self, workspace_root: Path, conversation_id: str
            ) -> SimpleNamespace:
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
                self.start_calls.append((conversation_id, prompt))
                turn_id = f"{conversation_id}:turn-{len(self.start_calls)}"
                return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

            async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
                raise AssertionError("review should not be used")

            async def wait_for_turn(
                self,
                workspace_root: Path,
                conversation_id: str,
                turn_id: Optional[str],
                *,
                timeout: Optional[float] = None,
            ) -> SimpleNamespace:
                return SimpleNamespace(
                    status="ok",
                    assistant_text=f"reply for {turn_id}",
                    errors=[],
                )

            async def interrupt(
                self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
            ) -> None:
                pass

            async def stream_events(
                self, workspace_root: Path, conversation_id: str, turn_id: str
            ):
                if False:
                    yield ""

        harness = _FakeHarness()
        monkeypatch.setattr(
            support.execution_commands_module,
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

        first_message = support.TelegramMessage(
            update_id=1,
            message_id=10,
            chat_id=-2001,
            thread_id=201,
            from_user_id=42,
            text="first prompt",
            date=None,
            is_topic_message=True,
        )
        second_message = support.TelegramMessage(
            update_id=2,
            message_id=11,
            chat_id=-2001,
            thread_id=201,
            from_user_id=42,
            text="second prompt after restart",
            date=None,
            is_topic_message=True,
        )

        await handler._handle_normal_message(
            first_message, runtime=support._RuntimeStub()
        )

        orchestration_service = support.execution_commands_module._build_telegram_thread_orchestration_service(
            handler
        )
        binding = orchestration_service.get_binding(
            surface_kind="telegram",
            surface_key="-2001:201",
        )
        assert binding is not None
        clear_runtime_thread_binding(tmp_path, binding.thread_target_id)

        await handler._handle_normal_message(
            second_message, runtime=support._RuntimeStub()
        )

        assert handler._sent[-1].startswith(_SESSION_NOTICE)
        assert "inv-backend-thread-2" in handler._sent[-1]
        assert record.active_thread_id == "inv-backend-thread-2"

    @pytest.mark.anyio
    async def test_consecutive_turns_without_restart_do_not_emit_recovery_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codex_autorunner.agents.registry import AgentDescriptor
        from tests import telegram_pma_routing_support as support

        _SESSION_NOTICE = (
            "Notice: the previous live session was unavailable, "
            "so I started a new session."
        )

        record = support.TelegramTopicRecord(
            pma_enabled=False,
            workspace_path=str(tmp_path),
            repo_id="repo-inv-2",
            agent="codex",
        )
        handler = support._ManagedThreadPMAHandler(record, tmp_path)

        class _FakeHarness:
            display_name = "Fake"
            capabilities = frozenset(
                {"durable_threads", "message_turns", "interrupt", "event_streaming"}
            )

            def __init__(self) -> None:
                self._conversation_count = 0

            async def ensure_ready(self, workspace_root: Path) -> None:
                pass

            async def backend_runtime_instance_id(
                self, workspace_root: Path
            ) -> Optional[str]:
                return "runtime-inv-2"

            def supports(self, capability: str) -> bool:
                return capability in self.capabilities

            async def new_conversation(
                self, workspace_root: Path, title: Optional[str] = None
            ) -> SimpleNamespace:
                self._conversation_count += 1
                return SimpleNamespace(id=f"inv2-thread-{self._conversation_count}")

            async def resume_conversation(
                self, workspace_root: Path, conversation_id: str
            ) -> SimpleNamespace:
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
                turn_id = f"{conversation_id}:turn-1"
                return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

            async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
                raise AssertionError("review should not be used")

            async def wait_for_turn(
                self,
                workspace_root: Path,
                conversation_id: str,
                turn_id: Optional[str],
                *,
                timeout: Optional[float] = None,
            ) -> SimpleNamespace:
                return SimpleNamespace(
                    status="ok",
                    assistant_text="normal reply",
                    errors=[],
                )

            async def interrupt(
                self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
            ) -> None:
                pass

            async def stream_events(
                self, workspace_root: Path, conversation_id: str, turn_id: str
            ):
                if False:
                    yield ""

        harness = _FakeHarness()
        monkeypatch.setattr(
            support.execution_commands_module,
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

        first_message = support.TelegramMessage(
            update_id=1,
            message_id=10,
            chat_id=-3001,
            thread_id=301,
            from_user_id=42,
            text="first",
            date=None,
            is_topic_message=True,
        )
        second_message = support.TelegramMessage(
            update_id=2,
            message_id=11,
            chat_id=-3001,
            thread_id=301,
            from_user_id=42,
            text="second",
            date=None,
            is_topic_message=True,
        )

        await handler._handle_normal_message(
            first_message, runtime=support._RuntimeStub()
        )
        await handler._handle_normal_message(
            second_message, runtime=support._RuntimeStub()
        )

        assert not any(sent.startswith(_SESSION_NOTICE) for sent in handler._sent)
