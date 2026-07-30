"""Typing-only shared contract for TelegramBotService mixins.

``TelegramBotService`` combines many mixins (transport, runtime, notification,
approval, question, selection, and command handlers). Each mixin freely uses
instance attributes that are really defined on sibling mixins or set in
``TelegramBotService.__init__``. mypy checks each mixin in isolation and cannot
see those cross-mixin attributes, so it reports a flood of ``[attr-defined]``
errors.

``_TelegramServiceAttrs`` declares that shared instance contract in one place.
Every mixin base inherits it, so ``self.<attr>`` resolves. It carries only
annotations -- no ``__init__``, no method bodies -- so it is runtime-free.

Method types use ``Callable[..., T]`` (async => ``Callable[..., Coroutine[Any,
Any, T]]``): the ``...`` matches any argument list a real definition may use,
so a mixin that also *defines* the method overrides the declaration cleanly; the
return type is kept precise so callers type-check and ``Any`` does not leak.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

if TYPE_CHECKING:
    from ...core.flows.models import FlowRunRecord
    from ...core.text_delta_coalescer import TextDeltaCoalescer
    from ...voice import VoiceConfig, VoiceService
    from ..agents.codex_backend import ApprovalDecision
    from ..app_server.client import CodexAppServerClient
    from ..app_server.supervisor import WorkspaceAppServerSupervisor
    from ..chat.update_notifier import ChatUpdateStatusNotifier
    from .client import TelegramBotClient
    from .config import TelegramBotConfig
    from .constants import TurnKey
    from .handlers.commands_spec import CommandSpec
    from .outbox import TelegramOutboxManager
    from .progress_stream import TurnProgressTracker
    from .state import TelegramStateStore
    from .state_types import TelegramTopicRecord
    from .topic_router import TopicRouter
    from .types import (
        CompactState,
        DocumentBrowserState,
        ModelPendingState,
        ModelPickerState,
        PendingApproval,
        PendingQuestion,
        ReviewCommitSelectionState,
        SelectionState,
        TurnContext,
        UpdateConfirmState,
    )
    from .ui_state import TelegramUiState
    from .voice import TelegramVoiceManager


class _TelegramServiceAttrs:
    # ------------------------------------------------------------------
    # Core service attributes (set in TelegramBotService.__init__).
    # ------------------------------------------------------------------
    _config: TelegramBotConfig
    _logger: logging.Logger
    _router: TopicRouter
    _store: TelegramStateStore
    _bot: TelegramBotClient
    _hub_root: Optional[Path]
    _manifest_path: Optional[Path]
    _voice_config: Optional[VoiceConfig]
    _voice_service: Optional[VoiceService]
    _voice_manager: Optional[TelegramVoiceManager]
    _ui_state: TelegramUiState
    _app_server_supervisor: WorkspaceAppServerSupervisor
    _outbox_manager: TelegramOutboxManager
    _update_status_notifier: ChatUpdateStatusNotifier
    _command_specs: dict[str, CommandSpec]
    _update_repo_url: Optional[str]
    _update_repo_ref: Optional[str]

    # ------------------------------------------------------------------
    # Ephemeral UI / selection state (mirrored from self._ui_state plus
    # service-local turn bookkeeping; all set in __init__).
    # ------------------------------------------------------------------
    _pending_questions: dict[str, PendingQuestion]
    _resume_options: dict[str, SelectionState]
    _bind_options: dict[str, SelectionState]
    _flow_run_options: dict[str, SelectionState]
    _update_options: dict[str, SelectionState]
    _update_confirm_options: dict[str, UpdateConfirmState]
    _review_commit_options: dict[str, ReviewCommitSelectionState]
    _review_commit_subjects: dict[str, dict[str, str]]
    _pending_review_custom: dict[str, dict[str, Any]]
    _compact_pending: dict[str, CompactState]
    _agent_options: dict[str, SelectionState]
    _agent_profile_options: dict[str, SelectionState]
    _model_options: dict[str, ModelPickerState]
    _model_pending: dict[str, ModelPendingState]
    _document_browser_states: dict[str, DocumentBrowserState]
    _pending_approvals: dict[str, PendingApproval]
    _oversize_warnings: set[TurnKey]
    _ticket_flow_pause_targets: dict[str, str]
    _model_catalog_cache: dict[str, tuple[Any, float]]

    # ------------------------------------------------------------------
    # Turn / progress bookkeeping (keyed by TurnKey).
    # ------------------------------------------------------------------
    _turn_contexts: dict[TurnKey, TurnContext]
    _turn_preview_text: dict[TurnKey, str]
    _turn_preview_updated_at: dict[TurnKey, float]
    _turn_progress_trackers: dict[TurnKey, TurnProgressTracker]
    _turn_progress_updated_at: dict[TurnKey, float]
    _turn_progress_rendered: dict[TurnKey, str]
    _turn_progress_locks: dict[TurnKey, asyncio.Lock]
    _turn_progress_tasks: dict[TurnKey, asyncio.Task[None]]
    _turn_progress_heartbeat_tasks: dict[TurnKey, asyncio.Task[None]]
    _reasoning_buffers: dict[str, TextDeltaCoalescer]
    _token_usage_by_turn: OrderedDict[str, dict[str, Any]]
    _token_usage_by_thread: OrderedDict[str, dict[str, Any]]

    # ------------------------------------------------------------------
    # Borrowed methods -- messaging/transport (TelegramMessageTransport).
    # ------------------------------------------------------------------
    _send_message: Callable[..., Coroutine[Any, Any, Optional[int]]]
    _send_message_with_outbox: Callable[..., Coroutine[Any, Any, bool]]
    _send_placeholder: Callable[..., Coroutine[Any, Any, Optional[int]]]
    _send_document: Callable[..., Coroutine[Any, Any, bool]]
    _send_voice_transcript_message: Callable[..., Coroutine[Any, Any, Optional[int]]]
    _send_voice_progress_message: Callable[..., Coroutine[Any, Any, Optional[int]]]
    _format_voice_transcript_message: Callable[..., str]
    _finalize_voice_transcript: Callable[..., Coroutine[Any, Any, None]]
    _edit_message_text: Callable[..., Coroutine[Any, Any, bool]]
    _delete_message: Callable[..., Coroutine[Any, Any, bool]]
    _edit_callback_message: Callable[..., Coroutine[Any, Any, bool]]
    _answer_callback: Callable[..., Coroutine[Any, Any, None]]
    _deliver_turn_response: Callable[..., Coroutine[Any, Any, bool]]
    _format_turn_metrics_text: Callable[..., Optional[str]]
    _metrics_mode: Callable[..., str]
    _send_turn_metrics: Callable[..., Coroutine[Any, Any, bool]]
    _append_metrics_to_placeholder: Callable[..., Coroutine[Any, Any, bool]]
    _render_message: Callable[..., tuple[str, Optional[str]]]
    _prepare_message: Callable[..., tuple[str, Optional[str]]]
    _prepare_outgoing_text: Callable[..., tuple[str, Optional[str]]]
    _build_debug_prefix: Callable[..., str]

    # ------------------------------------------------------------------
    # Borrowed methods -- workspace/turn (TelegramWorkspaceAndTurnMixin).
    # ------------------------------------------------------------------
    _resolve_topic_key: Callable[..., Coroutine[Any, Any, str]]
    _canonical_workspace_root: Callable[[Optional[str]], Optional[Path]]
    _client_for_workspace: Callable[
        ..., Coroutine[Any, Any, Optional[CodexAppServerClient]]
    ]
    _resolve_turn_key: Callable[..., Optional[TurnKey]]
    _resolve_turn_context: Callable[..., Optional[TurnContext]]
    _register_turn_context: Callable[..., bool]
    _workspace_id_for_path: Callable[..., Optional[str]]
    _refresh_workspace_id: Callable[..., Coroutine[Any, Any, Optional[str]]]
    _resolve_workspace_path: Callable[..., tuple[Optional[str], Optional[str]]]
    _record_with_workspace_path: Callable[..., Optional[TelegramTopicRecord]]
    _clear_thinking_preview: Callable[..., None]
    _topic_scope_id: Callable[..., Optional[str]]

    # ------------------------------------------------------------------
    # Borrowed methods -- workspace command handlers.
    # ------------------------------------------------------------------
    _require_bound_record: Callable[
        ..., Coroutine[Any, Any, Optional[TelegramTopicRecord]]
    ]
    _resolve_workspace: Callable[
        ..., Optional[tuple[str, Optional[str], Optional[str], Optional[str]]]
    ]
    _ensure_thread_id: Callable[..., Coroutine[Any, Any, Optional[str]]]
    _require_thread_workspace: Callable[..., Coroutine[Any, Any, bool]]
    _handle_thread_conflict: Callable[..., Coroutine[Any, Any, None]]
    _find_thread_conflict: Callable[..., Coroutine[Any, Any, Optional[str]]]
    _verify_active_thread: Callable[
        ..., Coroutine[Any, Any, Optional[TelegramTopicRecord]]
    ]
    _apply_agent_change: Callable[..., Coroutine[Any, Any, str]]
    _apply_thread_result: Callable[..., Coroutine[Any, Any, TelegramTopicRecord]]
    _effective_agent: Callable[..., str]
    _effective_runtime_agent: Callable[..., str]
    _effective_agent_label: Callable[..., str]
    _effective_agent_state: Callable[..., tuple[str, Optional[str]]]
    _effective_agent_label_from_values: Callable[..., str]
    _effective_agent_profile: Callable[..., Optional[str]]
    _effective_policies: Callable[..., tuple[Optional[str], Optional[Any]]]
    _agent_display_name: Callable[..., str]
    _agent_rate_limit_source: Callable[..., Optional[str]]
    _agents_supporting_capability: Callable[..., list[str]]
    _agent_supports_effort: Callable[..., bool]
    _agent_supports_resume: Callable[..., bool]
    _list_manifest_repos: Callable[..., list[str]]
    _opencode_available: Callable[..., bool]
    _resolve_opencode_model_context_window: Callable[
        ..., Coroutine[Any, Any, Optional[int]]
    ]
    _send_agent_profile_picker: Callable[..., Coroutine[Any, Any, None]]

    # ------------------------------------------------------------------
    # Borrowed methods -- selection handlers / keyboards.
    # ------------------------------------------------------------------
    _finalize_selection: Callable[..., Coroutine[Any, Any, None]]
    _selection_belongs_to_user: Callable[..., bool]
    _selection_resume_thread_by_id: Callable[..., Coroutine[Any, Any, None]]
    _selection_bind_topic_by_repo_id: Callable[..., Coroutine[Any, Any, None]]
    _update_selection_message: Callable[..., Coroutine[Any, Any, None]]
    _prompt_update_selection_from_callback: Callable[..., Coroutine[Any, Any, None]]
    _prompt_update_confirmation: Callable[..., Coroutine[Any, Any, None]]
    _build_review_commit_keyboard: Callable[..., dict[str, Any]]
    _build_resume_keyboard: Callable[..., dict[str, Any]]
    _build_model_keyboard: Callable[..., dict[str, Any]]
    _build_bind_keyboard: Callable[..., dict[str, Any]]
    _build_update_keyboard: Callable[..., dict[str, Any]]
    _build_flow_runs_keyboard: Callable[..., dict[str, Any]]

    # ------------------------------------------------------------------
    # Borrowed methods -- approvals / questions / flows.
    # ------------------------------------------------------------------
    _read_rate_limits: Callable[..., Coroutine[Any, Any, Optional[dict[str, Any]]]]
    _handle_approval_request: Callable[..., Coroutine[Any, Any, ApprovalDecision]]
    _handle_question_request: Callable[
        ..., Coroutine[Any, Any, Optional[list[list[str]]]]
    ]
    _handle_flow_callback: Callable[..., Coroutine[Any, Any, None]]
    _get_paused_ticket_flow: Callable[..., Optional[tuple[str, FlowRunRecord]]]
    _is_missing_opencode_session_error: Callable[..., bool]
    _start_review: Callable[..., Coroutine[Any, Any, None]]
    _start_update: Callable[..., Coroutine[Any, Any, None]]

    # ------------------------------------------------------------------
    # Borrowed methods -- notifications / progress.
    # ------------------------------------------------------------------
    _handle_normal_message: Callable[..., Coroutine[Any, Any, None]]
    _clear_turn_progress: Callable[..., None]
    _start_turn_progress: Callable[..., Coroutine[Any, Any, None]]
    _apply_run_event_to_progress: Callable[..., Coroutine[Any, Any, None]]
    _note_progress_context_usage: Callable[..., Coroutine[Any, Any, None]]
    _turn_progress_heartbeat: Callable[..., Coroutine[Any, Any, None]]
    _cache_token_usage: Callable[..., None]
    _flush_outbox_files: Callable[..., Coroutine[Any, Any, None]]
    _has_active_turns: Callable[..., bool]

    # ------------------------------------------------------------------
    # Borrowed methods -- execution / lifecycle (TelegramBotService).
    # ------------------------------------------------------------------
    _spawn_task: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]]
    _touch_cache_timestamp: Callable[[str, object], None]
    _current_chat_operation_id: Callable[[], Optional[str]]
    _mark_chat_operation_state: Callable[..., Coroutine[Any, Any, None]]
    _enqueue_topic_work: Callable[..., None]
    _interrupt_timeout_check: Callable[..., Coroutine[Any, Any, None]]
    _dispatch_interrupt_request: Callable[..., Coroutine[Any, Any, None]]
    _ensure_turn_semaphore: Callable[..., asyncio.Semaphore]
    _set_queued_placeholder: Callable[..., None]
    _write_user_reply_from_telegram: Callable[
        ..., Coroutine[Any, Any, tuple[bool, str]]
    ]
    _with_conversation_id: Callable[..., str]
    _wait_for_turn_result: Callable[..., Coroutine[Any, Any, Any]]
    _await_turn_slot: Callable[..., Coroutine[Any, Any, bool]]

    # ------------------------------------------------------------------
    # Borrowed methods -- selections / prompts / keyboards (extra).
    # ------------------------------------------------------------------
    _selection_prompt: Callable[..., str]
    _flow_runs_prompt: Callable[..., str]
    _hermes_profile_options: Callable[..., tuple[Any, ...]]
    _interrupt_keyboard: Callable[[], dict[str, Any]]

    # ------------------------------------------------------------------
    # Borrowed methods -- workspace / files paths.
    # ------------------------------------------------------------------
    _thread_start_kwargs: Callable[..., dict[str, Any]]
    _process_monitor_root: Callable[..., Optional[Path]]
    _turn_key: Callable[..., Optional[TurnKey]]
    _files_inbox_dir: Callable[..., Path]
    _files_topic_dir: Callable[..., Path]
    _files_outbox_pending_dir: Callable[..., Path]
    _files_outbox_sent_dir: Callable[..., Path]
    _pma_inbox_dir: Callable[[], Optional[Path]]
    _pma_outbox_dir: Callable[[], Optional[Path]]
    _parse_command_args: Callable[..., list[str]]
