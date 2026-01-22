from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional, Sequence

if TYPE_CHECKING:
    from .progress_stream import TurnProgressTracker
    from .state import TelegramTopicRecord

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.flows import FlowStore
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.locks import process_alive
from ...core.logging_utils import log_event
from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.state import now_iso
from ...core.text_delta_coalescer import TextDeltaCoalescer
from ...core.utils import (
    build_opencode_supervisor,
    canonicalize_path,
)
from ...housekeeping import HousekeepingConfig, run_housekeeping_for_roots
from ...manifest import load_manifest
from ...tickets.outbox import resolve_outbox_paths
from ...voice import VoiceConfig, VoiceService
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from .adapter import (
    TelegramBotClient,
    TelegramCallbackQuery,
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramUpdate,
    TelegramUpdatePoller,
)
from .commands_registry import build_command_payloads, diff_command_lists
from .config import (
    AppServerUnavailableError,
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramMediaCandidate,
)
from .constants import (
    CACHE_CLEANUP_INTERVAL_SECONDS,
    COALESCE_BUFFER_TTL_SECONDS,
    DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    DEFAULT_WORKSPACE_STATE_ROOT,
    MEDIA_BATCH_BUFFER_TTL_SECONDS,
    MODEL_PENDING_TTL_SECONDS,
    OVERSIZE_WARNING_TTL_SECONDS,
    PENDING_APPROVAL_TTL_SECONDS,
    PENDING_QUESTION_TTL_SECONDS,
    PROGRESS_STREAM_TTL_SECONDS,
    QUEUED_PLACEHOLDER_TEXT,
    REASONING_BUFFER_TTL_SECONDS,
    SELECTION_STATE_TTL_SECONDS,
    TURN_PREVIEW_TTL_SECONDS,
    UPDATE_ID_PERSIST_INTERVAL_SECONDS,
    TurnKey,
)
from .dispatch import dispatch_update
from .handlers import callbacks as callback_handlers
from .handlers import messages as message_handlers
from .handlers.approvals import TelegramApprovalHandlers
from .handlers.commands import build_command_specs
from .handlers.commands_runtime import TelegramCommandHandlers
from .handlers.messages import _CoalescedBuffer
from .handlers.questions import TelegramQuestionHandlers
from .handlers.selections import TelegramSelectionHandlers
from .helpers import (
    ModelOption,
    _lock_payload_summary,
    _read_lock_payload,
    _split_topic_key,
    _telegram_lock_path,
    _truncate_text,
    _with_conversation_id,
)
from .notifications import TelegramNotificationHandlers
from .outbox import TelegramOutboxManager
from .runtime import TelegramRuntimeHelpers
from .state import (
    TelegramStateStore,
    TopicRouter,
    parse_topic_key,
    topic_key,
)
from .transport import TelegramMessageTransport
from .types import (
    CompactState,
    ModelPickerState,
    PendingApproval,
    PendingQuestion,
    ReviewCommitSelectionState,
    SelectionState,
    TurnContext,
)
from .voice import TelegramVoiceManager

TICKET_FLOW_WATCH_INTERVAL_SECONDS = 20


def _build_opencode_supervisor(
    config: TelegramBotConfig,
    *,
    logger: logging.Logger,
) -> Optional[OpenCodeSupervisor]:
    raw_command = os.environ.get("CAR_OPENCODE_COMMAND")
    opencode_binary = config.agent_binaries.get("opencode")

    supervisor = build_opencode_supervisor(
        opencode_command=[raw_command] if raw_command else None,
        opencode_binary=opencode_binary,
        workspace_root=config.root,
        logger=logger,
        request_timeout=None,
        max_handles=config.app_server_max_handles,
        idle_ttl_seconds=config.app_server_idle_ttl_seconds,
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


class TelegramBotService(
    TelegramRuntimeHelpers,
    TelegramMessageTransport,
    TelegramNotificationHandlers,
    TelegramApprovalHandlers,
    TelegramQuestionHandlers,
    TelegramSelectionHandlers,
    TelegramCommandHandlers,
):
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
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._update_repo_url = update_repo_url
        self._update_repo_ref = update_repo_ref
        self._allowlist = config.allowlist()
        self._store = TelegramStateStore(
            config.state_file, default_approval_mode=config.defaults.approval_mode
        )
        self._router = TopicRouter(self._store)
        self._app_server_state_root = Path(DEFAULT_WORKSPACE_STATE_ROOT).expanduser()
        self._app_server_supervisor = WorkspaceAppServerSupervisor(
            config.app_server_command,
            state_root=self._app_server_state_root,
            env_builder=self._build_workspace_env,
            approval_handler=self._handle_approval_request,
            notification_handler=self._handle_app_server_notification,
            logger=self._logger,
            max_handles=config.app_server_max_handles,
            idle_ttl_seconds=config.app_server_idle_ttl_seconds,
        )
        self._opencode_supervisor = _build_opencode_supervisor(
            config,
            logger=self._logger,
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
        self._model_options: dict[str, ModelPickerState] = {}
        self._model_pending: dict[str, ModelOption] = {}
        self._agent_options: dict[str, SelectionState] = {}
        self._voice_config = voice_config
        self._voice_service = voice_service
        self._housekeeping_config = housekeeping_config
        if self._voice_service is None and voice_config is not None:
            try:
                self._voice_service = VoiceService(voice_config, logger=self._logger)
            except Exception as exc:
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
        self._turn_progress_updated_at: dict[TurnKey, float] = {}
        self._turn_progress_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_heartbeat_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_locks: dict[TurnKey, asyncio.Lock] = {}
        self._oversize_warnings: set[TurnKey] = set()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_questions: dict[str, PendingQuestion] = {}
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._update_options: dict[str, SelectionState] = {}
        self._update_confirm_options: dict[str, bool] = {}
        self._review_commit_options: dict[str, ReviewCommitSelectionState] = {}
        self._review_commit_subjects: dict[str, dict[str, str]] = {}
        self._pending_review_custom: dict[str, dict[str, Any]] = {}
        self._compact_pending: dict[str, CompactState] = {}
        self._coalesced_buffers: dict[str, _CoalescedBuffer] = {}
        self._coalesce_locks: dict[str, asyncio.Lock] = {}
        self._media_batch_buffers: dict[str, message_handlers._MediaBatchBuffer] = {}
        self._media_batch_locks: dict[str, asyncio.Lock] = {}
        self._outbox_inflight: set[str] = set()
        self._outbox_lock: Optional[asyncio.Lock] = None
        self._queued_placeholder_map: dict[tuple[int, int], int] = {}
        self._queued_placeholder_timestamps: dict[tuple[int, int], float] = {}
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
        self._cache_timestamps: dict[str, dict[object, float]] = {}
        self._last_update_ids: dict[str, int] = {}
        self._last_update_persisted_at: dict[str, float] = {}
        self._spawned_tasks: set[asyncio.Task[Any]] = set()
        self._outbox_manager = TelegramOutboxManager(
            self._store,
            send_message=self._send_message,
            edit_message_text=self._edit_message_text,
            delete_message=self._delete_message,
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

    async def _housekeeping_roots(self) -> list[Path]:
        roots: set[Path] = set()
        try:
            state = await self._store.load()
            for record in state.topics.values():
                if isinstance(record.workspace_path, str) and record.workspace_path:
                    roots.add(Path(record.workspace_path).expanduser().resolve())
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.housekeeping.state_failed",
                exc=exc,
            )
        if self._hub_root and self._manifest_path and self._manifest_path.exists():
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                for repo in manifest.repos:
                    roots.add((self._hub_root / repo.path).resolve())
            except Exception as exc:
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
        roots: set[Path] = set()
        try:
            state = await self._store.load()
            for record in state.topics.values():
                if isinstance(record.workspace_path, str) and record.workspace_path:
                    roots.add(Path(record.workspace_path).expanduser().resolve())
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.prewarm.state_failed",
                exc=exc,
            )
        return sorted(roots)

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

        async def prewarm_one(workspace_root: Path) -> None:
            nonlocal prewarmed_count, failed_count
            async with sem:
                try:
                    await self._app_server_supervisor.get_client(workspace_root)
                    prewarmed_count += 1
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.prewarm.client_ready",
                        workspace_root=str(workspace_root),
                    )
                except Exception as exc:
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
        )

    async def _housekeeping_loop(self) -> None:
        config = self._housekeeping_config
        if config is None or not config.enabled:
            return
        interval = max(config.interval_seconds, 1)
        while True:
            try:
                roots = await self._housekeeping_roots()
                if roots:
                    await asyncio.to_thread(
                        run_housekeeping_for_roots,
                        config,
                        roots,
                        self._logger,
                    )
                await self._app_server_supervisor.prune_idle()
                if self._opencode_supervisor is not None:
                    await self._opencode_supervisor.prune_idle()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.housekeeping.failed",
                    exc=exc,
                )
            await asyncio.sleep(interval)

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
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = _read_lock_payload(lock_path)
            pid = existing.get("pid") if isinstance(existing, dict) else None
            if isinstance(pid, int) and process_alive(pid):
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
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
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
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        self._instance_lock_path = lock_path
        log_event(
            self._logger,
            logging.INFO,
            "telegram.lock.acquired",
            lock_path=str(lock_path),
            **_lock_payload_summary(payload),
        )

    def _release_instance_lock(self) -> None:
        lock_path = self._instance_lock_path
        if lock_path is None:
            return
        existing = _read_lock_payload(lock_path)
        if isinstance(existing, dict):
            pid = existing.get("pid")
            if isinstance(pid, int) and pid != os.getpid():
                return
        try:
            lock_path.unlink()
        except OSError:
            pass
        self._instance_lock_path = None

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        if self._turn_semaphore is None:
            self._turn_semaphore = asyncio.Semaphore(
                self._config.concurrency.max_parallel_turns
            )
        return self._turn_semaphore

    async def run_polling(self) -> None:
        if self._config.mode != "polling":
            raise TelegramBotConfigError(
                f"Unsupported telegram_bot.mode '{self._config.mode}'"
            )
        self._config.validate()
        self._acquire_instance_lock()
        # Bind the semaphore to the running loop to avoid cross-loop await failures.
        self._turn_semaphore = asyncio.Semaphore(
            self._config.concurrency.max_parallel_turns
        )
        self._outbox_manager.start()
        self._voice_manager.start()
        try:
            await self._prime_bot_identity()
            await self._register_bot_commands()
            await self._restore_pending_approvals()
            await self._outbox_manager.restore()
            await self._voice_manager.restore()
            await self._prime_poller_offset()
            self._outbox_task = asyncio.create_task(self._outbox_manager.run_loop())
            self._voice_task = asyncio.create_task(self._voice_manager.run_loop())
            self._housekeeping_task = asyncio.create_task(self._housekeeping_loop())
            self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())
            self._ticket_flow_watch_task = asyncio.create_task(
                self._ticket_flow_watch_loop()
            )
            self._spawn_task(self._prewarm_workspace_clients())
            log_event(
                self._logger,
                logging.INFO,
                "telegram.bot.started",
                mode=self._config.mode,
                poll_timeout=self._config.poll_timeout_seconds,
                allowed_updates=list(self._config.poll_allowed_updates),
                allowed_chats=len(self._config.allowed_chat_ids),
                allowed_users=len(self._config.allowed_user_ids),
                require_topics=self._config.require_topics,
                max_parallel_turns=self._config.concurrency.max_parallel_turns,
                per_topic_queue=self._config.concurrency.per_topic_queue,
                media_enabled=self._config.media.enabled,
                media_images=self._config.media.images,
                media_voice=self._config.media.voice,
                app_server_turn_timeout_seconds=self._config.app_server_turn_timeout_seconds,
                poller_offset=self._poller.offset,
            )
            try:
                await self._maybe_send_update_status_notice()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.update.notify_failed",
                    exc=exc,
                )
            try:
                await self._maybe_send_compact_status_notice()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.compact.notify_failed",
                    exc=exc,
                )
            while True:
                updates = []
                try:
                    updates = await self._poller.poll(
                        timeout=self._config.poll_timeout_seconds
                    )
                    if self._poller.offset is not None:
                        await self._record_poll_offset(updates)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.poll.failed",
                        exc=exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                for update in updates:
                    self._spawn_task(dispatch_update(self, update))
        finally:
            try:
                if self._outbox_task is not None:
                    self._outbox_task.cancel()
                    try:
                        await self._outbox_task
                    except asyncio.CancelledError:
                        pass
                if self._voice_task is not None:
                    self._voice_task.cancel()
                    try:
                        await self._voice_task
                    except asyncio.CancelledError:
                        pass
                if self._housekeeping_task is not None:
                    self._housekeeping_task.cancel()
                    try:
                        await self._housekeeping_task
                    except asyncio.CancelledError:
                        pass
                if self._cache_cleanup_task is not None:
                    self._cache_cleanup_task.cancel()
                    try:
                        await self._cache_cleanup_task
                    except asyncio.CancelledError:
                        pass
                if self._ticket_flow_watch_task is not None:
                    self._ticket_flow_watch_task.cancel()
                    try:
                        await self._ticket_flow_watch_task
                    except asyncio.CancelledError:
                        pass
                if self._spawned_tasks:
                    for task in list(self._spawned_tasks):
                        task.cancel()
                    await asyncio.gather(*self._spawned_tasks, return_exceptions=True)
            finally:
                try:
                    await self._bot.close()
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.bot.close_failed",
                        exc=exc,
                    )
                try:
                    await self._app_server_supervisor.close_all()
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.app_server.close_failed",
                        exc=exc,
                    )
                try:
                    await self._store.close()
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.state.close_failed",
                        exc=exc,
                    )
                self._release_instance_lock()

    async def _prime_bot_identity(self) -> None:
        try:
            payload = await self._bot.get_me()
        except Exception:
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
            except Exception as exc:
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
            except Exception as exc:
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
        except Exception as exc:
            log_event(self._logger, logging.WARNING, "telegram.task.failed", exc=exc)

    def _touch_cache_timestamp(self, cache_name: str, key: object) -> None:
        cache = self._cache_timestamps.setdefault(cache_name, {})
        cache[key] = time.monotonic()

    def _evict_expired_cache_entries(self, cache_name: str, ttl_seconds: float) -> None:
        cache = self._cache_timestamps.get(cache_name)
        if not cache:
            return
        now = time.monotonic()
        expired: list[object] = []
        for key, updated_at in cache.items():
            if (now - updated_at) > ttl_seconds:
                expired.append(key)
        if not expired:
            return
        for key in expired:
            cache.pop(key, None)
            if cache_name == "reasoning_buffers":
                self._reasoning_buffers.pop(key, None)
            elif cache_name == "turn_preview":
                self._turn_preview_text.pop(key, None)
                self._turn_preview_updated_at.pop(key, None)
            elif cache_name == "progress_trackers":
                self._turn_progress_trackers.pop(key, None)
                self._turn_progress_rendered.pop(key, None)
                self._turn_progress_updated_at.pop(key, None)
                task = self._turn_progress_tasks.pop(key, None)
                if task and not task.done():
                    task.cancel()
                heartbeat_task = self._turn_progress_heartbeat_tasks.pop(key, None)
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
            elif cache_name == "oversize_warnings":
                self._oversize_warnings.discard(key)
            elif cache_name == "coalesced_buffers":
                self._coalesced_buffers.pop(key, None)
                self._coalesce_locks.pop(key, None)
            elif cache_name == "media_batch_buffers":
                self._media_batch_buffers.pop(key, None)
                self._media_batch_locks.pop(key, None)
            elif cache_name == "resume_options":
                self._resume_options.pop(key, None)
            elif cache_name == "bind_options":
                self._bind_options.pop(key, None)
            elif cache_name == "agent_options":
                self._agent_options.pop(key, None)
            elif cache_name == "update_options":
                self._update_options.pop(key, None)
            elif cache_name == "update_confirm_options":
                self._update_confirm_options.pop(key, None)
            elif cache_name == "review_commit_options":
                self._review_commit_options.pop(key, None)
            elif cache_name == "review_commit_subjects":
                self._review_commit_subjects.pop(key, None)
            elif cache_name == "pending_review_custom":
                self._pending_review_custom.pop(key, None)
            elif cache_name == "compact_pending":
                self._compact_pending.pop(key, None)
            elif cache_name == "model_options":
                self._model_options.pop(key, None)
            elif cache_name == "model_pending":
                self._model_pending.pop(key, None)
            elif cache_name == "pending_approvals":
                self._pending_approvals.pop(key, None)
            elif cache_name == "pending_questions":
                self._pending_questions.pop(key, None)

    async def _cache_cleanup_loop(self) -> None:
        interval = max(CACHE_CLEANUP_INTERVAL_SECONDS, 1.0)
        while True:
            await asyncio.sleep(interval)
            self._evict_expired_cache_entries(
                "reasoning_buffers", REASONING_BUFFER_TTL_SECONDS
            )
            self._evict_expired_cache_entries("turn_preview", TURN_PREVIEW_TTL_SECONDS)
            self._evict_expired_cache_entries(
                "progress_trackers", PROGRESS_STREAM_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "oversize_warnings", OVERSIZE_WARNING_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "coalesced_buffers", COALESCE_BUFFER_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "media_batch_buffers", MEDIA_BATCH_BUFFER_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "resume_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "bind_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "agent_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "update_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "update_confirm_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "review_commit_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "review_commit_subjects", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "pending_review_custom", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "compact_pending", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "model_options", SELECTION_STATE_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "model_pending", MODEL_PENDING_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "pending_approvals", PENDING_APPROVAL_TTL_SECONDS
            )
            self._evict_expired_cache_entries(
                "pending_questions", PENDING_QUESTION_TTL_SECONDS
            )
            now = time.monotonic()
            expired_placeholders = []
            for key, timestamp in self._queued_placeholder_timestamps.items():
                if (now - timestamp) > PENDING_APPROVAL_TTL_SECONDS:
                    expired_placeholders.append(key)
            for key in expired_placeholders:
                self._queued_placeholder_map.pop(key, None)
                self._queued_placeholder_timestamps.pop(key, None)

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
        if not entries:
            return None

        def score(entry: tuple[str, "TelegramTopicRecord"]) -> tuple[int, float, str]:
            key, record = entry
            thread_id = None
            try:
                _chat_id, thread_id, _scope = parse_topic_key(key)
            except Exception:
                thread_id = None
            active_raw = getattr(record, "active_thread_id", None)
            try:
                active_thread = int(active_raw) if active_raw is not None else None
            except (TypeError, ValueError):
                active_thread = None
            active_match = (
                int(thread_id) == active_thread if thread_id is not None else False
            )
            return (1 if active_match else 0, self._parse_last_active(record), key)

        return max(entries, key=score)

    @staticmethod
    def _set_ticket_handoff_marker(
        value: Optional[str],
    ) -> "Callable[[TelegramTopicRecord], None]":
        def apply(topic: "TelegramTopicRecord") -> None:
            topic.last_ticket_handoff_seq = value

        return apply

    async def _ticket_flow_watch_loop(self) -> None:
        interval = max(TICKET_FLOW_WATCH_INTERVAL_SECONDS, 1)
        while True:
            try:
                await self._watch_ticket_flow_pauses()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.ticket_flow.watch_failed",
                    exc=exc,
                )
            await asyncio.sleep(interval)

    async def _watch_ticket_flow_pauses(self) -> None:
        topics = await self._store.list_topics()
        if not topics:
            return
        workspace_topics: dict[Path, list[tuple[str, "TelegramTopicRecord"]]] = {}
        for key, record in topics.items():
            if not isinstance(record.workspace_path, str) or not record.workspace_path:
                continue
            workspace_root = canonicalize_path(Path(record.workspace_path))
            workspace_topics.setdefault(workspace_root, []).append((key, record))

        tasks = [
            asyncio.create_task(self._notify_ticket_flow_pause(workspace_root, entries))
            for workspace_root, entries in workspace_topics.items()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _notify_ticket_flow_pause(
        self,
        workspace_root: Path,
        entries: list[tuple[str, "TelegramTopicRecord"]],
    ) -> None:
        try:
            pause = await asyncio.to_thread(
                self._load_ticket_flow_pause, workspace_root
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return
        if pause is None:
            return
        run_id, seq, content = pause
        marker = f"{run_id}:{seq}"
        pending = [
            (key, record)
            for key, record in entries
            if record.last_ticket_handoff_seq != marker
        ]
        if not pending:
            return
        primary = self._select_ticket_flow_topic(pending)
        if not primary:
            return
        message_text = self._format_ticket_flow_pause_message(run_id, seq, content)
        updates: list[tuple[str, Optional[str]]] = [
            (key, record.last_ticket_handoff_seq) for key, record in pending
        ]
        for key, _previous in updates:
            await self._store.update_topic(key, self._set_ticket_handoff_marker(marker))

        primary_key, _primary_record = primary
        try:
            chat_id, thread_id, _scope = parse_topic_key(primary_key)
        except Exception:
            for key, previous in updates:
                await self._store.update_topic(
                    key, self._set_ticket_handoff_marker(previous)
                )
            return

        try:
            await self._send_message_with_outbox(
                chat_id,
                message_text,
                thread_id=thread_id,
                reply_to=None,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.notify_failed",
                exc=exc,
                topic_key=primary_key,
                run_id=run_id,
                seq=seq,
            )
            for key, previous in updates:
                await self._store.update_topic(
                    key, self._set_ticket_handoff_marker(previous)
                )

    def _load_ticket_flow_pause(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, str]]:
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        store = FlowStore(db_path)
        try:
            store.initialize()
            runs = store.list_flow_runs(
                flow_type="ticket_flow", status=FlowRunStatus.PAUSED
            )
            if not runs:
                return None
            latest = runs[0]
            runs_dir_raw = latest.input_data.get("runs_dir")
            runs_dir = (
                Path(runs_dir_raw)
                if isinstance(runs_dir_raw, str) and runs_dir_raw
                else Path(".codex-autorunner/runs")
            )
            paths = resolve_outbox_paths(
                workspace_root=workspace_root, runs_dir=runs_dir, run_id=latest.id
            )
            history_dir = paths.handoff_history_dir
            seq = self._latest_handoff_seq(history_dir)
            if not seq:
                reason = self._format_ticket_flow_pause_reason(latest)
                return latest.id, "paused", reason
            message_path = history_dir / seq / "USER_MESSAGE.md"
            try:
                content = message_path.read_text(encoding="utf-8")
            except OSError:
                return None
            return latest.id, seq, content
        finally:
            store.close()

    def _latest_handoff_seq(self, history_dir: Path) -> Optional[str]:
        if not history_dir.exists() or not history_dir.is_dir():
            return None
        seqs = [
            child.name
            for child in history_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name.isdigit()
        ]
        if not seqs:
            return None
        return max(seqs)

    def _format_ticket_flow_pause_reason(self, record: "FlowRunRecord") -> str:
        state = record.state or {}
        engine = state.get("ticket_engine") or {}
        reason = (
            engine.get("reason") or record.error_message or "Paused without details."
        )
        return f"Reason: {reason}"

    def _format_ticket_flow_pause_message(
        self, run_id: str, seq: str, content: str
    ) -> str:
        trimmed = _truncate_text(content.strip() or "(no handoff message)", 3000)
        return (
            f"Ticket flow paused (run {run_id}). Latest handoff #{seq}:\n\n"
            f"{trimmed}\n\nUse /flow resume to continue."
        )

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
        if record and record.agent == "opencode":
            session_id = record.active_thread_id
            workspace_path = record.workspace_path
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
            except Exception as exc:
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
            client = await self._client_for_workspace(
                record.workspace_path if record else None
            )
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
        except Exception as exc:
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
        await message_handlers.handle_message(self, message)

    def _should_bypass_topic_queue(self, message: TelegramMessage) -> bool:
        return message_handlers.should_bypass_topic_queue(self, message)

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

    async def _should_process_update(self, key: str, update_id: int) -> bool:
        if not isinstance(update_id, int):
            return True
        if isinstance(update_id, bool):
            return True
        last_id = self._last_update_ids.get(key)
        if last_id is None:
            record = None
            try:
                record = await self._store.get_topic(key)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.update_id.load.failed",
                    exc=exc,
                    topic_key=key,
                    update_id=update_id,
                )
            last_id = record.last_update_id if record else None
            if isinstance(last_id, int) and not isinstance(last_id, bool):
                self._last_update_ids[key] = last_id
            else:
                last_id = None
        if isinstance(last_id, int) and update_id <= last_id:
            return False
        self._last_update_ids[key] = update_id
        try:
            await self._maybe_persist_update_id(key, update_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update_id.persist.failed",
                exc=exc,
                topic_key=key,
                update_id=update_id,
            )
        return True

    async def _maybe_persist_update_id(self, key: str, update_id: int) -> None:
        now = time.monotonic()
        last_persisted = self._last_update_persisted_at.get(key, 0.0)
        if (now - last_persisted) < UPDATE_ID_PERSIST_INTERVAL_SECONDS:
            return

        def apply(record: "TelegramTopicRecord") -> None:
            record.last_update_id = update_id

        await self._store.update_topic(key, apply)
        self._last_update_persisted_at[key] = now

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        await callback_handlers.handle_callback(self, callback)

    def _enqueue_topic_work(
        self, key: str, work: Any, *, force_queue: bool = False
    ) -> None:
        runtime = self._router.runtime_for(key)
        wrapped = self._wrap_topic_work(key, work)
        if force_queue or self._config.concurrency.per_topic_queue:
            self._spawn_task(runtime.queue.enqueue(wrapped))
        else:
            self._spawn_task(wrapped())

    async def _maybe_send_queued_placeholder(
        self, message: TelegramMessage, *, topic_key: str
    ) -> Optional[int]:
        runtime = self._router.runtime_for(topic_key)
        is_busy = runtime.current_turn_id is not None or runtime.queue.pending() > 0
        if not is_busy:
            return None
        placeholder_id = await self._send_placeholder(
            message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            text=QUEUED_PLACEHOLDER_TEXT,
        )
        if placeholder_id is None:
            return None
        self._set_queued_placeholder(
            message.chat_id, message.message_id, placeholder_id
        )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.placeholder.queued",
            topic_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            placeholder_id=placeholder_id,
        )
        return placeholder_id

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
        placeholder_id = self._queued_placeholder_map.pop((chat_id, message_id), None)
        self._queued_placeholder_timestamps.pop((chat_id, message_id), None)
        return placeholder_id

    def _get_queued_placeholder(self, chat_id: int, message_id: int) -> Optional[int]:
        return self._queued_placeholder_map.get((chat_id, message_id))

    def _set_queued_placeholder(
        self, chat_id: int, message_id: int, placeholder_id: int
    ) -> None:
        self._queued_placeholder_map[(chat_id, message_id)] = placeholder_id
        self._queued_placeholder_timestamps[(chat_id, message_id)] = time.monotonic()

    def _clear_queued_placeholder(self, chat_id: int, message_id: int) -> None:
        self._queued_placeholder_map.pop((chat_id, message_id), None)
        self._queued_placeholder_timestamps.pop((chat_id, message_id), None)

    def _wrap_topic_work(self, key: str, work: Any) -> Any:
        conversation_id = None
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
            conversation_id = topic_key(chat_id, thread_id)
        except Exception:
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
