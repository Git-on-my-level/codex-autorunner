from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Coroutine, Optional, Sequence

if TYPE_CHECKING:
    from .state import TelegramTopicRecord

from ...core.locks import process_alive
from ...core.logging_utils import log_event
from ...core.state import now_iso
from ...housekeeping import HousekeepingConfig, run_housekeeping_for_roots
from ...manifest import load_manifest
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
from .config import (
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramMediaCandidate,
)
from .constants import (
    DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    DEFAULT_WORKSPACE_STATE_ROOT,
    TurnKey,
)
from .dispatch import dispatch_update
from .handlers import callbacks as callback_handlers
from .handlers import messages as message_handlers
from .handlers.approvals import TelegramApprovalHandlers
from .handlers.commands import build_command_specs
from .handlers.commands_runtime import TelegramCommandHandlers
from .handlers.messages import _CoalescedBuffer
from .handlers.selections import TelegramSelectionHandlers
from .helpers import (
    ModelOption,
    _lock_payload_summary,
    _read_lock_payload,
    _split_topic_key,
    _telegram_lock_path,
    _with_conversation_id,
)
from .notifications import TelegramNotificationHandlers
from .outbox import TelegramOutboxManager
from .runtime import TelegramRuntimeHelpers
from .state import (
    TelegramStateStore,
    TopicRouter,
)
from .transport import TelegramMessageTransport
from .types import (
    ModelPickerState,
    PendingApproval,
    ReviewCommitSelectionState,
    SelectionState,
    TurnContext,
)
from .voice import TelegramVoiceManager


class TelegramBotService(
    TelegramRuntimeHelpers,
    TelegramMessageTransport,
    TelegramNotificationHandlers,
    TelegramApprovalHandlers,
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
        )
        self._bot = TelegramBotClient(config.bot_token or "", logger=self._logger)
        self._poller = TelegramUpdatePoller(
            self._bot, allowed_updates=config.poll_allowed_updates
        )
        self._model_options: dict[str, ModelPickerState] = {}
        self._model_pending: dict[str, ModelOption] = {}
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
        self._reasoning_buffers: dict[str, str] = {}
        self._turn_preview_text: dict[TurnKey, str] = {}
        self._turn_preview_updated_at: dict[TurnKey, float] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._update_options: dict[str, SelectionState] = {}
        self._update_confirm_options: dict[str, bool] = {}
        self._review_commit_options: dict[str, ReviewCommitSelectionState] = {}
        self._review_commit_subjects: dict[str, dict[str, str]] = {}
        self._pending_review_custom: dict[str, dict[str, Any]] = {}
        self._coalesced_buffers: dict[str, _CoalescedBuffer] = {}
        self._coalesce_locks: dict[str, asyncio.Lock] = {}
        self._outbox_inflight: set[str] = set()
        self._outbox_lock: Optional[asyncio.Lock] = None
        self._bot_username: Optional[str] = None
        self._token_usage_by_thread: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._token_usage_by_turn: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._outbox_task: Optional[asyncio.Task[None]] = None
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

    def _housekeeping_roots(self) -> list[Path]:
        roots: set[Path] = set()
        try:
            state = self._store.load()
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

    async def _housekeeping_loop(self) -> None:
        config = self._housekeeping_config
        if config is None or not config.enabled:
            return
        interval = max(config.interval_seconds, 1)
        while True:
            try:
                roots = self._housekeeping_roots()
                if roots:
                    await asyncio.to_thread(
                        run_housekeeping_for_roots,
                        config,
                        roots,
                        self._logger,
                    )
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
            await self._restore_pending_approvals()
            await self._outbox_manager.restore()
            await self._voice_manager.restore()
            self._prime_poller_offset()
            self._outbox_task = asyncio.create_task(self._outbox_manager.run_loop())
            self._voice_task = asyncio.create_task(self._voice_manager.run_loop())
            self._housekeeping_task = asyncio.create_task(self._housekeeping_loop())
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
                media_enabled=self._config.media.enabled,
                media_images=self._config.media.images,
                media_voice=self._config.media.voice,
                poller_offset=self._poller.offset,
            )
            while True:
                updates = []
                try:
                    updates = await self._poller.poll(
                        timeout=self._config.poll_timeout_seconds
                    )
                    if self._poller.offset is not None:
                        self._record_poll_offset(updates)
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

    def _prime_poller_offset(self) -> None:
        last_update_id = self._store.get_last_update_id_global()
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

    def _record_poll_offset(self, updates: Sequence[TelegramUpdate]) -> None:
        offset = self._poller.offset
        if offset is None:
            return
        last_update_id = offset - 1
        if last_update_id < 0:
            return
        stored = self._store.update_last_update_id_global(last_update_id)
        if updates:
            max_update_id = max(update.update_id for update in updates)
            log_event(
                self._logger,
                logging.INFO,
                "telegram.poll.offset.updated",
                incoming_update_id=max_update_id,
                stored_global_update_id=stored,
                poller_offset=offset,
            )

    def _spawn_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        task.add_done_callback(self._log_task_result)

    def _log_task_result(self, task: asyncio.Future) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(self._logger, logging.WARNING, "telegram.task.failed", exc=exc)

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
        key = self._resolve_topic_key(chat_id, thread_id)
        record = self._router.get_topic(key)
        client = await self._client_for_workspace(
            record.workspace_path if record else None
        )
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
        self, message: TelegramMessage, *, topic_key: Optional[str] = None
    ) -> None:
        await message_handlers.handle_message_inner(self, message, topic_key=topic_key)

    def _coalesce_key_for_topic(self, key: str, user_id: Optional[int]) -> str:
        return message_handlers.coalesce_key_for_topic(self, key, user_id)

    def _coalesce_key(self, message: TelegramMessage) -> str:
        return message_handlers.coalesce_key(self, message)

    async def _buffer_coalesced_message(
        self, message: TelegramMessage, text: str
    ) -> None:
        await message_handlers.buffer_coalesced_message(self, message, text)

    async def _coalesce_flush_after(self, key: str) -> None:
        await message_handlers.coalesce_flush_after(self, key)

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
        self, message: TelegramMessage, runtime: Any, caption_text: str
    ) -> None:
        await message_handlers.handle_media_message(
            self, message, runtime, caption_text
        )

    def _with_conversation_id(
        self, message: str, *, chat_id: int, thread_id: Optional[int]
    ) -> str:
        return _with_conversation_id(message, chat_id=chat_id, thread_id=thread_id)

    def _should_process_update(self, key: str, update_id: int) -> bool:
        if not isinstance(update_id, int):
            return True
        if isinstance(update_id, bool):
            return True
        duplicate = False

        def apply(record: "TelegramTopicRecord") -> None:
            nonlocal duplicate
            last_id = record.last_update_id
            if isinstance(last_id, int) and not isinstance(last_id, bool):
                if update_id <= last_id:
                    duplicate = True
                    return
            record.last_update_id = update_id

        self._store.update_topic(key, apply)
        return not duplicate

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        await callback_handlers.handle_callback(self, callback)

    def _enqueue_topic_work(
        self, key: str, work: Any, *, force_queue: bool = False
    ) -> None:
        runtime = self._router.runtime_for(key)
        if force_queue or self._config.concurrency.per_topic_queue:
            self._spawn_task(runtime.queue.enqueue(work))
        else:
            self._spawn_task(work())
