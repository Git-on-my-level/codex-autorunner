from __future__ import annotations

import asyncio
import collections
import contextvars
import json
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional, Sequence

if TYPE_CHECKING:
    from .progress_stream import TurnProgressTracker
    from .state import TelegramTopicRecord

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.config import load_hub_config, load_repo_config
from ...core.config_contract import ConfigError
from ...core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ...core.flows.models import FlowRunRecord
from ...core.flows.pause_dispatch import format_pause_reason, latest_dispatch_seq
from ...core.hub_control_plane import (
    HandshakeCompatibility,
    HttpHubControlPlaneClient,
    HubControlPlaneError,
)
from ...core.hub_control_plane.handshake_startup import perform_startup_hub_handshake
from ...core.hub_control_plane.service import (
    CONTROL_PLANE_API_VERSION as _CONTROL_PLANE_API_VERSION,
)
from ...core.locks import FileLock, FileLockBusy
from ...core.logging_utils import log_event
from ...core.orchestration import (
    ORCHESTRATION_SCHEMA_VERSION,
    ChatOperationState,
    SQLiteChatOperationLedger,
)
from ...core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
)
from ...core.orchestration.managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryEngine,
)
from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.runtime_services import RuntimeServices
from ...core.state import now_iso
from ...core.state_roots import resolve_global_state_root
from ...core.text_delta_coalescer import TextDeltaCoalescer
from ...core.utils import build_opencode_supervisor
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_runtime_resources
from ...housekeeping import HousekeepingConfig, run_housekeeping_for_roots
from ...integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ...manifest import load_manifest
from ...tickets.replies import dispatch_reply, ensure_reply_dirs, resolve_reply_paths
from ...voice import VoiceConfig, VoiceService
from ..app_server.event_buffer import AppServerEventBuffer
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from ..chat.bound_live_progress import (
    bound_chat_progress_delivered_message_id,
    mark_bound_chat_progress_delivered,
    retire_bound_chat_progress_outbox_records,
)
from ..chat.channel_directory import ChannelDirectoryStore
from ..chat.collaboration_policy import (
    CollaborationEvaluationContext,
    CollaborationEvaluationResult,
    build_telegram_collaboration_policy,
    evaluate_collaboration_policy,
)
from ..chat.managed_thread_delivery_support import (
    ManagedThreadDeliveryCleanupContext,
    ManagedThreadDeliverySendResult,
    deliver_managed_thread_terminal_record,
)
from ..chat.managed_thread_delivery_worker import (
    ManagedThreadDeliveryWorker,
)
from ..chat.managed_thread_turns import (
    render_managed_thread_delivery_record_text,
)
from ..chat.queue_status import (
    QUEUE_STATUS_ITEM_LIMIT,
    QueueStatusItem,
    coerce_queue_status_items,
    format_queue_status_text,
)
from ..chat.service import ChatBotServiceCore
from ..chat.turn_policy import PlainTextTurnContext
from ..chat.update_notifier import ChatUpdateStatusNotifier
from .adapter import (
    InlineButton,
    TelegramAPIError,
    TelegramBotClient,
    TelegramCallbackQuery,
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramUpdate,
    TelegramUpdatePoller,
    build_inline_keyboard,
    encode_cancel_callback,
)
from .cache_manager import TelegramCacheManager
from .chat_adapter import TelegramChatAdapter
from .chat_state_store import TelegramChatStateStore
from .chat_transport import TelegramChatTransport
from .commands_registry import build_command_payloads, diff_command_lists
from .config import (
    AppServerUnavailableError,
    TelegramBotConfig,
    TelegramBotLockError,
    TelegramMediaCandidate,
)
from .config import TelegramBotConfigError as TelegramBotConfigError  # re-export
from .constants import (
    DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    TurnKey,
)
from .dispatch import dispatch_update
from .handlers import callbacks as callback_handlers
from .handlers import messages as message_handlers
from .handlers.approvals import TelegramApprovalHandlers
from .handlers.commands import build_command_specs
from .handlers.commands_runtime import TelegramCommandHandlers
from .handlers.media_ingress import MediaBatchBuffer as _MediaBatchBuffer
from .handlers.messages import _CoalescedBuffer
from .handlers.questions import TelegramQuestionHandlers
from .handlers.selections import TelegramSelectionHandlers
from .helpers import (
    _lock_payload_summary,
    _read_lock_payload,
    _split_topic_key,
    _telegram_lock_path,
    _with_conversation_id,
)
from .managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as _recover_managed_thread_executions_on_startup_impl,
)
from .notifications import TelegramNotificationHandlers
from .outbox import TelegramOutboxManager
from .queue_status_message_manager import TelegramQueueStatusMessageManager
from .queued_placeholder_manager import TelegramQueuedPlaceholderManager
from .runtime import TelegramWorkspaceAndTurnMixin
from .state import (
    OutboxRecord,
    TelegramStateStore,
    parse_topic_key,
    topic_key,
)
from .ticket_flow_bridge import (
    TelegramTicketFlowBridge,
)
from .topic_router import TopicRouter
from .transport import TelegramMessageTransport
from .types import (
    PendingApproval,
    TurnContext,
)
from .typing_manager import TelegramTypingManager
from .ui_state import TelegramUiState
from .update_deduper import TelegramUpdateDeduper
from .voice import TelegramVoiceManager

TICKET_FLOW_WATCH_INTERVAL_SECONDS = 20
TYPING_HEARTBEAT_INTERVAL_SECONDS = 4.0
TELEGRAM_HUB_HANDSHAKE_RETRY_WINDOW_SECONDS = 45.0
TELEGRAM_HUB_HANDSHAKE_RETRY_DELAY_SECONDS = 1.0
TELEGRAM_HUB_HANDSHAKE_RETRY_MAX_DELAY_SECONDS = 5.0
_CURRENT_TELEGRAM_OPERATION_ID: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("telegram_chat_operation_id", default=None)
)


def _build_opencode_supervisor(
    config: TelegramBotConfig,
    *,
    logger: logging.Logger,
) -> Optional[OpenCodeSupervisor]:
    opencode_command = config.opencode_command or None
    opencode_binary = config.agent_binaries.get("opencode")

    supervisor = build_opencode_supervisor(
        opencode_command=opencode_command,
        opencode_binary=opencode_binary,
        workspace_root=config.root,
        logger=logger,
        request_timeout=None,
        max_handles=config.opencode_max_handles,
        idle_ttl_seconds=config.opencode_idle_ttl_seconds,
        base_env=None,
        subagent_models=None,
    )

    if supervisor is None:
        log_event(
            logger,
            logging.INFO,
            "telegram.opencode.unavailable",
            reason="command_missing",
        )
        return None

    return supervisor


def _next_reply_seq_sync(reply_history_dir: Any) -> int:
    from pathlib import Path

    path = Path(reply_history_dir)
    if not path.exists() or not path.is_dir():
        return 1
    existing: list[int] = []
    _SEQ_RE = __import__("re").compile(r"^[0-9]{4}$")
    for child in path.iterdir():
        try:
            if not child.is_dir():
                continue
            if not _SEQ_RE.fullmatch(child.name):
                continue
            existing.append(int(child.name))
        except OSError:
            continue
    return (max(existing) + 1) if existing else 1


class TelegramBotService(
    TelegramWorkspaceAndTurnMixin,
    TelegramMessageTransport,
    TelegramNotificationHandlers,
    TelegramApprovalHandlers,
    TelegramQuestionHandlers,
    TelegramSelectionHandlers,
    TelegramCommandHandlers,
):
    TICKET_FLOW_WATCH_INTERVAL_SECONDS = TICKET_FLOW_WATCH_INTERVAL_SECONDS

    def __init__(
        self,
        config: TelegramBotConfig,
        *,
        logger: Optional[logging.Logger] = None,
        hub_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        voice_config: Optional[VoiceConfig] = None,
        voice_service: Optional[VoiceService] = None,
        housekeeping_config: Optional[HousekeepingConfig] = None,
        update_repo_url: Optional[str] = None,
        update_repo_ref: Optional[str] = None,
        update_skip_checks: bool = False,
        update_backend: str = "auto",
        update_linux_service_names: Optional[dict[str, str]] = None,
        app_server_auto_restart: Optional[bool] = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._hub_supervisor = None
        self._hub_thread_registry = None
        self._hub_config_path: Optional[Path] = None
        config_root = hub_root or self._config.root
        generated_hub_config = config_root / ".codex-autorunner" / "config.yml"
        if generated_hub_config.exists():
            self._hub_config_path = generated_hub_config
        else:
            root_hub_config = config_root / "codex-autorunner.yml"
            if root_hub_config.exists():
                self._hub_config_path = root_hub_config
        if self._hub_root:
            try:
                self._hub_thread_registry = AppServerThreadRegistry(
                    default_app_server_threads_path(self._hub_root)
                )
            except (OSError, ValueError, TypeError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.pma.thread_registry.unavailable",
                    hub_root=str(self._hub_root),
                    exc=exc,
                )
        self._hub_client: Optional[HttpHubControlPlaneClient] = None
        self._hub_handshake_compatibility: Optional[HandshakeCompatibility] = None
        try:
            hub_config = load_hub_config(config_root)
            base_path = hub_config.server_base_path or ""
            if base_path.endswith("/"):
                base_path = base_path[:-1]
            hub_base_url = (
                f"http://{hub_config.server_host}:{hub_config.server_port}{base_path}"
            )
            self._hub_client = HttpHubControlPlaneClient(base_url=hub_base_url)
        except (ConfigError, OSError, ValueError, ImportError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.hub_control_plane.client_init_failed",
                hub_root=str(config_root),
                exc=exc,
            )
        self._update_repo_url = update_repo_url
        self._update_repo_ref = update_repo_ref
        self._update_skip_checks = update_skip_checks
        self._update_backend = update_backend
        self._update_linux_service_names = update_linux_service_names or {}
        self._app_server_auto_restart = app_server_auto_restart
        self._collaboration_policy = (
            config.collaboration_policy
            or build_telegram_collaboration_policy(
                allowed_chat_ids=config.allowed_chat_ids,
                allowed_user_ids=config.allowed_user_ids,
                require_topics=config.require_topics,
                trigger_mode=config.trigger_mode,
            )
        )
        self._allowlist = config.allowlist()
        self._store = TelegramStateStore(
            config.state_file, default_approval_mode=config.defaults.approval_mode
        )
        self._chat_operation_store = SQLiteChatOperationLedger(
            self._hub_root or self._config.root
        )
        self._router = TopicRouter(self._store)
        self._app_server_state_root = resolve_global_state_root() / "workspaces"
        self.app_server_events = AppServerEventBuffer()
        self._app_server_supervisor = WorkspaceAppServerSupervisor(
            config.app_server_command,
            state_root=self._app_server_state_root,
            env_builder=self._build_workspace_env,
            approval_handler=self._handle_approval_request,
            notification_handler=self._handle_buffered_app_server_notification,
            logger=self._logger,
            auto_restart=self._app_server_auto_restart,
            max_handles=config.app_server_max_handles,
            idle_ttl_seconds=config.app_server_idle_ttl_seconds,
        )
        self.app_server_supervisor = self._app_server_supervisor
        self._opencode_supervisor = _build_opencode_supervisor(
            config,
            logger=self._logger,
        )
        self.opencode_supervisor = self._opencode_supervisor
        self._runtime_services = RuntimeServices(
            app_server_supervisor=self._app_server_supervisor,
            opencode_supervisor=self._opencode_supervisor,
            flow_runtime_builder=build_ticket_flow_runtime_resources,
        )
        poll_timeout = float(config.poll_timeout_seconds)
        request_timeout = config.poll_request_timeout_seconds
        if request_timeout is None:
            # Keep HTTP timeout above long-poll timeout to avoid ReadTimeout churn.
            request_timeout = max(poll_timeout + 5.0, 10.0)
        self._bot = TelegramBotClient(
            config.bot_token or "",
            logger=self._logger,
            timeout_seconds=float(request_timeout),
        )
        self._poller = TelegramUpdatePoller(
            self._bot, allowed_updates=config.poll_allowed_updates
        )
        self._chat_adapter = TelegramChatAdapter(
            self._bot,
            poller=self._poller,
        )
        self._chat_transport = TelegramChatTransport(self)
        self._chat_state_store = TelegramChatStateStore(self._store)
        channel_directory_root = self._hub_root or self._config.root
        self._channel_directory_store = ChannelDirectoryStore(channel_directory_root)
        self._chat_core = ChatBotServiceCore(
            owner=self,
            runtime_services=self._runtime_services,
            state_store=self._chat_state_store,
            adapter=self._chat_adapter,
            transport=self._chat_transport,
        )
        self._ui_state = TelegramUiState()
        self._model_options = self._ui_state.model_options
        self._model_pending = self._ui_state.model_pending
        self._model_catalog_cache: dict[str, tuple[Any, float]] = {}
        self._agent_options = self._ui_state.agent_options
        self._agent_profile_options = self._ui_state.agent_profile_options
        self._voice_config = voice_config
        self._voice_service = voice_service
        self._housekeeping_config = housekeeping_config
        self._startup_started_at_monotonic: Optional[float] = None
        self._hub_handshake_retry_window_seconds = (
            TELEGRAM_HUB_HANDSHAKE_RETRY_WINDOW_SECONDS
        )
        self._hub_handshake_retry_delay_seconds = (
            TELEGRAM_HUB_HANDSHAKE_RETRY_DELAY_SECONDS
        )
        self._hub_handshake_retry_max_delay_seconds = (
            TELEGRAM_HUB_HANDSHAKE_RETRY_MAX_DELAY_SECONDS
        )
        if self._voice_service is None and voice_config is not None:
            try:
                self._voice_service = VoiceService(voice_config, logger=self._logger)
            except (OSError, ValueError, RuntimeError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.voice.init_failed",
                    exc=exc,
                )
        self._turn_semaphore: Optional[asyncio.Semaphore] = None
        self._turn_contexts: dict[TurnKey, TurnContext] = {}
        self._reasoning_buffers: dict[str, TextDeltaCoalescer] = {}
        self._turn_preview_text: dict[TurnKey, str] = {}
        self._turn_preview_updated_at: dict[TurnKey, float] = {}
        self._turn_progress_trackers: dict[TurnKey, "TurnProgressTracker"] = {}
        self._turn_progress_rendered: dict[TurnKey, str] = {}
        self._turn_progress_final_rendered: dict[TurnKey, str] = {}
        self._turn_progress_final_summary: dict[TurnKey, str] = {}
        self._turn_progress_updated_at: dict[TurnKey, float] = {}
        self._turn_progress_backoff_until: dict[TurnKey, float] = {}
        self._turn_progress_failure_streaks: dict[TurnKey, int] = {}
        self._turn_progress_suppressed_counts: dict[TurnKey, int] = {}
        self._turn_progress_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_heartbeat_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_locks: dict[TurnKey, asyncio.Lock] = {}
        self._oversize_warnings: set[TurnKey] = set()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_questions = self._ui_state.pending_questions
        self._typing_manager = TelegramTypingManager(owner=self, logger=self._logger)
        self._ticket_flow_pause_targets: dict[str, str] = {}
        self._ticket_flow_bridge = TelegramTicketFlowBridge(
            logger=self._logger,
            store=self._store,
            pause_targets=self._ticket_flow_pause_targets,
            send_message_with_outbox=self._send_message_with_outbox,
            send_document=self._send_document,
            pause_config=self._config.pause_dispatch_notifications,
            default_notification_chat_id=self._config.default_notification_chat_id,
            hub_root=hub_root,
            manifest_path=manifest_path,
            config_root=self._config.root,
            runtime_services=self._runtime_services,
        )
        self._resume_options = self._ui_state.resume_options
        self._bind_options = self._ui_state.bind_options
        self._flow_run_options = self._ui_state.flow_run_options
        self._update_options = self._ui_state.update_options
        self._update_confirm_options = self._ui_state.update_confirm_options
        self._review_commit_options = self._ui_state.review_commit_options
        self._review_commit_subjects = self._ui_state.review_commit_subjects
        self._pending_review_custom = self._ui_state.pending_review_custom
        self._compact_pending = self._ui_state.compact_pending
        self._document_browser_states = self._ui_state.document_browser_states
        self._coalesced_buffers: dict[str, _CoalescedBuffer] = {}
        self._coalesce_locks: dict[str, asyncio.Lock] = {}
        self._media_batch_buffers: dict[str, _MediaBatchBuffer] = {}
        self._media_batch_locks: dict[str, asyncio.Lock] = {}
        self._outbox_inflight: set[str] = set()
        self._outbox_lock: Optional[asyncio.Lock] = None
        self._queued_placeholder_manager = TelegramQueuedPlaceholderManager()
        self._queue_status_message_manager = TelegramQueueStatusMessageManager()
        self._bot_username: Optional[str] = None
        self._token_usage_by_thread: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._token_usage_by_turn: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._outbox_task: Optional[asyncio.Task[None]] = None
        self._cache_cleanup_task: Optional[asyncio.Task[None]] = None
        self._ticket_flow_watch_task: Optional[asyncio.Task[None]] = None
        self._terminal_flow_watch_task: Optional[asyncio.Task[None]] = None
        self._cache_manager = TelegramCacheManager(
            logger=self._logger, config=self._config
        )
        self._update_deduper = TelegramUpdateDeduper(
            logger=self._logger, now_func=lambda: time.monotonic()
        )
        self._spawned_tasks: set[asyncio.Task[Any]] = set()
        self._update_status_notifier = ChatUpdateStatusNotifier(
            platform="telegram",
            logger=self._logger,
            read_status=self._read_update_status,
            send_notice=self._send_update_status_notice,
            spawn_task=self._spawn_task,
            mark_notified=self._mark_update_notified,
            format_status=self._format_update_status_message,
            running_message="Update still running. Use /update status for the latest state.",
        )
        self._outbox_manager = TelegramOutboxManager(
            self._store,
            send_message=self._send_message,
            edit_message_text=self._edit_message_text,
            delete_message=self._delete_message,
            on_delivered=self._handle_telegram_outbox_delivery,
            logger=self._logger,
        )
        self._voice_manager = TelegramVoiceManager(
            self._config,
            self._store,
            voice_config=self._voice_config,
            voice_service=self._voice_service,
            send_message=self._send_message,
            edit_message_text=self._edit_message_text,
            send_progress_message=self._send_voice_progress_message,
            deliver_transcript=self._deliver_voice_transcript,
            download_file=self._download_telegram_file,
            logger=self._logger,
        )
        self._voice_task: Optional[asyncio.Task[None]] = None
        self._housekeeping_task: Optional[asyncio.Task[None]] = None
        self._command_specs = build_command_specs(self)
        self._instance_lock_path: Optional[Path] = None
        self._instance_lock: Optional[FileLock] = None
        self._delivery_worker: Optional[ManagedThreadDeliveryWorker] = None
        self._delivery_worker_task: Optional[asyncio.Task[None]] = None

    def _build_delivery_worker(self) -> ManagedThreadDeliveryWorker:
        service = self

        class _TelegramDeliveryAdapter:
            @property
            def adapter_key(self) -> str:
                return "telegram"

            async def deliver_managed_thread_record(
                self, record: Any, *, claim: Any
            ) -> Any:
                _ = claim
                transport_target = dict(record.target.transport_target or {})
                target_chat_id = int(transport_target.get("chat_id", 0))
                target_thread_id = transport_target.get("thread_id")
                if not target_chat_id:
                    return ManagedThreadDeliveryAttemptResult(
                        outcome=ManagedThreadDeliveryOutcome.ABANDONED,
                        error="missing_telegram_chat_id",
                    )

                async def _send_success(
                    _context: ManagedThreadDeliveryCleanupContext,
                ) -> ManagedThreadDeliverySendResult:
                    await service._send_message(
                        target_chat_id,
                        render_managed_thread_delivery_record_text(record),
                        thread_id=target_thread_id,
                        reply_to=None,
                    )
                    return ManagedThreadDeliverySendResult()

                async def _send_failure(
                    _context: ManagedThreadDeliveryCleanupContext,
                ) -> ManagedThreadDeliverySendResult:
                    await service._send_message(
                        target_chat_id,
                        (
                            f"Turn failed: {record.envelope.error_text or 'execution error'}"
                        ),
                        thread_id=target_thread_id,
                        reply_to=None,
                    )
                    return ManagedThreadDeliverySendResult()

                async def _cleanup(
                    context: ManagedThreadDeliveryCleanupContext,
                ) -> None:
                    await service._flush_outbox_files(
                        SimpleNamespace(
                            workspace_path=context.transport_target.get(
                                "workspace_path"
                            ),
                            pma_enabled=bool(
                                context.transport_target.get("pma_enabled")
                            ),
                        ),
                        chat_id=target_chat_id,
                        thread_id=target_thread_id,
                        reply_to=None,
                        topic_key=str(context.transport_target.get("topic_key") or ""),
                    )

                return await deliver_managed_thread_terminal_record(
                    record,
                    send_success=_send_success,
                    send_failure=_send_failure,
                    cleanup=_cleanup,
                )

        state_root = Path(self._hub_root or self._config.root)
        engine = SQLiteManagedThreadDeliveryEngine(state_root)
        return ManagedThreadDeliveryWorker(
            engine=engine,
            adapter=_TelegramDeliveryAdapter(),
            logger=self._logger,
        )

    async def _handle_telegram_outbox_delivery(
        self, record: OutboxRecord, delivered_message_id: Optional[int]
    ) -> None:
        delivered_id_str = (
            str(delivered_message_id) if isinstance(delivered_message_id, int) else None
        )
        operation = str(record.operation or "").strip().lower()
        delivered_without_message_id = operation in {"edit", "delete"}
        cleanup_payload = record.delivery_metadata
        if record.operation_id:
            if delivered_id_str is None and not delivered_without_message_id:
                await self._mark_chat_operation_state(
                    record.operation_id,
                    state=ChatOperationState.FAILED,
                    delivery_state="failed",
                    terminal_outcome="failed",
                )
            else:
                state_changes: dict[str, object] = {
                    "delivery_state": "delivered",
                }
                if delivered_id_str is not None:
                    state_changes["anchor_ref"] = delivered_id_str
                await self._mark_chat_operation_state(
                    record.operation_id,
                    state=ChatOperationState.COMPLETED,
                    **state_changes,
                )
        if delivered_id_str is None:
            if (
                operation == "send"
                and isinstance(cleanup_payload, dict)
                and cleanup_payload.get("kind") == "managed_thread_live_progress"
            ):
                surface_key = str(cleanup_payload.get("surface_key") or "").strip()
                progress_send_record_id = (
                    await retire_bound_chat_progress_outbox_records(
                        store=self._store,
                        surface_kind="telegram",
                        surface_key=surface_key,
                        managed_thread_id=str(
                            cleanup_payload.get("managed_thread_id") or ""
                        ).strip(),
                        managed_turn_id=str(
                            cleanup_payload.get("managed_turn_id") or ""
                        ).strip(),
                    )
                )
                raw_progress_message_id = bound_chat_progress_delivered_message_id(
                    hub_root=self._config.root,
                    delivery_record_id=progress_send_record_id,
                )
                if raw_progress_message_id:
                    try:
                        progress_message_id = int(raw_progress_message_id)
                        await self._store.enqueue_outbox(
                            OutboxRecord(
                                record_id=(
                                    "managed-thread-progress-failure:"
                                    f"{progress_send_record_id}"
                                ),
                                chat_id=record.chat_id,
                                thread_id=record.thread_id,
                                reply_to_message_id=None,
                                placeholder_message_id=None,
                                text=(
                                    "Status: this turn finished, but Telegram failed "
                                    "before the final reply was delivered. Please "
                                    "retry if needed."
                                ),
                                created_at=now_iso(),
                                operation="edit",
                                message_id=progress_message_id,
                                outbox_key=(
                                    "managed-thread-progress-failure:"
                                    f"{progress_send_record_id}"
                                ),
                            )
                        )
                    except ValueError:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.outbox.progress_failure_cleanup_failed",
                            record_id=record.record_id,
                            progress_message_id=raw_progress_message_id,
                        )
            return
        if self._hub_client is None:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.outbox.delivery_mark.hub_client_unavailable",
                record_id=record.record_id,
            )
        else:
            from ...core.hub_control_plane import (
                NotificationDeliveryMarkRequest as _CPDeliveryMarkRequest,
            )

            try:
                await self._hub_client.mark_notification_delivered(
                    _CPDeliveryMarkRequest(
                        delivery_record_id=record.record_id,
                        delivered_message_id=delivered_id_str,
                    )
                )
            except (HubControlPlaneError, OSError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.delivery_mark.control_plane_failed",
                    record_id=record.record_id,
                    exc=exc,
                )
        if (
            delivered_id_str is not None
            and record.operation == "send"
            and record.record_id.startswith("managed-thread-progress:")
        ):
            mark_bound_chat_progress_delivered(
                hub_root=self._config.root,
                delivery_record_id=record.record_id,
                delivered_message_id=delivered_id_str,
            )
        cleanup_payload = record.delivery_metadata
        if not isinstance(cleanup_payload, dict):
            return
        if cleanup_payload.get("kind") != "managed_thread_live_progress":
            return
        managed_thread_id = str(cleanup_payload.get("managed_thread_id") or "").strip()
        managed_turn_id = str(cleanup_payload.get("managed_turn_id") or "").strip()
        surface_key = str(cleanup_payload.get("surface_key") or "").strip()
        if not managed_thread_id or not managed_turn_id or not surface_key:
            return
        progress_send_record_id = await retire_bound_chat_progress_outbox_records(
            store=self._store,
            surface_kind="telegram",
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        )
        progress_message_id: Optional[int] = None
        raw_progress_message_id = bound_chat_progress_delivered_message_id(
            hub_root=self._config.root,
            delivery_record_id=progress_send_record_id,
        )
        if raw_progress_message_id:
            try:
                progress_message_id = int(raw_progress_message_id)
            except ValueError:
                progress_message_id = None
        if progress_message_id is None:
            await self._store.delete_outbox(progress_send_record_id)
            return
        cleanup_record = OutboxRecord(
            record_id=(
                "managed-thread-progress-cleanup:"
                f"{progress_send_record_id}:{delivered_id_str}"
            ),
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="",
            created_at=now_iso(),
            operation="delete",
            message_id=progress_message_id,
            outbox_key=(
                "managed-thread-progress-cleanup:"
                f"{progress_send_record_id}:{progress_message_id}"
            ),
        )
        await self._store.enqueue_outbox(cleanup_record)

    async def _housekeeping_roots(self) -> list[Path]:
        roots = set(
            await self._workspace_roots_from_state(
                state_failed_event="telegram.housekeeping.state_failed",
                prune_reason="housekeeping",
            )
        )
        if self._hub_root and self._manifest_path and self._manifest_path.exists():
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                for repo in manifest.repos:
                    roots.add((self._hub_root / repo.path).resolve())
            except (ValueError, OSError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.housekeeping.manifest_failed",
                    exc=exc,
                )
        if self._config.root:
            roots.add(self._config.root.resolve())
        return sorted(roots)

    async def _gather_workspace_roots(self) -> list[Path]:
        return await self._workspace_roots_from_state(
            state_failed_event="telegram.prewarm.state_failed",
            prune_reason="prewarm",
        )

    async def _workspace_roots_from_state(
        self,
        *,
        state_failed_event: str,
        prune_reason: str,
    ) -> list[Path]:
        roots: set[Path] = set()
        stale_topics: list[tuple[str, Path]] = []
        try:
            state = await self._store.load()
            for key, record in state.topics.items():
                workspace_root = self._canonical_workspace_root(record.workspace_path)
                if workspace_root is None:
                    continue
                if workspace_root.is_dir():
                    roots.add(workspace_root)
                    continue
                stale_topics.append((key, workspace_root))
        except (OSError, ValueError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                state_failed_event,
                exc=exc,
            )
            return []
        if stale_topics:
            await self._clear_stale_workspace_topics(
                stale_topics,
                reason=prune_reason,
            )
        return sorted(roots)

    async def _clear_stale_workspace_topics(
        self,
        topics: Sequence[tuple[str, Path]],
        *,
        reason: str,
    ) -> None:
        pruned_keys: list[str] = []
        pruned_workspaces: list[str] = []
        for key, workspace_root in topics:

            def clear_stale_workspace(record: TelegramTopicRecord) -> None:
                record.workspace_path = None
                record.workspace_id = None

            try:
                await self._store.update_topic(key, clear_stale_workspace)
            except (OSError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.workspace_binding.prune_failed",
                    reason=reason,
                    topic_key=key,
                    workspace_root=str(workspace_root),
                    exc=exc,
                )
                continue
            pruned_keys.append(key)
            pruned_workspaces.append(str(workspace_root))
        if pruned_keys:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.workspace_binding.pruned",
                reason=reason,
                pruned_count=len(pruned_keys),
                topic_keys=pruned_keys,
                workspaces=pruned_workspaces,
            )

    async def _prewarm_workspace_clients(self) -> None:
        workspace_roots = await self._gather_workspace_roots()
        if not workspace_roots:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.prewarm.skipped",
                reason="no_workspaces",
            )
            return

        log_event(
            self._logger,
            logging.INFO,
            "telegram.prewarm.started",
            workspace_count=len(workspace_roots),
            workspaces=[str(p) for p in workspace_roots],
        )

        sem = asyncio.Semaphore(3)
        prewarmed_count = 0
        failed_count = 0
        skipped_missing_count = 0

        async def prewarm_one(workspace_root: Path) -> None:
            nonlocal prewarmed_count, failed_count, skipped_missing_count
            async with sem:
                if not workspace_root.is_dir():
                    skipped_missing_count += 1
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.prewarm.client_skipped",
                        workspace_root=str(workspace_root),
                        reason="missing_workspace",
                    )
                    return
                try:
                    await self._app_server_supervisor.get_client(workspace_root)
                    prewarmed_count += 1
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.prewarm.client_ready",
                        workspace_root=str(workspace_root),
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    failed_count += 1
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.prewarm.client_failed",
                        workspace_root=str(workspace_root),
                        exc=exc,
                    )

        await asyncio.gather(
            *[prewarm_one(root) for root in workspace_roots],
            return_exceptions=True,
        )

        log_event(
            self._logger,
            logging.INFO,
            "telegram.prewarm.completed",
            workspace_count=len(workspace_roots),
            prewarmed_count=prewarmed_count,
            failed_count=failed_count,
            skipped_missing_count=skipped_missing_count,
        )

    async def _housekeeping_loop(self) -> None:
        config = self._housekeeping_config
        if config is None or not config.enabled:
            return
        base_interval = max(config.interval_seconds, 1)
        idle_streak = 0
        while True:
            try:
                roots = await self._housekeeping_roots()
                if not roots:
                    idle_streak += 1
                    extended = min(
                        base_interval * (1.5 ** min(idle_streak, 8)),
                        base_interval * 4,
                    )
                    await asyncio.sleep(extended)
                    continue
                idle_streak = 0
                await self._run_housekeeping_cycle_with_roots(config, roots)
            except Exception as exc:  # intentional: top-level error handler
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.housekeeping.failed",
                    exc=exc,
                )
            await asyncio.sleep(base_interval)

    async def _run_housekeeping_cycle(self, config: HousekeepingConfig) -> None:
        roots = await self._housekeeping_roots()
        await self._run_housekeeping_cycle_with_roots(config, roots)

    async def _run_housekeeping_cycle_with_roots(
        self, config: HousekeepingConfig, roots: list[Path]
    ) -> None:
        if roots:
            for root in roots:
                try:
                    repo_config = load_repo_config(root, hub_path=self._hub_config_path)
                    summary = await asyncio.to_thread(
                        prune_filebox_root,
                        root,
                        policy=resolve_filebox_retention_policy(repo_config.pma),
                    )
                    if summary.inbox_pruned or summary.outbox_pruned:
                        log_event(
                            self._logger,
                            logging.INFO,
                            "telegram.filebox.cleanup",
                            root=str(root),
                            inbox_pruned=summary.inbox_pruned,
                            outbox_pruned=summary.outbox_pruned,
                            bytes_before=summary.bytes_before,
                            bytes_after=summary.bytes_after,
                        )
                except (ValueError, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.filebox.cleanup_failed",
                        root=str(root),
                        exc=exc,
                    )
            await asyncio.to_thread(
                run_housekeeping_for_roots,
                config,
                roots,
                self._logger,
            )
        await self._app_server_supervisor.prune_idle()
        if self._opencode_supervisor is not None:
            await self._opencode_supervisor.prune_idle()

    def _evaluate_collaboration_message_policy(
        self,
        message: TelegramMessage,
        *,
        text: str,
        is_explicit_command: bool,
    ) -> CollaborationEvaluationResult:
        return evaluate_collaboration_policy(
            self._collaboration_policy,
            CollaborationEvaluationContext(
                actor_id=(
                    str(message.from_user_id)
                    if message.from_user_id is not None
                    else None
                ),
                container_id=str(message.chat_id),
                destination_id=str(message.chat_id),
                subdestination_id=(
                    str(message.thread_id) if message.thread_id is not None else None
                ),
                is_explicit_command=is_explicit_command,
                plain_text=PlainTextTurnContext(
                    text=text,
                    chat_type=message.chat_type,
                    bot_username=self._bot_username,
                    reply_to_is_bot=message.reply_to_is_bot,
                    reply_to_username=message.reply_to_username,
                    reply_to_message_id=(
                        str(message.reply_to_message_id)
                        if message.reply_to_message_id is not None
                        else None
                    ),
                    thread_id=(
                        str(message.thread_id)
                        if message.thread_id is not None
                        else None
                    ),
                ),
            ),
        )

    def _log_collaboration_policy_result(
        self,
        message: TelegramMessage,
        result: CollaborationEvaluationResult,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.collaboration_policy.evaluated",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            user_id=message.from_user_id,
            **result.log_fields(),
        )

    def _ensure_outbox_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._outbox_lock
        lock_loop = getattr(lock, "_loop", None) if lock else None
        if (
            lock is None
            or lock_loop is None
            or lock_loop is not loop
            or lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            self._outbox_lock = lock
        return lock

    async def _mark_outbox_inflight(self, record_id: str) -> bool:
        lock = self._ensure_outbox_lock()
        async with lock:
            if record_id in self._outbox_inflight:
                return False
            self._outbox_inflight.add(record_id)
            return True

    async def _clear_outbox_inflight(self, record_id: str) -> None:
        lock = self._ensure_outbox_lock()
        async with lock:
            self._outbox_inflight.discard(record_id)

    def _acquire_instance_lock(self) -> None:
        token = self._config.bot_token
        if not token:
            raise TelegramBotLockError("missing telegram bot token")
        lock_path = _telegram_lock_path(token)
        payload = {
            "pid": os.getpid(),
            "started_at": now_iso(),
            "host": socket.gethostname(),
            "cwd": os.getcwd(),
            "config_root": str(self._config.root),
        }
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock = FileLock(lock_path)
            lock.acquire(blocking=False)
        except FileLockBusy as exc:
            existing = _read_lock_payload(lock_path)
            log_event(
                self._logger,
                logging.ERROR,
                "telegram.lock.contended",
                lock_path=str(lock_path),
                **_lock_payload_summary(existing),
            )
            raise TelegramBotLockError(
                "Telegram bot already running for this token."
            ) from exc
        lock.write_text(json.dumps(payload) + "\n")
        self._instance_lock_path = lock_path
        self._instance_lock = lock
        log_event(
            self._logger,
            logging.INFO,
            "telegram.lock.acquired",
            lock_path=str(lock_path),
            **_lock_payload_summary(payload),
        )

    def _release_instance_lock(self) -> None:
        lock = self._instance_lock
        lock_path = self._instance_lock_path
        if lock is None or lock_path is None:
            return
        try:
            existing = _read_lock_payload(lock_path)
            if isinstance(existing, dict):
                pid = existing.get("pid")
                if isinstance(pid, int) and pid == os.getpid():
                    lock.write_text("")
        finally:
            lock.release()
        self._instance_lock_path = None
        self._instance_lock = None

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        if self._turn_semaphore is None:
            self._turn_semaphore = asyncio.Semaphore(
                self._config.concurrency.max_parallel_turns
            )
        return self._turn_semaphore

    async def run_polling(self) -> None:
        self._startup_started_at_monotonic = time.monotonic()
        handshake_ok = await self._perform_hub_handshake()
        if not handshake_ok:
            raise SystemExit(1)
        await _recover_managed_thread_executions_on_startup_impl(self)
        await self._chat_core.run()

    @property
    def hub_client(self) -> Optional[HttpHubControlPlaneClient]:
        return self._hub_client

    async def _perform_hub_handshake(self) -> bool:
        expected_schema_generation = ORCHESTRATION_SCHEMA_VERSION
        if self._hub_client is None:
            log_event(
                self._logger,
                logging.ERROR,
                "telegram.hub_control_plane.client_not_configured",
                hub_root=str(self._hub_root or self._config.root),
                expected_schema_generation=expected_schema_generation,
            )
            return False

        hub_root_str = str(self._hub_root or self._config.root)
        ok, compatibility = await perform_startup_hub_handshake(
            hub_client=self._hub_client,
            log_event_name_prefix="telegram",
            handshake_client_name="telegram",
            hub_root_str=hub_root_str,
            startup_monotonic=self._startup_started_at_monotonic,
            retry_window_seconds=self._hub_handshake_retry_window_seconds,
            retry_delay_seconds=self._hub_handshake_retry_delay_seconds,
            retry_max_delay_seconds=self._hub_handshake_retry_max_delay_seconds,
            client_api_version=_CONTROL_PLANE_API_VERSION,
            logger=self._logger,
        )
        if compatibility is not None:
            self._hub_handshake_compatibility = compatibility
        return ok

    async def _dispatch_update(self, update: TelegramUpdate) -> None:
        await dispatch_update(self, update)

    async def _prime_bot_identity(self) -> None:
        try:
            payload = await self._bot.get_me()
        except TelegramAPIError:
            return
        if isinstance(payload, dict):
            username = payload.get("username")
            if isinstance(username, str) and username:
                self._bot_username = username

    async def _register_bot_commands(self) -> None:
        registration = self._config.command_registration
        if not registration.enabled:
            log_event(
                self._logger,
                logging.DEBUG,
                "telegram.commands.disabled",
            )
            return
        desired, invalid = build_command_payloads(self._command_specs)
        if invalid:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.commands.invalid",
                invalid=invalid,
            )
        if not desired:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.commands.empty",
            )
            return
        if len(desired) > 100:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.commands.truncated",
                desired_count=len(desired),
            )
            desired = desired[:100]
        for scope_spec in registration.scopes:
            scope = scope_spec.scope
            language_code = scope_spec.language_code
            try:
                current = await self._bot.get_my_commands(
                    scope=scope,
                    language_code=language_code,
                )
            except TelegramAPIError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.commands.get_failed",
                    scope=scope,
                    language_code=language_code,
                    exc=exc,
                )
                continue
            diff = diff_command_lists(desired, current)
            if not diff.needs_update:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "telegram.commands.up_to_date",
                    scope=scope,
                    language_code=language_code,
                )
                continue
            try:
                updated = await self._bot.set_my_commands(
                    desired,
                    scope=scope,
                    language_code=language_code,
                )
            except TelegramAPIError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.commands.set_failed",
                    scope=scope,
                    language_code=language_code,
                    exc=exc,
                )
                continue
            log_event(
                self._logger,
                logging.INFO,
                "telegram.commands.updated",
                scope=scope,
                language_code=language_code,
                updated=updated,
                added=diff.added,
                removed=diff.removed,
                changed=diff.changed,
                order_changed=diff.order_changed,
            )

    async def _prime_poller_offset(self) -> None:
        last_update_id = await self._store.get_last_update_id_global()
        if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
            return
        offset = last_update_id + 1
        self._poller.set_offset(offset)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.poll.offset.init",
            stored_global_update_id=last_update_id,
            poller_offset=offset,
        )

    async def _record_poll_offset(self, updates: Sequence[TelegramUpdate]) -> None:
        offset = self._poller.offset
        if offset is None:
            return
        last_update_id = offset - 1
        if last_update_id < 0:
            return
        stored = await self._store.update_last_update_id_global(last_update_id)
        max_update_id = max((update.update_id for update in updates), default=None)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.poll.offset.updated",
            incoming_update_id=max_update_id,
            stored_global_update_id=stored,
            poller_offset=offset,
        )

    def _spawn_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._spawned_tasks.add(task)
        task.add_done_callback(self._log_task_result)
        return task

    def _log_task_result(self, task: asyncio.Future) -> None:
        if isinstance(task, asyncio.Task):
            self._spawned_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # intentional: top-level error handler
            log_event(self._logger, logging.WARNING, "telegram.task.failed", exc=exc)

    def _touch_cache_timestamp(self, cache_name: str, key: object) -> None:
        self._cache_manager.touch(cache_name, key)

    def _evict_expired_cache_entries(self, cache_name: str, ttl_seconds: float) -> None:
        self._cache_manager.evict_expired(cache_name, ttl_seconds, state=self)

    @property
    def _cache_timestamps(self) -> dict[str, dict[object, float]]:
        return self._cache_manager._cache_timestamps

    def _has_any_cache_entries(self) -> bool:
        return bool(self._cache_timestamps)

    async def _cache_cleanup_loop(self) -> None:
        await self._cache_manager.cleanup_loop(state=self)

    @staticmethod
    def _parse_last_active(record: "TelegramTopicRecord") -> float:
        raw = getattr(record, "last_active_at", None)
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").timestamp()
            except ValueError:
                return float("-inf")
        return float("-inf")

    def _select_ticket_flow_topic(
        self, entries: list[tuple[str, "TelegramTopicRecord"]]
    ) -> Optional[tuple[str, "TelegramTopicRecord"]]:
        return self._ticket_flow_bridge._select_ticket_flow_topic(entries)

    @staticmethod
    def _set_ticket_dispatch_marker(
        value: Optional[str],
    ) -> "Callable[[TelegramTopicRecord], None]":
        def apply(topic: "TelegramTopicRecord") -> None:
            topic.last_ticket_dispatch_seq = value

        return apply

    async def _ticket_flow_watch_loop(self) -> None:
        await self._ticket_flow_bridge.watch_ticket_flow_pauses(
            TICKET_FLOW_WATCH_INTERVAL_SECONDS
        )

    async def _watch_ticket_flow_pauses(self) -> None:
        await self._ticket_flow_bridge._scan_and_notify_pauses()

    async def _notify_ticket_flow_pause(
        self,
        workspace_root: Path,
        entries: list[tuple[str, "TelegramTopicRecord"]],
    ) -> None:
        await self._ticket_flow_bridge._notify_ticket_flow_pause(
            workspace_root, entries
        )

    def _load_ticket_flow_pause(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, str, Optional[Path]]]:
        return self._ticket_flow_bridge._load_ticket_flow_pause(workspace_root)

    def _latest_dispatch_seq(self, history_dir: Path) -> Optional[str]:
        return latest_dispatch_seq(history_dir)

    def _format_ticket_flow_pause_reason(self, record: "FlowRunRecord") -> str:
        return format_pause_reason(record)

    def _get_paused_ticket_flow(
        self, workspace_root: Path, preferred_run_id: Optional[str] = None
    ) -> Optional[tuple[str, FlowRunRecord]]:
        return self._ticket_flow_bridge.get_paused_ticket_flow(
            workspace_root, preferred_run_id=preferred_run_id
        )

    async def _auto_resume_ticket_flow_run(
        self, workspace_root: Path, run_id: str
    ) -> None:
        await self._ticket_flow_bridge.auto_resume_run(workspace_root, run_id)

    async def _write_user_reply_from_telegram(
        self,
        workspace_root: Path,
        run_id: str,
        run_record: FlowRunRecord,
        message: TelegramMessage,
        text: str,
        files: Optional[list[tuple[str, bytes]]] = None,
    ) -> tuple[bool, str]:
        try:
            reply_paths = resolve_reply_paths(
                workspace_root=workspace_root, run_id=run_id
            )
            ensure_reply_dirs(reply_paths)

            cleaned_text = text.strip()
            raw = cleaned_text
            if raw and not raw.endswith("\n"):
                raw += "\n"

            await asyncio.to_thread(
                reply_paths.user_reply_path.write_text, raw, encoding="utf-8"
            )

            if files:
                for filename, data in files:
                    safe_name = Path(filename).name
                    if not safe_name or safe_name in {".", ".."}:
                        safe_name = "attachment"
                    dest = reply_paths.reply_dir / safe_name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(dest.write_bytes, data)

            seq = await asyncio.to_thread(
                lambda: _next_reply_seq_sync(reply_paths.reply_history_dir)
            )
            dispatch, errors = await asyncio.to_thread(
                dispatch_reply, reply_paths, next_seq=seq
            )
            if errors:
                return False, "\n".join(errors)
            if dispatch is None:
                return False, "Failed to archive reply"
            return (
                True,
                f"Reply archived (seq {dispatch.seq}).",
            )
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            self._logger.warning(
                "Failed to write USER_REPLY.md from Telegram",
                exc=exc,
                workspace_root=str(workspace_root),
                run_id=run_id,
            )
            return False, f"Failed to write reply: {exc}"

    async def _interrupt_timeout_check(
        self, key: str, turn_id: str, message_id: int
    ) -> None:
        await asyncio.sleep(DEFAULT_INTERRUPT_TIMEOUT_SECONDS)
        runtime = self._router.runtime_for(key)
        if runtime.current_turn_id != turn_id:
            return
        if runtime.interrupt_message_id != message_id:
            return
        if runtime.interrupt_turn_id != turn_id:
            return
        chat_id, _thread_id = _split_topic_key(key)
        await self._edit_message_text(
            chat_id,
            message_id,
            "Still stopping... (30s). If this is stuck, try /interrupt again.",
        )
        runtime.interrupt_requested = False

    async def _dispatch_interrupt_request(
        self,
        *,
        turn_id: str,
        codex_thread_id: Optional[str],
        runtime: Any,
        chat_id: int,
        thread_id: Optional[int],
    ) -> None:
        key = await self._resolve_topic_key(chat_id, thread_id)
        record = await self._router.get_topic(key)
        turn_ctx = self._resolve_turn_context(turn_id, thread_id=codex_thread_id)
        if turn_ctx is not None and turn_ctx.topic_key and turn_ctx.topic_key != key:
            scoped_record = await self._router.get_topic(turn_ctx.topic_key)
            if scoped_record is not None:
                key = turn_ctx.topic_key
                record = scoped_record
        workspace_path = record.workspace_path if record else None
        if record and getattr(record, "pma_enabled", False):
            # PMA topics are hub-scoped; their stored topic records may not carry a
            # workspace path. Turn execution patches workspace_path to hub_root, but
            # interrupt dispatch must do the same so /interrupt works in PMA mode.
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is not None:
                workspace_path = str(hub_root)
        if record and record.agent == "opencode":
            session_id = record.active_thread_id or codex_thread_id
            if (
                not session_id
                or self._opencode_supervisor is None
                or not isinstance(workspace_path, str)
                or not workspace_path
            ):
                runtime.interrupt_requested = False
                if runtime.interrupt_message_id is not None:
                    await self._edit_message_text(
                        chat_id,
                        runtime.interrupt_message_id,
                        "Interrupt failed (OpenCode unavailable).",
                    )
                    runtime.interrupt_message_id = None
                    runtime.interrupt_turn_id = None
                return
            try:
                client = await self._opencode_supervisor.get_client(
                    Path(workspace_path)
                )
                await client.abort(session_id)
            except (RuntimeError, OSError, BrokenPipeError, ProcessLookupError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.interrupt.failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    exc=exc,
                )
                if (
                    runtime.interrupt_message_id is not None
                    and runtime.interrupt_turn_id == turn_id
                ):
                    await self._edit_message_text(
                        chat_id,
                        runtime.interrupt_message_id,
                        "Interrupt failed (OpenCode error).",
                    )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
                runtime.interrupt_requested = False
                return
            runtime.interrupt_message_id = None
            runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
            return
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError:
            runtime.interrupt_requested = False
            if runtime.interrupt_message_id is not None:
                await self._edit_message_text(
                    chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt failed (app-server unavailable).",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            return
        if client is None:
            runtime.interrupt_requested = False
            if runtime.interrupt_message_id is not None:
                await self._edit_message_text(
                    chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt failed (app-server error).",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            return
        try:
            await client.turn_interrupt(turn_id, thread_id=codex_thread_id)
        except (RuntimeError, ConnectionError, OSError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.interrupt.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
                exc=exc,
            )
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == turn_id
            ):
                await self._edit_message_text(
                    chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt failed (app-server error).",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False

    async def _handle_message(self, message: TelegramMessage) -> None:
        self._record_channel_directory_seen_from_telegram_message(message)
        await message_handlers.handle_message(self, message)

    def _should_bypass_topic_queue(self, message: TelegramMessage) -> bool:
        return message_handlers.should_bypass_topic_queue(self, message)

    def _record_channel_directory_seen_from_telegram_message(
        self, message: TelegramMessage
    ) -> None:
        thread_id = (
            str(message.thread_id) if isinstance(message.thread_id, int) else None
        )
        chat_label = (
            message.chat_title.strip()
            if isinstance(message.chat_title, str) and message.chat_title.strip()
            else str(message.chat_id)
        )
        topic_label: Optional[str] = None
        if thread_id is None:
            display = chat_label
        else:
            topic_label = (
                message.thread_title.strip()
                if isinstance(message.thread_title, str)
                and message.thread_title.strip()
                else None
            )
            if topic_label is None:
                topic_label = self._existing_topic_label(
                    chat_id=str(message.chat_id),
                    thread_id=thread_id,
                    chat_label=chat_label,
                )
            if topic_label is None:
                topic_label = thread_id
            display = f"{chat_label} / {topic_label}"

        meta: dict[str, Any] = {}
        if isinstance(message.chat_type, str) and message.chat_type.strip():
            meta["chat_type"] = message.chat_type.strip()
        if (
            thread_id is not None
            and isinstance(topic_label, str)
            and topic_label != thread_id
        ):
            meta["topic_title"] = topic_label

        try:
            self._channel_directory_store.record_seen(
                "telegram",
                str(message.chat_id),
                thread_id,
                display,
                meta,
            )
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.channel_directory.record_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )

    async def _handle_buffered_app_server_notification(
        self, message: dict[str, Any]
    ) -> None:
        await self.app_server_events.handle_notification(message)
        await self._handle_app_server_notification(message)

    def _existing_topic_label(
        self, *, chat_id: str, thread_id: str, chat_label: str
    ) -> Optional[str]:
        try:
            entries = self._channel_directory_store.list_entries(limit=None)
        except OSError:
            return None

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("platform") != "telegram":
                continue
            if str(entry.get("chat_id") or "") != chat_id:
                continue
            if str(entry.get("thread_id") or "") != thread_id:
                continue

            meta = entry.get("meta")
            if isinstance(meta, dict):
                topic_title = meta.get("topic_title")
                if isinstance(topic_title, str):
                    normalized = topic_title.strip()
                    if normalized:
                        return normalized

            display = entry.get("display")
            if isinstance(display, str):
                prefix = f"{chat_label} / "
                if display.startswith(prefix):
                    topic_part = display[len(prefix) :].strip()
                    if topic_part and topic_part != thread_id:
                        return topic_part
                separator_index = display.rfind(" / ")
                if separator_index >= 0:
                    topic_part = display[separator_index + 3 :].strip()
                    if topic_part and topic_part != thread_id:
                        return topic_part
            break
        return None

    async def _handle_edited_message(self, message: TelegramMessage) -> None:
        await message_handlers.handle_edited_message(self, message)

    async def _handle_message_inner(
        self,
        message: TelegramMessage,
        *,
        topic_key: Optional[str] = None,
        placeholder_id: Optional[int] = None,
    ) -> None:
        await message_handlers.handle_message_inner(
            self, message, topic_key=topic_key, placeholder_id=placeholder_id
        )

    def _coalesce_key_for_topic(self, key: str, user_id: Optional[int]) -> str:
        return message_handlers.coalesce_key_for_topic(self, key, user_id)

    async def _coalesce_key(self, message: TelegramMessage) -> str:
        return await message_handlers.coalesce_key(self, message)

    async def _buffer_coalesced_message(
        self,
        message: TelegramMessage,
        text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        await message_handlers.buffer_coalesced_message(
            self, message, text, placeholder_id=placeholder_id
        )

    async def _coalesce_flush_after(self, key: str) -> None:
        window_seconds = self._config.coalesce_window_seconds
        await message_handlers.coalesce_flush_after(self, key, window_seconds)

    async def _flush_coalesced_message(self, message: TelegramMessage) -> None:
        await message_handlers.flush_coalesced_message(self, message)

    async def _flush_coalesced_key(self, key: str) -> None:
        await message_handlers.flush_coalesced_key(self, key)

    def _build_coalesced_message(self, buffer: _CoalescedBuffer) -> TelegramMessage:
        return message_handlers.build_coalesced_message(buffer)

    def _message_has_media(self, message: TelegramMessage) -> bool:
        return message_handlers.message_has_media(message)

    def _select_photo(
        self, photos: Sequence[TelegramPhotoSize]
    ) -> Optional[TelegramPhotoSize]:
        return message_handlers.select_photo(photos)

    def _document_is_image(self, document: TelegramDocument) -> bool:
        return message_handlers.document_is_image(document)

    def _select_image_candidate(
        self, message: TelegramMessage
    ) -> Optional[TelegramMediaCandidate]:
        return message_handlers.select_image_candidate(message)

    def _select_voice_candidate(
        self, message: TelegramMessage
    ) -> Optional[TelegramMediaCandidate]:
        return message_handlers.select_voice_candidate(message)

    async def _handle_media_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        await message_handlers.handle_media_message(
            self,
            message,
            runtime,
            caption_text,
            placeholder_id=placeholder_id,
        )

    def _with_conversation_id(
        self, message: str, *, chat_id: int, thread_id: Optional[int]
    ) -> str:
        return _with_conversation_id(message, chat_id=chat_id, thread_id=thread_id)

    def _current_chat_operation_id(self) -> Optional[str]:
        operation_id = _CURRENT_TELEGRAM_OPERATION_ID.get()
        return str(operation_id).strip() or None if operation_id is not None else None

    def _with_chat_operation(self, operation_id: Optional[str], work: Any) -> Any:
        normalized_operation_id = (
            str(operation_id or "").strip() or None
            if operation_id is not None
            else None
        )
        if normalized_operation_id is None:
            return work

        async def wrapped() -> Any:
            token = _CURRENT_TELEGRAM_OPERATION_ID.set(normalized_operation_id)
            try:
                return await work()
            finally:
                _CURRENT_TELEGRAM_OPERATION_ID.reset(token)

        return wrapped

    async def _register_accepted_chat_operation(
        self,
        *,
        operation_id: str,
        surface_operation_key: str,
        conversation_id: Optional[str],
        chat_id: Optional[int],
        thread_id: Optional[int],
        user_id: Optional[int],
        message_id: Optional[int],
        kind: str,
    ) -> bool:
        registration = self._chat_operation_store.register_operation(
            operation_id=operation_id,
            surface_kind="telegram",
            surface_operation_key=surface_operation_key,
            state=ChatOperationState.RECEIVED,
            conversation_id=conversation_id,
            metadata={
                "kind": kind,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "user_id": user_id,
                "message_id": message_id,
            },
        )
        self._chat_operation_store.patch_operation(
            registration.snapshot.operation_id,
            ack_requested_at=now_iso(),
        )
        return registration.inserted

    async def _mark_chat_operation_state(
        self,
        operation_id: Optional[str],
        *,
        state: ChatOperationState,
        validate_transition: bool = False,
        **changes: Any,
    ) -> None:
        normalized_operation_id = str(operation_id or "").strip()
        if not normalized_operation_id:
            return
        try:
            self._chat_operation_store.patch_operation(
                normalized_operation_id,
                state=state,
                validate_transition=validate_transition,
                **changes,
            )
        except ValueError:
            snapshot = self._chat_operation_store.get_operation(normalized_operation_id)
            if snapshot is None:
                return
            if snapshot.first_visible_feedback_at is not None:
                changes["first_visible_feedback_at"] = (
                    snapshot.first_visible_feedback_at
                )
            self._chat_operation_store.upsert_operation(
                snapshot.__class__(
                    **{
                        **snapshot.__dict__,
                        "state": state,
                        "updated_at": now_iso(),
                        **changes,
                    }
                )
            )

    async def _should_process_update(self, key: str, update_id: int) -> bool:
        return await self._update_deduper.should_process(
            key,
            update_id,
            store=self._store,
            persist_interval=self._config.cache.update_id_persist_interval_seconds,
        )

    async def _maybe_persist_update_id(self, key: str, update_id: int) -> None:
        await self._update_deduper._maybe_persist(
            key,
            update_id,
            store=self._store,
            persist_interval=self._config.cache.update_id_persist_interval_seconds,
        )

    @property
    def _last_update_ids(self) -> dict[str, int]:
        return self._update_deduper._last_update_ids

    @property
    def _last_update_persisted_at(self) -> dict[str, float]:
        return self._update_deduper._last_persisted_at

    async def _typing_session_active(self, key: tuple[int, Optional[int]]) -> bool:
        return await self._typing_manager.is_active(key)

    async def _begin_typing_indicator(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        await self._typing_manager.begin(chat_id, thread_id)

    async def _end_typing_indicator(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        await self._typing_manager.end(chat_id, thread_id)

    @property
    def _typing_sessions(self) -> dict[tuple[int, Optional[int]], int]:
        return self._typing_manager._sessions

    @property
    def _typing_tasks(self) -> dict[tuple[int, Optional[int]], asyncio.Task[None]]:
        return self._typing_manager._tasks

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        await callback_handlers.handle_callback(self, callback)

    def _enqueue_topic_work(
        self,
        key: str,
        work: Any,
        *,
        force_queue: bool = False,
        item_id: Optional[str] = None,
        item_label: Optional[str] = None,
    ) -> Optional[str]:
        runtime = self._router.runtime_for(key)
        wrapped = self._wrap_topic_work(key, work)
        if force_queue or self._config.concurrency.per_topic_queue:
            return runtime.queue.enqueue_detached(
                wrapped,
                item_id=item_id,
                item_label=item_label,
            )
        self._spawn_task(wrapped())
        return None

    def _get_queue_status_message_id(
        self, chat_id: int, thread_id: Optional[int]
    ) -> Optional[int]:
        return self._queue_status_message_manager.get(chat_id, thread_id)

    def _set_queue_status_message_id(
        self, chat_id: int, thread_id: Optional[int], message_id: int
    ) -> None:
        self._queue_status_message_manager.set(chat_id, thread_id, message_id)

    def _clear_queue_status_message_id(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        self._queue_status_message_manager.clear(chat_id, thread_id)

    @property
    def _queue_status_message_map(self) -> dict[tuple[int, Optional[int]], int]:
        return self._queue_status_message_manager.map

    @property
    def _queue_status_message_timestamps(
        self,
    ) -> dict[tuple[int, Optional[int]], float]:
        return self._queue_status_message_manager.timestamps

    def _queue_status_keyboard(
        self, items: list[QueueStatusItem]
    ) -> Optional[dict[str, Any]]:
        rows: list[list[InlineButton]] = []
        for index, item in enumerate(items[:QUEUE_STATUS_ITEM_LIMIT], start=1):
            source = str(item.item_id)
            rows.append(
                [
                    InlineButton(
                        f"Cancel {index}",
                        encode_cancel_callback(f"queue_cancel:{source}"),
                    ),
                    InlineButton(
                        f"Send {index}",
                        encode_cancel_callback(f"queue_interrupt_send:{source}"),
                    ),
                ]
            )
        if not rows:
            return None
        return build_inline_keyboard(rows)

    async def _queue_status_items_for_topic(
        self, topic_key: str
    ) -> list[QueueStatusItem]:
        runtime = self._router.runtime_for(topic_key)
        pending_items = getattr(runtime.queue, "pending_items", None)
        if not callable(pending_items):
            return []
        return coerce_queue_status_items(
            pending_items(),
            fallback_prefix="Request",
        )

    async def _refresh_topic_queue_status_message(
        self,
        *,
        topic_key: str,
        chat_id: int,
        thread_id: Optional[int],
        reply_to_message_id: Optional[int] = None,
        repost: bool = False,
    ) -> Optional[int]:
        items = await self._queue_status_items_for_topic(topic_key)
        existing_message_id = self._get_queue_status_message_id(chat_id, thread_id)
        if not items:
            if existing_message_id is not None:
                self._clear_queue_status_message_id(chat_id, thread_id)
                await self._delete_message(
                    chat_id,
                    existing_message_id,
                    thread_id=thread_id,
                )
            return None
        text = format_queue_status_text(items)
        reply_markup = self._queue_status_keyboard(items)
        if existing_message_id is not None and not repost:
            edited = await self._edit_message_text(
                chat_id,
                existing_message_id,
                text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            if edited:
                self._set_queue_status_message_id(
                    chat_id, thread_id, existing_message_id
                )
                return existing_message_id
            # Stale anchor (for example message deleted); drop it and resend below.
            self._clear_queue_status_message_id(chat_id, thread_id)
            existing_message_id = None
        message_id = await self._send_placeholder(
            chat_id,
            thread_id=thread_id,
            reply_to=reply_to_message_id,
            text=text,
            reply_markup=reply_markup,
        )
        if message_id is None:
            return existing_message_id
        self._set_queue_status_message_id(chat_id, thread_id, message_id)
        if existing_message_id is not None and existing_message_id != message_id:
            await self._delete_message(
                chat_id,
                existing_message_id,
                thread_id=thread_id,
            )
        return message_id

    def _wrap_placeholder_work(
        self,
        *,
        chat_id: int,
        placeholder_id: Optional[int],
        work: Any,
    ) -> Any:
        if placeholder_id is None:
            return work

        async def wrapped() -> Any:
            try:
                return await work()
            finally:
                await self._delete_message(chat_id, placeholder_id)

        return wrapped

    def _claim_queued_placeholder(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._queued_placeholder_manager.claim(chat_id, message_id)

    def _get_queued_placeholder(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._queued_placeholder_manager.get(chat_id, message_id)

    def _set_queued_placeholder(
        self, chat_id: int, message_id: int, placeholder_id: int
    ) -> None:
        self._queued_placeholder_manager.set(chat_id, message_id, placeholder_id)

    def _clear_queued_placeholder(self, chat_id: int, message_id: int) -> None:
        self._queued_placeholder_manager.clear(chat_id, message_id)

    @property
    def _queued_placeholder_map(self) -> dict[tuple[int, int], int]:
        return self._queued_placeholder_manager.map

    @property
    def _queued_placeholder_timestamps(self) -> dict[tuple[int, int], float]:
        return self._queued_placeholder_manager.timestamps

    def _wrap_topic_work(self, key: str, work: Any) -> Any:
        conversation_id = None
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
            conversation_id = topic_key(chat_id, thread_id)
        except ValueError:
            conversation_id = None

        if not conversation_id:
            return work

        async def wrapped() -> Any:
            token = set_conversation_id(conversation_id)
            try:
                return await work()
            finally:
                reset_conversation_id(token)

        return wrapped
