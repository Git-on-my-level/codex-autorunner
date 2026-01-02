from __future__ import annotations

import asyncio
import collections
import dataclasses
import html
import logging
import os
import re
import secrets
import shlex
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from .app_server_client import (
    ApprovalDecision,
    CodexAppServerClient,
    CodexAppServerError,
)
from .logging_utils import log_event
from .manifest import load_manifest
from .routes.system import _normalize_update_target, _spawn_update_process
from .telegram_adapter import (
    ApprovalCallback,
    BindCallback,
    CancelCallback,
    EffortCallback,
    UpdateCallback,
    ModelCallback,
    PageCallback,
    ResumeCallback,
    TelegramAllowlist,
    TelegramBotClient,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramUpdate,
    TelegramUpdatePoller,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    allowlist_allows,
    build_approval_keyboard,
    build_bind_keyboard,
    build_effort_keyboard,
    build_model_keyboard,
    build_resume_keyboard,
    build_update_keyboard,
    encode_page_callback,
    is_interrupt_alias,
    parse_callback_data,
    parse_command,
)
from .state import now_iso
from .telegram_state import (
    APPROVAL_MODE_YOLO,
    OutboxRecord,
    PendingApprovalRecord,
    TelegramStateStore,
    TopicRouter,
    normalize_approval_mode,
    topic_key,
)
from .utils import resolve_executable, subprocess_env
from .voice import VoiceConfig, VoiceService, VoiceServiceError

DEFAULT_ALLOWED_UPDATES = ("message", "edited_message", "callback_query")
DEFAULT_POLL_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 10
DEFAULT_THREAD_LIST_LIMIT = 10
DEFAULT_MODEL_LIST_LIMIT = 25
DEFAULT_MCP_LIST_LIMIT = 50
DEFAULT_SKILLS_LIST_LIMIT = 50
TOKEN_USAGE_CACHE_LIMIT = 256
TOKEN_USAGE_TURN_CACHE_LIMIT = 512
DEFAULT_INTERRUPT_TIMEOUT_SECONDS = 30.0
DEFAULT_INTERRUPT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_SAFE_APPROVAL_POLICY = "on-request"
DEFAULT_YOLO_APPROVAL_POLICY = "never"
DEFAULT_YOLO_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_PARSE_MODE = "HTML"
DEFAULT_STATE_FILE = ".codex-autorunner/telegram_state.json"
DEFAULT_APP_SERVER_COMMAND = ["codex", "app-server"]
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
OUTBOX_RETRY_INTERVAL_SECONDS = 10.0
OUTBOX_IMMEDIATE_RETRY_DELAYS = (0.5, 2.0, 5.0)
OUTBOX_MAX_ATTEMPTS = 8
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
RESUME_PICKER_PROMPT = (
    "Select a thread to resume (buttons below or reply with number/id)."
)
BIND_PICKER_PROMPT = "Select a repo to bind (buttons below or reply with number/id)."
MODEL_PICKER_PROMPT = "Select a model (buttons below)."
EFFORT_PICKER_PROMPT = "Select a reasoning effort for {model}."
UPDATE_PICKER_PROMPT = "Select update target (buttons below)."
UPDATE_TARGET_OPTIONS = (
    ("both", "Both (web + Telegram)"),
    ("web", "Web only"),
    ("telegram", "Telegram only"),
)
WORKING_PLACEHOLDER = "Working..."
COMMAND_DISABLED_TEMPLATE = "'/{name}' is disabled while a task is in progress."
MAX_MENTION_BYTES = 200_000
VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
CONTEXT_BASELINE_TOKENS = 12000
APPROVAL_POLICY_VALUES = {"untrusted", "on-failure", "on-request", "never"}
APPROVAL_PRESETS = {
    "read-only": ("on-request", "readOnly"),
    "auto": ("on-request", "workspaceWrite"),
    "full-access": ("never", "dangerFullAccess"),
}
DEFAULT_MEDIA_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_MAX_VOICE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_IMAGE_PROMPT = "Describe the image."
COALESCE_WINDOW_SECONDS = 2.0
COMPACT_SUMMARY_PROMPT = (
    "Summarize the conversation so far into a concise context block I can paste into "
    "a new thread. Include goals, constraints, decisions, and current state."
)
INIT_PROMPT = "\n".join(
    [
        "Generate a file named AGENTS.md that serves as a contributor guide for this repository.",
        "Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.",
        "Follow the outline below, but adapt as needed - add sections if relevant, and omit those that do not apply to this project.",
        "",
        "Document Requirements",
        "",
        "- Title the document \"Repository Guidelines\".",
        "- Use Markdown headings (#, ##, etc.) for structure.",
        "- Keep the document concise. 200-400 words is optimal.",
        "- Keep explanations short, direct, and specific to this repository.",
        "- Provide examples where helpful (commands, directory paths, naming patterns).",
        "- Maintain a professional, instructional tone.",
        "",
        "Recommended Sections",
        "",
        "Project Structure & Module Organization",
        "",
        "- Outline the project structure, including where the source code, tests, and assets are located.",
        "",
        "Build, Test, and Development Commands",
        "",
        "- List key commands for building, testing, and running locally (e.g., npm test, make build).",
        "- Briefly explain what each command does.",
        "",
        "Coding Style & Naming Conventions",
        "",
        "- Specify indentation rules, language-specific style preferences, and naming patterns.",
        "- Include any formatting or linting tools used.",
        "",
        "Testing Guidelines",
        "",
        "- Identify testing frameworks and coverage requirements.",
        "- State test naming conventions and how to run tests.",
        "",
        "Commit & Pull Request Guidelines",
        "",
        "- Summarize commit message conventions found in the project's Git history.",
        "- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).",
        "",
        "(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions.",
    ]
)
IMAGE_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
IMAGE_EXTS = set(IMAGE_CONTENT_TYPES.values())
PARSE_MODE_ALIASES = {
    "html": "HTML",
    "markdown": "Markdown",
    "markdownv2": "MarkdownV2",
}
_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ESCAPE_RE = re.compile(r"([_*\[\]\(\)`])")
_MARKDOWN_V2_ESCAPE_RE = re.compile(r"([_*\[\]\(\)~`>#+\-=|{}.!\\])")


class TelegramBotConfigError(Exception):
    """Raised when telegram bot config is invalid."""


@dataclass(frozen=True)
class TelegramBotDefaults:
    approval_mode: str
    approval_policy: Optional[str]
    sandbox_policy: Optional[str]
    yolo_approval_policy: str
    yolo_sandbox_policy: str

    def policies_for_mode(self, mode: str) -> tuple[Optional[str], Optional[str]]:
        normalized = normalize_approval_mode(mode, default=APPROVAL_MODE_YOLO)
        if normalized == APPROVAL_MODE_YOLO:
            return self.yolo_approval_policy, self.yolo_sandbox_policy
        return self.approval_policy, self.sandbox_policy


@dataclass(frozen=True)
class TelegramBotConcurrency:
    max_parallel_turns: int
    per_topic_queue: bool


@dataclass(frozen=True)
class TelegramBotMediaConfig:
    enabled: bool
    images: bool
    voice: bool
    max_image_bytes: int
    max_voice_bytes: int
    image_prompt: str


@dataclass(frozen=True)
class TelegramMediaCandidate:
    kind: str
    file_id: str
    file_name: Optional[str]
    mime_type: Optional[str]
    file_size: Optional[int]
    duration: Optional[int] = None


@dataclass(frozen=True)
class TelegramBotConfig:
    root: Path
    enabled: bool
    mode: str
    bot_token_env: str
    chat_id_env: str
    parse_mode: Optional[str]
    bot_token: Optional[str]
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    require_topics: bool
    defaults: TelegramBotDefaults
    concurrency: TelegramBotConcurrency
    media: TelegramBotMediaConfig
    state_file: Path
    app_server_command_env: str
    app_server_command: list[str]
    poll_timeout_seconds: int
    poll_allowed_updates: list[str]

    @classmethod
    def from_raw(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        root: Path,
        env: Optional[dict[str, str]] = None,
    ) -> "TelegramBotConfig":
        env = env or dict(os.environ)
        cfg = raw if isinstance(raw, dict) else {}
        enabled = bool(cfg.get("enabled", False))
        mode = str(cfg.get("mode", "polling"))
        bot_token_env = str(cfg.get("bot_token_env", "CAR_TELEGRAM_BOT_TOKEN"))
        chat_id_env = str(cfg.get("chat_id_env", "CAR_TELEGRAM_CHAT_ID"))
        parse_mode_raw = cfg.get("parse_mode") if "parse_mode" in cfg else DEFAULT_PARSE_MODE
        parse_mode = _normalize_parse_mode(parse_mode_raw)
        bot_token = env.get(bot_token_env)

        allowed_chat_ids = set(_parse_int_list(cfg.get("allowed_chat_ids")))
        allowed_chat_ids.update(_parse_int_list(env.get(chat_id_env)))
        allowed_user_ids = set(_parse_int_list(cfg.get("allowed_user_ids")))

        require_topics = bool(cfg.get("require_topics", True))

        defaults_raw = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}
        approval_mode = normalize_approval_mode(
            defaults_raw.get("approval_mode"), default=APPROVAL_MODE_YOLO
        )
        approval_policy = defaults_raw.get("approval_policy", DEFAULT_SAFE_APPROVAL_POLICY)
        sandbox_policy = defaults_raw.get("sandbox_policy")
        if sandbox_policy is not None:
            sandbox_policy = str(sandbox_policy)
        yolo_approval_policy = str(
            defaults_raw.get("yolo_approval_policy", DEFAULT_YOLO_APPROVAL_POLICY)
        )
        yolo_sandbox_policy = str(
            defaults_raw.get("yolo_sandbox_policy", DEFAULT_YOLO_SANDBOX_POLICY)
        )
        defaults = TelegramBotDefaults(
            approval_mode=approval_mode,
            approval_policy=str(approval_policy) if approval_policy is not None else None,
            sandbox_policy=sandbox_policy,
            yolo_approval_policy=yolo_approval_policy,
            yolo_sandbox_policy=yolo_sandbox_policy,
        )

        concurrency_raw = (
            cfg.get("concurrency") if isinstance(cfg.get("concurrency"), dict) else {}
        )
        max_parallel_turns = int(concurrency_raw.get("max_parallel_turns", 4))
        if max_parallel_turns <= 0:
            max_parallel_turns = 1
        per_topic_queue = bool(concurrency_raw.get("per_topic_queue", True))
        concurrency = TelegramBotConcurrency(
            max_parallel_turns=max_parallel_turns,
            per_topic_queue=per_topic_queue,
        )

        media_raw = cfg.get("media") if isinstance(cfg.get("media"), dict) else {}
        media_enabled = bool(media_raw.get("enabled", True))
        media_images = bool(media_raw.get("images", True))
        media_voice = bool(media_raw.get("voice", True))
        max_image_bytes = int(
            media_raw.get("max_image_bytes", DEFAULT_MEDIA_MAX_IMAGE_BYTES)
        )
        if max_image_bytes <= 0:
            max_image_bytes = DEFAULT_MEDIA_MAX_IMAGE_BYTES
        max_voice_bytes = int(
            media_raw.get("max_voice_bytes", DEFAULT_MEDIA_MAX_VOICE_BYTES)
        )
        if max_voice_bytes <= 0:
            max_voice_bytes = DEFAULT_MEDIA_MAX_VOICE_BYTES
        image_prompt = str(media_raw.get("image_prompt", DEFAULT_MEDIA_IMAGE_PROMPT)).strip()
        if not image_prompt:
            image_prompt = DEFAULT_MEDIA_IMAGE_PROMPT
        media = TelegramBotMediaConfig(
            enabled=media_enabled,
            images=media_images,
            voice=media_voice,
            max_image_bytes=max_image_bytes,
            max_voice_bytes=max_voice_bytes,
            image_prompt=image_prompt,
        )

        state_file = Path(cfg.get("state_file", DEFAULT_STATE_FILE))
        if not state_file.is_absolute():
            state_file = (root / state_file).resolve()

        app_server_command_env = str(
            cfg.get("app_server_command_env", "CAR_TELEGRAM_APP_SERVER_COMMAND")
        )
        app_server_command: list[str] = []
        if app_server_command_env:
            env_command = env.get(app_server_command_env)
            if env_command:
                app_server_command = _parse_command(env_command)
        if not app_server_command:
            app_server_command = _parse_command(cfg.get("app_server_command"))
        if not app_server_command:
            app_server_command = list(DEFAULT_APP_SERVER_COMMAND)

        polling_raw = (
            cfg.get("polling") if isinstance(cfg.get("polling"), dict) else {}
        )
        poll_timeout_seconds = int(
            polling_raw.get("timeout_seconds", DEFAULT_POLL_TIMEOUT_SECONDS)
        )
        allowed_updates = polling_raw.get("allowed_updates")
        if isinstance(allowed_updates, list):
            poll_allowed_updates = [str(item) for item in allowed_updates if item]
        else:
            poll_allowed_updates = list(DEFAULT_ALLOWED_UPDATES)

        return cls(
            root=root,
            enabled=enabled,
            mode=mode,
            bot_token_env=bot_token_env,
            chat_id_env=chat_id_env,
            parse_mode=parse_mode,
            bot_token=bot_token,
            allowed_chat_ids=allowed_chat_ids,
            allowed_user_ids=allowed_user_ids,
            require_topics=require_topics,
            defaults=defaults,
            concurrency=concurrency,
            media=media,
            state_file=state_file,
            app_server_command_env=app_server_command_env,
            app_server_command=app_server_command,
            poll_timeout_seconds=poll_timeout_seconds,
            poll_allowed_updates=poll_allowed_updates,
        )

    def validate(self) -> None:
        issues: list[str] = []
        if not self.bot_token:
            issues.append(f"missing bot token env '{self.bot_token_env}'")
        if not self.allowed_chat_ids:
            issues.append(
                "no allowed chat ids configured (set allowed_chat_ids or chat_id_env)"
            )
        if not self.allowed_user_ids:
            issues.append("no allowed user ids configured (set allowed_user_ids)")
        if not self.app_server_command:
            issues.append("app_server_command must be set")
        if self.poll_timeout_seconds <= 0:
            issues.append("poll_timeout_seconds must be greater than 0")
        if issues:
            raise TelegramBotConfigError("; ".join(issues))

    def allowlist(self) -> TelegramAllowlist:
        return TelegramAllowlist(
            allowed_chat_ids=self.allowed_chat_ids,
            allowed_user_ids=self.allowed_user_ids,
            require_topic=self.require_topics,
        )


@dataclass
class PendingApproval:
    request_id: str
    turn_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: Optional[int]
    created_at: str
    future: asyncio.Future[ApprovalDecision]


@dataclass
class TurnContext:
    topic_key: str
    chat_id: int
    thread_id: Optional[int]
    reply_to_message_id: Optional[int]
    placeholder_message_id: Optional[int] = None


@dataclass
class SelectionState:
    items: list[tuple[str, str]]
    page: int = 0


@dataclass
class _CoalescedBuffer:
    message: TelegramMessage
    parts: list[str]
    task: Optional[asyncio.Task[None]] = None


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: Optional[str] = None


@dataclass
class ModelPickerState:
    items: list[tuple[str, str]]
    options: dict[str, ModelOption]
    page: int = 0


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[TelegramMessage, str, Any], Awaitable[None]]
    allow_during_turn: bool = False


class TelegramBotService:
    def __init__(
        self,
        config: TelegramBotConfig,
        *,
        logger: Optional[logging.Logger] = None,
        hub_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        voice_config: Optional[VoiceConfig] = None,
        voice_service: Optional[VoiceService] = None,
        update_repo_url: Optional[str] = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._update_repo_url = update_repo_url
        self._allowlist = config.allowlist()
        self._store = TelegramStateStore(
            config.state_file, default_approval_mode=config.defaults.approval_mode
        )
        self._router = TopicRouter(self._store)
        app_server_cwd = hub_root or config.root
        app_server_env = _app_server_env(config.app_server_command, app_server_cwd)
        self._client = CodexAppServerClient(
            config.app_server_command,
            cwd=app_server_cwd,
            env=app_server_env,
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
        self._turn_semaphore = asyncio.Semaphore(config.concurrency.max_parallel_turns)
        self._turn_contexts: dict[str, TurnContext] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._update_options: dict[str, SelectionState] = {}
        self._coalesced_buffers: dict[str, _CoalescedBuffer] = {}
        self._coalesce_locks: dict[str, asyncio.Lock] = {}
        self._bot_username: Optional[str] = None
        self._token_usage_by_thread: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._token_usage_by_turn: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._outbox_inflight: set[str] = set()
        self._outbox_lock: Optional[asyncio.Lock] = None
        self._outbox_task: Optional[asyncio.Task[None]] = None
        self._command_specs = self._build_command_specs()

    async def run_polling(self) -> None:
        if self._config.mode != "polling":
            raise TelegramBotConfigError(
                f"Unsupported telegram_bot.mode '{self._config.mode}'"
            )
        self._config.validate()
        # Bind the semaphore to the running loop to avoid cross-loop await failures.
        self._turn_semaphore = asyncio.Semaphore(
            self._config.concurrency.max_parallel_turns
        )
        self._outbox_inflight = set()
        self._outbox_lock = asyncio.Lock()
        await self._start_app_server_with_backoff()
        await self._prime_bot_identity()
        await self._restore_pending_approvals()
        await self._restore_outbox()
        self._outbox_task = asyncio.create_task(self._outbox_loop())
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
        )
        try:
            while True:
                updates = []
                try:
                    updates = await self._poller.poll(
                        timeout=self._config.poll_timeout_seconds
                    )
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
                    self._spawn_task(self._dispatch_update(update))
        finally:
            if self._outbox_task is not None:
                self._outbox_task.cancel()
                try:
                    await self._outbox_task
                except asyncio.CancelledError:
                    pass
            await self._bot.close()
            await self._client.close()

    async def _prime_bot_identity(self) -> None:
        try:
            payload = await self._bot.get_me()
        except Exception:
            return
        if isinstance(payload, dict):
            username = payload.get("username")
            if isinstance(username, str) and username:
                self._bot_username = username

    async def _restore_pending_approvals(self) -> None:
        state = self._store.load()
        if not state.pending_approvals:
            return
        grouped: dict[tuple[int, Optional[int]], list[PendingApprovalRecord]] = {}
        for record in state.pending_approvals.values():
            key = (record.chat_id, record.thread_id)
            grouped.setdefault(key, []).append(record)
        for (chat_id, thread_id), records in grouped.items():
            items = []
            for record in records:
                age = _approval_age_seconds(record.created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                items.append(f"{record.request_id} ({age_label})")
                self._store.clear_pending_approval(record.request_id)
            message = (
                "Cleared stale approval requests from a previous session. "
                "Re-run the request or use /interrupt if the turn is still active.\n"
                f"Requests: {', '.join(items)}"
            )
            try:
                await self._send_message(
                    chat_id,
                    message,
                    thread_id=thread_id,
                )
            except Exception:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.approval.restore_failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                )

    async def _restore_outbox(self) -> None:
        records = self._store.list_outbox()
        if not records:
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.restore",
            count=len(records),
        )
        await self._flush_outbox(records)

    async def _outbox_loop(self) -> None:
        while True:
            await asyncio.sleep(OUTBOX_RETRY_INTERVAL_SECONDS)
            try:
                records = self._store.list_outbox()
                if records:
                    await self._flush_outbox(records)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.flush_failed",
                    exc=exc,
                )

    async def _flush_outbox(self, records: list[OutboxRecord]) -> None:
        for record in records:
            if record.attempts >= OUTBOX_MAX_ATTEMPTS:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.gave_up",
                    record_id=record.record_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    attempts=record.attempts,
                )
                self._store.delete_outbox(record.record_id)
                if record.placeholder_message_id is not None:
                    await self._edit_message_text(
                        record.chat_id,
                        record.placeholder_message_id,
                        "Delivery failed after retries. Please resend.",
                    )
                continue
            await self._attempt_outbox_send(record)

    async def _attempt_outbox_send(self, record: OutboxRecord) -> bool:
        current = self._store.get_outbox(record.record_id)
        if current is None:
            return False
        record = current
        if not await self._mark_outbox_inflight(record.record_id):
            return False
        try:
            await self._send_message(
                record.chat_id,
                record.text,
                thread_id=record.thread_id,
                reply_to=record.reply_to_message_id,
            )
        except Exception as exc:
            record.attempts += 1
            record.last_error = str(exc)[:500]
            record.last_attempt_at = now_iso()
            self._store.update_outbox(record)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.outbox.send_failed",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                attempts=record.attempts,
                exc=exc,
            )
            return False
        finally:
            await self._clear_outbox_inflight(record.record_id)
        self._store.delete_outbox(record.record_id)
        if record.placeholder_message_id is not None:
            await self._delete_message(
                record.chat_id, record.placeholder_message_id
            )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.delivered",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
        )
        return True

    async def _mark_outbox_inflight(self, record_id: str) -> bool:
        if self._outbox_lock is None:
            self._outbox_lock = asyncio.Lock()
        async with self._outbox_lock:
            if record_id in self._outbox_inflight:
                return False
            self._outbox_inflight.add(record_id)
            return True

    async def _clear_outbox_inflight(self, record_id: str) -> None:
        if self._outbox_lock is None:
            return
        async with self._outbox_lock:
            self._outbox_inflight.discard(record_id)

    async def _send_message_with_outbox(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int] = None,
    ) -> bool:
        record = OutboxRecord(
            record_id=secrets.token_hex(8),
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to,
            placeholder_message_id=placeholder_id,
            text=text,
            created_at=now_iso(),
        )
        self._store.enqueue_outbox(record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.enqueued",
            record_id=record.record_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        for delay in OUTBOX_IMMEDIATE_RETRY_DELAYS:
            if await self._attempt_outbox_send(record):
                return True
            if record.attempts >= OUTBOX_MAX_ATTEMPTS:
                return False
            await asyncio.sleep(delay)
        return False

    async def _start_app_server_with_backoff(self) -> None:
        delay = APP_SERVER_START_BACKOFF_INITIAL_SECONDS
        while True:
            try:
                await self._client.start()
                return
            except CodexAppServerError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.start_failed",
                    delay_seconds=round(delay, 2),
                    exc=exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, APP_SERVER_START_BACKOFF_MAX_SECONDS)

    def _spawn_task(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(self._log_task_result)

    def _log_task_result(self, task: asyncio.Future) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(self._logger, logging.WARNING, "telegram.task.failed", exc=exc)

    def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return self._router.resolve_key(chat_id, thread_id)

    def _topic_scope_id(
        self, repo_id: Optional[str], workspace_path: Optional[str]
    ) -> Optional[str]:
        normalized_repo = repo_id.strip() if isinstance(repo_id, str) else ""
        normalized_path = workspace_path.strip() if isinstance(workspace_path, str) else ""
        if normalized_repo and normalized_path:
            return f"{normalized_repo}@{normalized_path}"
        if normalized_repo:
            return normalized_repo
        if normalized_path:
            return normalized_path
        return None

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
        await self._edit_message_text(chat_id, message_id, "Interrupt timed out.")
        runtime.interrupt_requested = False
        runtime.interrupt_message_id = None
        runtime.interrupt_turn_id = None

    async def _dispatch_interrupt_request(
        self,
        *,
        turn_id: str,
        runtime: Any,
        chat_id: int,
        thread_id: Optional[int],
    ) -> None:
        try:
            await asyncio.wait_for(
                self._client.turn_interrupt(turn_id),
                timeout=DEFAULT_INTERRUPT_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.interrupt.request_timeout",
                chat_id=chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == turn_id
            ):
                await self._edit_message_text(
                    chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt failed.",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
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
                    "Interrupt failed.",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False

    async def _dispatch_update(self, update: TelegramUpdate) -> None:
        chat_id = None
        user_id = None
        thread_id = None
        message_id = None
        is_topic = None
        is_edited = None
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user_id
            thread_id = update.message.thread_id
            message_id = update.message.message_id
            is_topic = update.message.is_topic_message
            is_edited = update.message.is_edited
        elif update.callback:
            chat_id = update.callback.chat_id
            user_id = update.callback.from_user_id
            thread_id = update.callback.thread_id
            message_id = update.callback.message_id
        log_event(
            self._logger,
            logging.INFO,
            "telegram.update.received",
            update_id=update.update_id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
            message_id=message_id,
            is_topic=is_topic,
            is_edited=is_edited,
            has_message=bool(update.message),
            has_callback=bool(update.callback),
        )
        if not allowlist_allows(update, self._allowlist):
            self._log_denied(update)
            return
        if update.callback:
            await self._handle_callback(update.callback)
            return
        if update.message:
            await self._handle_message(update.message)

    def _log_denied(self, update: TelegramUpdate) -> None:
        chat_id = None
        user_id = None
        thread_id = None
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user_id
            thread_id = update.message.thread_id
        elif update.callback:
            chat_id = update.callback.chat_id
            user_id = update.callback.from_user_id
            thread_id = update.callback.thread_id
        log_event(
            self._logger,
            logging.INFO,
            "telegram.allowlist.denied",
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
        )

    async def _handle_message(self, message: TelegramMessage) -> None:
        if message.is_edited:
            await self._handle_edited_message(message)
            return
        raw_text = message.text or ""
        raw_caption = message.caption or ""
        text_candidate = raw_text if raw_text.strip() else raw_caption
        entities = message.entities if raw_text.strip() else message.caption_entities
        trimmed_text = text_candidate.strip()
        has_media = self._message_has_media(message)
        if not trimmed_text and not has_media:
            return
        bypass = has_media
        if trimmed_text:
            if is_interrupt_alias(trimmed_text):
                bypass = True
            elif parse_command(
                text_candidate, entities=entities, bot_username=self._bot_username
            ):
                bypass = True
        if bypass:
            await self._flush_coalesced_message(message)
            await self._handle_message_inner(message)
            return
        await self._buffer_coalesced_message(message, text_candidate)

    async def _handle_edited_message(self, message: TelegramMessage) -> None:
        text = (message.text or "").strip()
        if not text:
            text = (message.caption or "").strip()
        if not text:
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        runtime = self._router.runtime_for(key)
        turn_id = runtime.current_turn_id
        if not turn_id:
            return
        ctx = self._turn_contexts.get(turn_id)
        if ctx is None or ctx.reply_to_message_id != message.message_id:
            return
        await self._handle_interrupt(message, runtime)
        edited_text = f"Edited: {text}"
        self._enqueue_topic_work(
            key,
            lambda: self._handle_normal_message(
                message,
                runtime,
                text_override=edited_text,
            ),
        )

    async def _handle_message_inner(self, message: TelegramMessage) -> None:
        raw_text = message.text or ""
        raw_caption = message.caption or ""
        text = raw_text.strip()
        entities = message.entities
        if not text:
            text = raw_caption.strip()
            entities = message.caption_entities
        has_media = self._message_has_media(message)
        if not text and not has_media:
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        runtime = self._router.runtime_for(key)

        if text and self._handle_pending_resume(key, text):
            return
        if text and self._handle_pending_bind(key, text):
            return

        if text and is_interrupt_alias(text):
            await self._handle_interrupt(message, runtime)
            return

        command_text = raw_text if raw_text.strip() else raw_caption
        command = (
            parse_command(
                command_text, entities=entities, bot_username=self._bot_username
            )
            if command_text
            else None
        )
        if command:
            if command.name != "resume":
                self._resume_options.pop(key, None)
            if command.name != "bind":
                self._bind_options.pop(key, None)
            if command.name != "model":
                self._model_options.pop(key, None)
                self._model_pending.pop(key, None)
        else:
            self._resume_options.pop(key, None)
            self._bind_options.pop(key, None)
            self._model_options.pop(key, None)
            self._model_pending.pop(key, None)
        if command:
            spec = self._command_specs.get(command.name)
            if spec and spec.allow_during_turn:
                self._spawn_task(self._handle_command(command, message, runtime))
            else:
                self._enqueue_topic_work(
                    key,
                    lambda: self._handle_command(command, message, runtime),
                )
            return

        if has_media:
            self._enqueue_topic_work(
                key,
                lambda: self._handle_media_message(message, runtime, text),
            )
            return

        self._enqueue_topic_work(
            key,
            lambda: self._handle_normal_message(message, runtime, text_override=text),
        )

    def _coalesce_key(self, message: TelegramMessage) -> str:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        user_id = message.from_user_id
        if user_id is None:
            return f"{key}:user:unknown"
        return f"{key}:user:{user_id}"

    async def _buffer_coalesced_message(self, message: TelegramMessage, text: str) -> None:
        key = self._coalesce_key(message)
        lock = self._coalesce_locks.setdefault(key, asyncio.Lock())
        async with lock:
            buffer = self._coalesced_buffers.get(key)
            if buffer is None:
                buffer = _CoalescedBuffer(message=message, parts=[text])
                self._coalesced_buffers[key] = buffer
            else:
                buffer.parts.append(text)
            task = buffer.task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            buffer.task = asyncio.create_task(self._coalesce_flush_after(key))

    async def _coalesce_flush_after(self, key: str) -> None:
        try:
            await asyncio.sleep(COALESCE_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await self._flush_coalesced_key(key)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.coalesce.flush_failed",
                key=key,
                exc=exc,
            )

    async def _flush_coalesced_message(self, message: TelegramMessage) -> None:
        await self._flush_coalesced_key(self._coalesce_key(message))

    async def _flush_coalesced_key(self, key: str) -> None:
        lock = self._coalesce_locks.get(key)
        if lock is None:
            return
        buffer = None
        async with lock:
            buffer = self._coalesced_buffers.pop(key, None)
            if buffer is None:
                return
            task = buffer.task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        combined_message = self._build_coalesced_message(buffer)
        await self._handle_message_inner(combined_message)

    def _build_coalesced_message(self, buffer: _CoalescedBuffer) -> TelegramMessage:
        combined_text = "\n".join(buffer.parts)
        return dataclasses.replace(buffer.message, text=combined_text, caption=None)

    def _message_has_media(self, message: TelegramMessage) -> bool:
        return bool(message.photos or message.document or message.voice or message.audio)

    def _select_photo(self, photos: Sequence[TelegramPhotoSize]) -> Optional[TelegramPhotoSize]:
        if not photos:
            return None
        return max(
            photos,
            key=lambda item: ((item.file_size or 0), item.width * item.height),
        )

    def _document_is_image(self, document: TelegramDocument) -> bool:
        if document.mime_type:
            base = document.mime_type.lower().split(";", 1)[0].strip()
            if base.startswith("image/"):
                return True
        if document.file_name:
            suffix = Path(document.file_name).suffix.lower()
            if suffix in IMAGE_EXTS:
                return True
        return False

    def _select_image_candidate(
        self, message: TelegramMessage
    ) -> Optional[TelegramMediaCandidate]:
        photo = self._select_photo(message.photos)
        if photo:
            return TelegramMediaCandidate(
                kind="photo",
                file_id=photo.file_id,
                file_name=None,
                mime_type=None,
                file_size=photo.file_size,
            )
        if message.document and self._document_is_image(message.document):
            document = message.document
            return TelegramMediaCandidate(
                kind="document",
                file_id=document.file_id,
                file_name=document.file_name,
                mime_type=document.mime_type,
                file_size=document.file_size,
            )
        return None

    def _select_voice_candidate(
        self, message: TelegramMessage
    ) -> Optional[TelegramMediaCandidate]:
        if message.voice:
            voice = message.voice
            return TelegramMediaCandidate(
                kind="voice",
                file_id=voice.file_id,
                file_name=None,
                mime_type=voice.mime_type,
                file_size=voice.file_size,
                duration=voice.duration,
            )
        if message.audio:
            audio = message.audio
            return TelegramMediaCandidate(
                kind="audio",
                file_id=audio.file_id,
                file_name=audio.file_name,
                mime_type=audio.mime_type,
                file_size=audio.file_size,
                duration=audio.duration,
            )
        return None

    async def _handle_media_message(
        self, message: TelegramMessage, runtime: Any, caption_text: str
    ) -> None:
        if not self._config.media.enabled:
            await self._send_message(
                message.chat_id,
                "Media handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        image_candidate = self._select_image_candidate(message)
        if image_candidate:
            if not self._config.media.images:
                await self._send_message(
                    message.chat_id,
                    "Image handling is disabled.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._handle_image_message(
                message, runtime, record, image_candidate, caption_text
            )
            return

        voice_candidate = self._select_voice_candidate(message)
        if voice_candidate:
            if not self._config.media.voice:
                await self._send_message(
                    message.chat_id,
                    "Voice transcription is disabled.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._handle_voice_message(
                message, runtime, record, voice_candidate, caption_text
            )
            return

        if caption_text:
            await self._handle_normal_message(
                message,
                runtime,
                text_override=caption_text,
                record=record,
            )
            return
        await self._send_message(
            message.chat_id,
            "Unsupported media type.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _handle_pending_resume(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._resume_options.get(key)
        if not state:
            return False
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        thread_id = page_items[choice - 1][0]
        self._resume_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._resume_thread_by_id(key, thread_id),
        )
        return True

    def _handle_pending_bind(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._bind_options.get(key)
        if not state:
            return False
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        repo_id = page_items[choice - 1][0]
        self._bind_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._bind_topic_by_repo_id(key, repo_id),
        )
        return True

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        parsed = parse_callback_data(callback.data)
        if parsed is None:
            return
        key = None
        if callback.chat_id is not None:
            key = self._resolve_topic_key(callback.chat_id, callback.thread_id)
        if isinstance(parsed, ApprovalCallback):
            await self._handle_approval_callback(callback, parsed)
        elif isinstance(parsed, ResumeCallback):
            if key:
                state = self._resume_options.get(key)
                if not state or not _selection_contains(state.items, parsed.thread_id):
                    await self._answer_callback(callback, "Selection expired")
                    return
                await self._resume_thread_by_id(key, parsed.thread_id, callback)
        elif isinstance(parsed, BindCallback):
            if key:
                state = self._bind_options.get(key)
                if not state or not _selection_contains(state.items, parsed.repo_id):
                    await self._answer_callback(callback, "Selection expired")
                    return
                await self._bind_topic_by_repo_id(key, parsed.repo_id, callback)
        elif isinstance(parsed, ModelCallback):
            if key:
                await self._handle_model_callback(key, callback, parsed)
        elif isinstance(parsed, EffortCallback):
            if key:
                await self._handle_effort_callback(key, callback, parsed)
        elif isinstance(parsed, UpdateCallback):
            if key:
                await self._handle_update_callback(key, callback, parsed)
        elif isinstance(parsed, CancelCallback):
            if key:
                await self._handle_selection_cancel(key, parsed, callback)
        elif isinstance(parsed, PageCallback):
            if key:
                await self._handle_selection_page(key, parsed, callback)

    async def _handle_model_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: ModelCallback,
    ) -> None:
        state = self._model_options.get(key)
        if not state:
            await self._answer_callback(callback, "Selection expired")
            return
        option = state.options.get(parsed.model_id)
        if not option:
            await self._answer_callback(callback, "Selection expired")
            return
        self._model_options.pop(key, None)
        self._model_pending[key] = option
        if option.default_effort:
            prompt = (
                f"Select a reasoning effort for {option.model_id} "
                f"(default {option.default_effort})."
            )
        else:
            prompt = EFFORT_PICKER_PROMPT.format(model=option.model_id)
        keyboard = self._build_effort_keyboard(option)
        await self._update_selection_message(key, callback, prompt, keyboard)
        await self._answer_callback(callback, "Select effort")

    async def _handle_effort_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: EffortCallback,
    ) -> None:
        option = self._model_pending.get(key)
        if not option:
            await self._answer_callback(callback, "Selection expired")
            return
        if parsed.effort not in option.efforts:
            await self._answer_callback(callback, "Selection expired")
            return
        self._model_pending.pop(key, None)
        chat_id, thread_id = _split_topic_key(key)
        self._router.update_topic(
            chat_id,
            thread_id,
            lambda record: _set_model_overrides(
                record,
                option.model_id,
                effort=parsed.effort,
            ),
        )
        await self._answer_callback(callback, "Model set")
        await self._finalize_selection(
            key,
            callback,
            f"Model set to {option.model_id} (effort={parsed.effort}). Will apply on the next turn.",
        )

    async def _handle_update_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: UpdateCallback,
    ) -> None:
        state = self._update_options.get(key)
        if not state or not _selection_contains(state.items, parsed.target):
            await self._answer_callback(callback, "Selection expired")
            return
        self._update_options.pop(key, None)
        try:
            update_target = _normalize_update_target(parsed.target)
        except ValueError:
            await self._answer_callback(callback, "Selection expired")
            await self._finalize_selection(key, callback, "Update target invalid.")
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._start_update(
            chat_id=chat_id,
            thread_id=thread_id,
            update_target=update_target,
            callback=callback,
            selection_key=key,
        )

    def _enqueue_topic_work(self, key: str, work: Any) -> None:
        runtime = self._router.runtime_for(key)
        if self._config.concurrency.per_topic_queue:
            self._spawn_task(runtime.queue.enqueue(work))
        else:
            self._spawn_task(work())

    async def _handle_command(
        self,
        command: TelegramCommand,
        message: TelegramMessage,
        runtime: Any,
    ) -> None:
        name = command.name
        args = command.args
        log_event(
            self._logger,
            logging.INFO,
            "telegram.command",
            name=name,
            args_len=len(args),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        spec = self._command_specs.get(name)
        if spec is None:
            self._resume_options.pop(key, None)
            self._bind_options.pop(key, None)
            self._model_options.pop(key, None)
            self._model_pending.pop(key, None)
            if name in ("list", "ls"):
                await self._send_message(
                    message.chat_id,
                    "Use /resume to list and switch threads.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Unsupported command: /{name}. Send /help for options.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if runtime.current_turn_id and not spec.allow_during_turn:
            await self._send_message(
                message.chat_id,
                COMMAND_DISABLED_TEMPLATE.format(name=name),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await spec.handler(message, args, runtime)

    def _build_command_specs(self) -> dict[str, CommandSpec]:
        return {
            "bind": CommandSpec(
                "bind",
                "bind this topic to a workspace",
                lambda message, args, _runtime: self._handle_bind(message, args),
            ),
            "new": CommandSpec(
                "new",
                "start a new session",
                lambda message, _args, _runtime: self._handle_new(message),
            ),
            "resume": CommandSpec(
                "resume",
                "list or resume a previous session",
                lambda message, args, _runtime: self._handle_resume(message, args),
            ),
            "review": CommandSpec(
                "review",
                "run a code review",
                self._handle_review,
            ),
            "model": CommandSpec(
                "model",
                "list or set the model",
                self._handle_model,
            ),
            "approvals": CommandSpec(
                "approvals",
                "set approval and sandbox policy",
                self._handle_approvals,
            ),
            "status": CommandSpec(
                "status",
                "show current binding and thread status",
                self._handle_status,
                allow_during_turn=True,
            ),
            "diff": CommandSpec(
                "diff",
                "show git diff for the bound workspace",
                self._handle_diff,
                allow_during_turn=True,
            ),
            "mention": CommandSpec(
                "mention",
                "include a file in a new request",
                self._handle_mention,
                allow_during_turn=True,
            ),
            "skills": CommandSpec(
                "skills",
                "list available skills",
                self._handle_skills,
                allow_during_turn=True,
            ),
            "mcp": CommandSpec(
                "mcp",
                "list MCP server status",
                self._handle_mcp,
                allow_during_turn=True,
            ),
            "experimental": CommandSpec(
                "experimental",
                "toggle experimental features",
                self._handle_experimental,
            ),
            "init": CommandSpec(
                "init",
                "generate AGENTS.md guidance",
                self._handle_init,
            ),
            "compact": CommandSpec(
                "compact",
                "compact the conversation (summary)",
                self._handle_compact,
            ),
            "rollout": CommandSpec(
                "rollout",
                "show current thread rollout path",
                self._handle_rollout,
                allow_during_turn=True,
            ),
            "update": CommandSpec(
                "update",
                "update CAR (prompt or both|web|telegram)",
                self._handle_update,
            ),
            "logout": CommandSpec(
                "logout",
                "log out of the Codex account",
                self._handle_logout,
            ),
            "feedback": CommandSpec(
                "feedback",
                "send feedback and logs",
                self._handle_feedback,
                allow_during_turn=True,
            ),
            "interrupt": CommandSpec(
                "interrupt",
                "stop the active turn",
                lambda message, _args, runtime: self._handle_interrupt(message, runtime),
                allow_during_turn=True,
            ),
            "quit": CommandSpec(
                "quit",
                "end the session (Telegram-local)",
                self._handle_quit,
                allow_during_turn=True,
            ),
            "exit": CommandSpec(
                "exit",
                "end the session (Telegram-local)",
                self._handle_quit,
                allow_during_turn=True,
            ),
            "help": CommandSpec(
                "help",
                "show this help message",
                self._handle_help,
                allow_during_turn=True,
            ),
        }

    def _parse_command_args(self, args: str) -> list[str]:
        if not args:
            return []
        try:
            return [part for part in shlex.split(args) if part]
        except ValueError:
            return [part for part in args.split() if part]

    def _effective_policies(
        self, record: "TelegramTopicRecord"
    ) -> tuple[Optional[str], Optional[Any]]:
        approval_policy, sandbox_policy = self._config.defaults.policies_for_mode(
            record.approval_mode
        )
        if record.approval_policy is not None:
            approval_policy = record.approval_policy
        if record.sandbox_policy is not None:
            sandbox_policy = record.sandbox_policy
        return approval_policy, sandbox_policy

    async def _verify_active_thread(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional["TelegramTopicRecord"]:
        thread_id = record.active_thread_id
        if not thread_id:
            return record
        try:
            result = await self._client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.verify_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to verify the active thread; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if not isinstance(resumed_path, str):
            await self._send_message(
                message.chat_id,
                "Active thread missing workspace metadata; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            workspace_root = Path(record.workspace_path or "").expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Active thread has invalid workspace metadata; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if not _path_within(workspace_root, resumed_root):
            await self._send_message(
                message.chat_id,
                "Active thread belongs to a different workspace; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return self._apply_thread_result(
            message.chat_id, message.thread_id, result, active_thread_id=thread_id
        )

    def _apply_thread_result(
        self,
        chat_id: int,
        thread_id: Optional[int],
        result: Any,
        *,
        active_thread_id: Optional[str] = None,
        overwrite_defaults: bool = False,
    ) -> "TelegramTopicRecord":
        info = _extract_thread_info(result)
        if active_thread_id is None:
            active_thread_id = info.get("thread_id")

        def apply(record: "TelegramTopicRecord") -> None:
            if active_thread_id:
                record.active_thread_id = active_thread_id
            if info.get("workspace_path"):
                record.workspace_path = info["workspace_path"]
            if info.get("rollout_path"):
                record.rollout_path = info["rollout_path"]
            if info.get("model") and (overwrite_defaults or record.model is None):
                record.model = info["model"]
            if info.get("effort") and (overwrite_defaults or record.effort is None):
                record.effort = info["effort"]
            if info.get("summary") and (overwrite_defaults or record.summary is None):
                record.summary = info["summary"]
            allow_thread_policies = record.approval_mode != APPROVAL_MODE_YOLO
            if allow_thread_policies and info.get("approval_policy") and (
                overwrite_defaults or record.approval_policy is None
            ):
                record.approval_policy = info["approval_policy"]
            if allow_thread_policies and info.get("sandbox_policy") and (
                overwrite_defaults or record.sandbox_policy is None
            ):
                record.sandbox_policy = info["sandbox_policy"]

        return self._router.update_topic(chat_id, thread_id, apply)

    async def _require_bound_record(
        self, message: TelegramMessage, *, prompt: Optional[str] = None
    ) -> Optional["TelegramTopicRecord"]:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                prompt
                or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return record

    async def _ensure_thread_id(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[str]:
        thread_id = record.active_thread_id
        if thread_id:
            verified = await self._verify_active_thread(message, record)
            if not verified or not verified.active_thread_id:
                return None
            return verified.active_thread_id
        thread = await self._client.thread_start(record.workspace_path or "")
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new Codex thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        self._apply_thread_result(
            message.chat_id,
            message.thread_id,
            thread,
            active_thread_id=thread_id,
        )
        return thread_id

    async def _handle_help(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        await self._send_message(
            message.chat_id,
            _format_help_text(self._command_specs),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_normal_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        text_override: Optional[str] = None,
        input_items: Optional[list[dict[str, Any]]] = None,
        record: Optional[Any] = None,
        send_placeholder: bool = True,
        transcript_message_id: Optional[int] = None,
        transcript_text: Optional[str] = None,
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = record or self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record.active_thread_id:
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return
            record = verified
        thread_id = record.active_thread_id
        turn_handle = None
        placeholder_id: Optional[int] = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
        prompt_text = text_override if text_override is not None else (message.text or "")
        try:
            if not thread_id:
                thread = await self._client.thread_start(record.workspace_path)
                thread_id = _extract_thread_id(thread)
                if not thread_id:
                    await self._send_message(
                        message.chat_id,
                        "Failed to start a new Codex thread.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                record = self._apply_thread_result(
                    message.chat_id,
                    message.thread_id,
                    thread,
                    active_thread_id=thread_id,
                )
            else:
                record = self._router.set_active_thread(
                    message.chat_id, message.thread_id, thread_id
                )
            approval_policy, sandbox_policy = self._effective_policies(record)
            turn_kwargs: dict[str, Any] = {}
            if record.model:
                turn_kwargs["model"] = record.model
            if record.effort:
                turn_kwargs["effort"] = record.effort
            if record.summary:
                turn_kwargs["summary"] = record.summary
            log_event(
                self._logger,
                logging.INFO,
                "telegram.turn.starting",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                approval_mode=record.approval_mode,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

            async with self._turn_semaphore:
                if send_placeholder:
                    placeholder_id = await self._send_placeholder(
                        message.chat_id,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                turn_handle = await self._client.turn_start(
                    thread_id,
                    prompt_text,
                    input_items=input_items,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                    **turn_kwargs,
                )
                turn_started_at = time.monotonic()
                runtime.current_turn_id = turn_handle.turn_id
                ctx = TurnContext(
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                self._turn_contexts[turn_handle.turn_id] = ctx
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.failed",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    "Codex turn failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
                await self._finalize_voice_transcript(
                    message.chat_id,
                    transcript_message_id,
                    transcript_text,
                )
            return
        finally:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False

        response = _compose_agent_response(result.agent_messages)
        if result.status == "interrupted" or runtime.interrupt_requested:
            response = _compose_interrupt_response(response)
            runtime.interrupt_requested = False
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == (turn_handle.turn_id if turn_handle else None)
            ):
                await self._edit_message_text(
                    message.chat_id,
                    runtime.interrupt_message_id,
                    "Interrupted.",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
        elif (
            runtime.interrupt_message_id is not None
            and runtime.interrupt_turn_id == (turn_handle.turn_id if turn_handle else None)
        ):
            await self._edit_message_text(
                message.chat_id,
                runtime.interrupt_message_id,
                "Interrupt failed.",
            )
            runtime.interrupt_message_id = None
            runtime.interrupt_turn_id = None
        log_event(
            self._logger,
            logging.INFO,
            "telegram.turn.completed",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_handle.turn_id if turn_handle else None,
            status=result.status,
            agent_message_count=len(result.agent_messages),
        )
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response,
        )
        turn_id = turn_handle.turn_id if turn_handle else None
        token_usage = (
            self._token_usage_by_turn.get(turn_id)
            if turn_id
            else None
        )
        if token_usage is None and thread_id:
            token_usage = self._token_usage_by_thread.get(thread_id)
        await self._send_turn_metrics(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            elapsed_seconds=turn_elapsed_seconds,
            token_usage=token_usage,
        )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent:
            await self._delete_message(message.chat_id, placeholder_id)
            await self._finalize_voice_transcript(
                message.chat_id,
                transcript_message_id,
                transcript_text,
            )

    async def _handle_image_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            has_caption=bool(caption_text),
        )
        max_bytes = self._config.media.max_image_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to download image.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            image_path = self._save_image_file(
                record.workspace_path, data, file_path, candidate
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to save image.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        prompt_text = caption_text.strip()
        if not prompt_text:
            prompt_text = self._config.media.image_prompt
        input_items = [
            {"type": "text", "text": prompt_text},
            {"type": "localImage", "path": str(image_path)},
        ]
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.ready",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            path=str(image_path),
            prompt_len=len(prompt_text),
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt_text,
            input_items=input_items,
            record=record,
        )

    async def _handle_voice_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            duration=candidate.duration,
            has_caption=bool(caption_text),
        )
        if not self._voice_service or not self._voice_config or not self._voice_config.enabled:
            await self._send_message(
                message.chat_id,
                "Voice transcription is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        max_bytes = self._config.media.max_voice_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.voice.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to download voice note.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        filename = candidate.file_name
        if not filename and file_path:
            filename = Path(file_path).name
        try:
            result = await asyncio.to_thread(
                self._voice_service.transcribe,
                data,
                client="telegram",
                filename=filename,
                content_type=candidate.mime_type,
            )
        except VoiceServiceError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.voice.transcribe_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                reason=exc.reason,
            )
            await self._send_message(
                message.chat_id,
                exc.detail,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        transcript = ""
        if isinstance(result, dict):
            transcript = str(result.get("text") or "")
        transcript = transcript.strip()
        if not transcript:
            await self._send_message(
                message.chat_id,
                "Voice note transcribed to empty text.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        combined = caption_text.strip()
        if combined:
            combined = f"{combined}\n\n{transcript}"
        else:
            combined = transcript
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.transcribed",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            text_len=len(transcript),
        )
        send_placeholder = True
        transcript_message_id = None
        transcript_message = self._format_voice_transcript_message(
            combined,
            WORKING_PLACEHOLDER,
        )
        try:
            transcript_message_id = await self._send_voice_transcript_message(
                message.chat_id,
                transcript_message,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            if transcript_message_id is not None:
                send_placeholder = False
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.voice.transcript_message_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=combined,
            record=record,
            send_placeholder=send_placeholder,
            transcript_message_id=transcript_message_id,
            transcript_text=combined,
        )

    async def _download_telegram_file(
        self, file_id: str
    ) -> tuple[bytes, Optional[str], Optional[int]]:
        payload = await self._bot.get_file(file_id)
        file_path = payload.get("file_path") if isinstance(payload, dict) else None
        file_size = payload.get("file_size") if isinstance(payload, dict) else None
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        data = await self._bot.download_file(file_path)
        return data, file_path, file_size

    def _image_storage_dir(self, workspace_path: str) -> Path:
        return (
            Path(workspace_path)
            / ".codex-autorunner"
            / "uploads"
            / "telegram-images"
        )

    def _choose_image_extension(
        self,
        *,
        file_path: Optional[str],
        file_name: Optional[str],
        mime_type: Optional[str],
    ) -> str:
        for candidate in (file_path, file_name):
            if candidate:
                suffix = Path(candidate).suffix.lower()
                if suffix in IMAGE_EXTS:
                    return suffix
        if mime_type:
            base = mime_type.lower().split(";", 1)[0].strip()
            mapped = IMAGE_CONTENT_TYPES.get(base)
            if mapped:
                return mapped
        return ".img"

    def _save_image_file(
        self,
        workspace_path: str,
        data: bytes,
        file_path: Optional[str],
        candidate: TelegramMediaCandidate,
    ) -> Path:
        images_dir = self._image_storage_dir(workspace_path)
        images_dir.mkdir(parents=True, exist_ok=True)
        ext = self._choose_image_extension(
            file_path=file_path,
            file_name=candidate.file_name,
            mime_type=candidate.mime_type,
        )
        token = secrets.token_hex(6)
        name = f"telegram-{int(time.time())}-{token}{ext}"
        path = images_dir / name
        path.write_bytes(data)
        return path

    async def _handle_interrupt(self, message: TelegramMessage, runtime: Any) -> None:
        turn_id = runtime.current_turn_id
        pending_request_ids = [
            request_id
            for request_id, pending in self._pending_approvals.items()
            if pending.chat_id == message.chat_id
            and pending.thread_id == message.thread_id
        ]
        for request_id in pending_request_ids:
            pending = self._pending_approvals.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result("cancel")
            self._store.clear_pending_approval(request_id)
        if pending_request_ids:
            runtime.pending_request_id = None
        if not turn_id:
            pending = self._store.pending_approvals_for_topic(
                message.chat_id, message.thread_id
            )
            if pending:
                self._store.clear_pending_approvals_for_topic(
                    message.chat_id, message.thread_id
                )
                runtime.pending_request_id = None
                await self._send_message(
                    message.chat_id,
                    f"Cleared {len(pending)} pending approval(s).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            log_event(
                self._logger,
                logging.INFO,
                "telegram.interrupt.none",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
            await self._send_message(
                message.chat_id,
                "No active turn to interrupt.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        runtime.interrupt_requested = True
        log_event(
            self._logger,
            logging.INFO,
            "telegram.interrupt.requested",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            turn_id=turn_id,
        )
        payload_text, parse_mode = self._prepare_message("Interrupt requested.")
        response = await self._bot.send_message(
            message.chat_id,
            payload_text,
            message_thread_id=message.thread_id,
            reply_to_message_id=message.message_id,
            parse_mode=parse_mode,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if isinstance(message_id, int):
            runtime.interrupt_message_id = message_id
            runtime.interrupt_turn_id = turn_id
            self._spawn_task(
                self._interrupt_timeout_check(
                    self._resolve_topic_key(message.chat_id, message.thread_id),
                    turn_id,
                    message_id,
                )
            )
        self._spawn_task(
            self._dispatch_interrupt_request(
                turn_id=turn_id,
                runtime=runtime,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        )

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        if not args:
            options = self._list_manifest_repos()
            if not options:
                await self._send_message(
                    message.chat_id,
                    "Usage: /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(repo_id, repo_id) for repo_id in options]
            state = SelectionState(items=items)
            keyboard = self._build_bind_keyboard(state)
            self._bind_options[key] = state
            await self._send_message(
                message.chat_id,
                self._selection_prompt(BIND_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        await self._bind_topic_with_arg(key, args, message)

    async def _bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(repo_id)
        if resolved is None:
            await self._answer_callback(callback, "Repo not found")
            await self._finalize_selection(key, callback, "Repo not found.")
            return
        workspace_path, resolved_repo_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        scope = self._topic_scope_id(resolved_repo_id, workspace_path)
        self._router.set_topic_scope(chat_id, thread_id, scope)
        self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            scope=scope,
        )
        await self._answer_callback(callback, "Bound to repo")
        await self._finalize_selection(
            key,
            callback,
            f"Bound to {resolved_repo_id or workspace_path}.",
        )

    async def _bind_topic_with_arg(
        self, key: str, arg: str, message: TelegramMessage
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(arg)
        if resolved is None:
            await self._send_message(
                message.chat_id,
                "Unknown repo or path. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path, repo_id = resolved
        scope = self._topic_scope_id(repo_id, workspace_path)
        self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            scope=scope,
        )
        await self._send_message(
            message.chat_id,
            f"Bound to {repo_id or workspace_path}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        thread = await self._client.thread_start(record.workspace_path)
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new Codex thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._apply_thread_result(
            message.chat_id, message.thread_id, thread, active_thread_id=thread_id
        )
        await self._send_message(
            message.chat_id,
            f"Started new thread {thread_id}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        argv = self._parse_command_args(args)
        trimmed = args.strip()
        show_unscoped = False
        if argv and argv[0].lower() in ("--all", "all", "--unscoped", "unscoped"):
            show_unscoped = True
            trimmed = ""
        if trimmed.isdigit():
            state = self._resume_options.get(key)
            if state:
                page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
                choice = int(trimmed)
                if 0 < choice <= len(page_items):
                    thread_id = page_items[choice - 1][0]
                    await self._resume_thread_by_id(key, thread_id)
                    return
        if trimmed and not trimmed.isdigit():
            if argv and argv[0].lower() in ("list", "ls"):
                trimmed = ""
            else:
                await self._resume_thread_by_id(key, trimmed)
                return
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            threads = await self._client.thread_list(
                cursor=None,
                limit=DEFAULT_THREAD_LIST_LIMIT,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to list threads; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        normalized = _coerce_thread_list(threads)
        filtered, unscoped, saw_path = _partition_threads(
            normalized, record.workspace_path
        )
        candidates = filtered
        if show_unscoped:
            seen_ids = {entry.get("id") for entry in filtered}
            candidates = filtered + [
                entry for entry in unscoped if entry.get("id") not in seen_ids
            ]
        if not candidates:
            if unscoped and not show_unscoped and not saw_path:
                await self._send_message(
                    message.chat_id,
                    "No workspace-tagged threads available. Use /resume --all to list "
                    "unscoped threads.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                "No previous threads found for this workspace. "
                "If threads exist, update the app-server to include cwd metadata or use /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        items: list[tuple[str, str]] = []
        for entry in candidates:
            thread_id = entry.get("id")
            if not thread_id:
                continue
            items.append((thread_id, _compact_preview(entry.get("preview"))))
        if not items:
            await self._send_message(
                message.chat_id,
                "No resumable threads found.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        state = SelectionState(items=items)
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        preview = None
        state = self._resume_options.get(key)
        if state:
            for item_id, label in state.items:
                if item_id == thread_id:
                    preview = label
                    break
        self._resume_options.pop(key, None)
        try:
            result = await self._client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
            chat_id, thread_id_val = _split_topic_key(key)
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        record = self._router.get_topic(key)
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key, callback, "Topic not bound; use /bind before resuming."
            )
            return
        if not isinstance(resumed_path, str):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                "Thread metadata missing workspace path; resume aborted to avoid cross-worktree mixups.",
            )
            return
        try:
            workspace_root = Path(record.workspace_path).expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except Exception:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                "Thread workspace path is invalid; resume aborted.",
            )
            return
        if not _path_within(workspace_root, resumed_root):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                "Thread belongs to a different workspace; resume aborted.",
            )
            return
        chat_id, thread_id_val = _split_topic_key(key)
        self._apply_thread_result(
            chat_id,
            thread_id_val,
            result,
            active_thread_id=thread_id,
            overwrite_defaults=True,
        )
        await self._answer_callback(callback, "Resumed thread")
        message = f"Resumed thread {thread_id}."
        if preview and preview != "(no preview)":
            message = f"{message}\nLast: {preview}"
        await self._finalize_selection(key, callback, message)

    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        record = self._router.ensure_topic(message.chat_id, message.thread_id)
        if runtime is None:
            runtime = self._router.runtime_for(
                self._resolve_topic_key(message.chat_id, message.thread_id)
            )
        approval_policy, sandbox_policy = self._effective_policies(record)
        lines = [
            f"Workspace: {record.workspace_path or 'unbound'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Active turn: {runtime.current_turn_id or 'none'}",
            f"Model: {record.model or 'default'}",
            f"Effort: {record.effort or 'default'}",
            f"Approval mode: {record.approval_mode}",
            f"Approval policy: {approval_policy or 'default'}",
            f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
        ]
        pending = self._store.pending_approvals_for_topic(
            message.chat_id, message.thread_id
        )
        if pending:
            lines.append(f"Pending approvals: {len(pending)}")
            if len(pending) == 1:
                age = _approval_age_seconds(pending[0].created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                lines.append(
                    f"Pending request: {pending[0].request_id} ({age_label})"
                )
            else:
                preview = ", ".join(item.request_id for item in pending[:3])
                suffix = "" if len(pending) <= 3 else "..."
                lines.append(f"Pending requests: {preview}{suffix}")
        if record.summary:
            lines.append(f"Summary: {record.summary}")
        if record.active_thread_id:
            token_usage = self._token_usage_by_thread.get(record.active_thread_id)
            lines.extend(_format_token_usage(token_usage))
        rate_limits = await self._read_rate_limits()
        lines.extend(_format_rate_limits(rate_limits))
        if not record.workspace_path:
            lines.append("Use /bind <repo_id> or /bind <path>.")
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _read_rate_limits(self) -> Optional[dict[str, Any]]:
        for method in ("account/rateLimits/read", "account/read"):
            try:
                result = await self._client.request(method, params=None, timeout=5.0)
            except (CodexAppServerError, asyncio.TimeoutError):
                continue
            rate_limits = _extract_rate_limits(result)
            if rate_limits:
                return rate_limits
        return None

    async def _handle_approvals(
        self, message: TelegramMessage, args: str, _runtime: Optional[Any] = None
    ) -> None:
        argv = self._parse_command_args(args)
        record = self._router.ensure_topic(message.chat_id, message.thread_id)
        if not argv:
            approval_policy, sandbox_policy = self._effective_policies(record)
            await self._send_message(
                message.chat_id,
                "\n".join(
                    [
                        f"Approval mode: {record.approval_mode}",
                        f"Approval policy: {approval_policy or 'default'}",
                        f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
                        "Usage: /approvals yolo|safe|read-only|auto|full-access",
                    ]
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        persist = False
        if "--persist" in argv:
            persist = True
            argv = [arg for arg in argv if arg != "--persist"]
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /approvals yolo|safe|read-only|auto|full-access",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        mode = argv[0].lower()
        if mode in ("yolo", "off", "disable", "disabled"):
            self._router.set_approval_mode(message.chat_id, message.thread_id, "yolo")
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    "Approval mode set to yolo.", persist=persist
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if mode in ("safe", "on", "enable", "enabled"):
            self._router.set_approval_mode(message.chat_id, message.thread_id, "safe")
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    "Approval mode set to safe.", persist=persist
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        preset = _normalize_approval_preset(mode)
        if mode == "preset" and len(argv) > 1:
            preset = _normalize_approval_preset(argv[1])
        if preset:
            approval_policy, sandbox_policy = APPROVAL_PRESETS[preset]
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        approval_policy = argv[0] if argv[0] in APPROVAL_POLICY_VALUES else None
        if approval_policy:
            sandbox_policy = argv[1] if len(argv) > 1 else None
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy or 'default'}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Usage: /approvals yolo|safe|read-only|auto|full-access",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_model(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        self._model_options.pop(key, None)
        self._model_pending.pop(key, None)
        argv = self._parse_command_args(args)
        if not argv:
            try:
                result = await self._client.request(
                    "model/list",
                    {"cursor": None, "limit": DEFAULT_MODEL_LIST_LIMIT},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list models; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            options = _coerce_model_options(result)
            if not options:
                await self._send_message(
                    message.chat_id,
                    "No models found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(option.model_id, option.label) for option in options]
            state = ModelPickerState(
                items=items,
                options={option.model_id: option for option in options},
            )
            self._model_options[key] = state
            try:
                keyboard = self._build_model_keyboard(state)
            except ValueError:
                self._model_options.pop(key, None)
                await self._send_message(
                    message.chat_id,
                    _format_model_list(result),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._selection_prompt(MODEL_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        if argv[0].lower() in ("list", "ls"):
            try:
                result = await self._client.request(
                    "model/list",
                    {"cursor": None, "limit": DEFAULT_MODEL_LIST_LIMIT},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list models; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                _format_model_list(result),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() in ("clear", "reset"):
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_model_overrides(record, None, clear_effort=True),
            )
            await self._send_message(
                message.chat_id,
                "Model overrides cleared.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() == "set" and len(argv) > 1:
            model = argv[1]
            effort = argv[2] if len(argv) > 2 else None
        else:
            model = argv[0]
            effort = argv[1] if len(argv) > 1 else None
        if effort and effort not in VALID_REASONING_EFFORTS:
            await self._send_message(
                message.chat_id,
                f"Unknown effort '{effort}'. Allowed: {', '.join(sorted(VALID_REASONING_EFFORTS))}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda record: _set_model_overrides(
                record,
                model,
                effort=effort,
            ),
        )
        effort_note = f" (effort={effort})" if effort else ""
        await self._send_message(
            message.chat_id,
            f"Model set to {model}{effort_note}. Will apply on the next turn.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_review(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        thread_id = await self._ensure_thread_id(message, record)
        if not thread_id:
            return
        argv = self._parse_command_args(args)
        delivery = "inline"
        if argv and argv[0].lower() == "detached":
            delivery = "detached"
            argv = argv[1:]
        target: dict[str, Any] = {"type": "uncommittedChanges"}
        if argv:
            keyword = argv[0].lower()
            if keyword == "base":
                if len(argv) < 2:
                    await self._send_message(
                        message.chat_id,
                        "Usage: /review base <branch>",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                target = {"type": "baseBranch", "branch": argv[1]}
            elif keyword == "commit":
                if len(argv) < 2:
                    await self._send_message(
                        message.chat_id,
                        "Usage: /review commit <sha>",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                target = {"type": "commit", "commit": argv[1]}
            elif keyword == "custom":
                instructions = " ".join(argv[1:]).strip()
                if not instructions:
                    await self._send_message(
                        message.chat_id,
                        "Usage: /review custom <instructions>",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                target = {"type": "custom", "instructions": instructions}
            else:
                target = {"type": "custom", "instructions": " ".join(argv)}
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            delivery=delivery,
            target=target.get("type"),
        )
        approval_policy, sandbox_policy = self._effective_policies(record)
        review_kwargs: dict[str, Any] = {}
        if approval_policy:
            review_kwargs["approval_policy"] = approval_policy
        if sandbox_policy:
            review_kwargs["sandbox_policy"] = sandbox_policy
        if record.model:
            review_kwargs["model"] = record.model
        if record.effort:
            review_kwargs["effort"] = record.effort
        if record.summary:
            review_kwargs["summary"] = record.summary
        turn_handle = None
        placeholder_id: Optional[int] = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
        try:
            async with self._turn_semaphore:
                placeholder_id = await self._send_placeholder(
                    message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                turn_handle = await self._client.review_start(
                    thread_id,
                    target=target,
                    delivery=delivery,
                    **review_kwargs,
                )
                turn_started_at = time.monotonic()
                runtime.current_turn_id = turn_handle.turn_id
                ctx = TurnContext(
                    topic_key=self._resolve_topic_key(
                        message.chat_id, message.thread_id
                    ),
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                self._turn_contexts[turn_handle.turn_id] = ctx
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.review.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    "Codex review failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
            return
        finally:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
        response = _compose_agent_response(result.agent_messages)
        if result.status == "interrupted" or runtime.interrupt_requested:
            response = _compose_interrupt_response(response)
            runtime.interrupt_requested = False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.completed",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_handle.turn_id if turn_handle else None,
            agent_message_count=len(result.agent_messages),
        )
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response,
        )
        turn_id = turn_handle.turn_id if turn_handle else None
        token_usage = (
            self._token_usage_by_turn.get(turn_id)
            if turn_id
            else None
        )
        if token_usage is None and thread_id:
            token_usage = self._token_usage_by_thread.get(thread_id)
        await self._send_turn_metrics(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            elapsed_seconds=turn_elapsed_seconds,
            token_usage=token_usage,
        )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent:
            await self._delete_message(message.chat_id, placeholder_id)

    async def _handle_diff(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        command = (
            "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || "
            "{ echo 'Not a git repo'; exit 0; }\n"
            "git diff --color;\n"
            "git ls-files --others --exclude-standard | "
            "while read -r f; do git diff --color --no-index -- /dev/null \"$f\"; done"
        )
        try:
            result = await self._client.request(
                "command/exec",
                {
                    "cwd": record.workspace_path,
                    "command": ["bash", "-lc", command],
                    "timeoutMs": 10000,
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.diff.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to compute diff; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        output = _render_command_output(result)
        if not output.strip():
            output = "(No diff output.)"
        await self._send_message(
            message.chat_id,
            output,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_mention(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        argv = self._parse_command_args(args)
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /mention <path> [request]",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace = Path(record.workspace_path or "").expanduser().resolve()
        path = Path(argv[0]).expanduser()
        if not path.is_absolute():
            path = workspace / path
        try:
            path = path.resolve()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Could not resolve that path.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not _path_within(workspace, path):
            await self._send_message(
                message.chat_id,
                "File must be within the bound workspace.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not path.exists() or not path.is_file():
            await self._send_message(
                message.chat_id,
                "File not found.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data = path.read_bytes()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Failed to read file.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > MAX_MENTION_BYTES:
            await self._send_message(
                message.chat_id,
                f"File too large (max {MAX_MENTION_BYTES} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if _looks_binary(data):
            await self._send_message(
                message.chat_id,
                "File appears to be binary; refusing to include it.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        text = data.decode("utf-8", errors="replace")
        try:
            display_path = str(path.relative_to(workspace))
        except ValueError:
            display_path = str(path)
        request = " ".join(argv[1:]).strip()
        if not request:
            request = "Please review this file."
        prompt = "\n".join(
            [
                "Please use the file below as authoritative context.",
                "",
                f'<file path="{display_path}">',
                text,
                "</file>",
                "",
                f"My request: {request}",
            ]
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt,
            record=record,
        )

    async def _handle_skills(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        try:
            result = await self._client.request(
                "skills/list",
                {"cwds": [record.workspace_path], "forceReload": False},
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.skills.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to list skills; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            _format_skills_list(result, record.workspace_path),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_mcp(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        try:
            result = await self._client.request(
                "mcpServerStatus/list",
                {"cursor": None, "limit": DEFAULT_MCP_LIST_LIMIT},
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.mcp.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to list MCP servers; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            _format_mcp_list(result),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_experimental(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        if not argv:
            try:
                result = await self._client.request(
                    "config/read",
                    {"includeLayers": False},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.experimental.read_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to read config; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                _format_feature_flags(result),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(argv) < 2:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        action = argv[0].lower()
        feature = argv[1].strip()
        if not feature:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if action in ("enable", "on", "true", "1"):
            value = True
        elif action in ("disable", "off", "false", "0"):
            value = False
        else:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key_path = feature if feature.startswith("features.") else f"features.{feature}"
        try:
            await self._client.request(
                "config/value/write",
                {
                    "keyPath": key_path,
                    "value": value,
                    "mergeStrategy": "replace",
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.experimental.write_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to update feature flag; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            f"Feature {key_path} set to {value}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_init(
        self, message: TelegramMessage, _args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        await self._handle_normal_message(
            message,
            runtime,
            text_override=INIT_PROMPT,
            record=record,
        )

    async def _handle_compact(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        if argv and argv[0].lower() in ("soft", "summary", "summarize"):
            record = await self._require_bound_record(message)
            if not record:
                return
            await self._handle_normal_message(
                message,
                runtime,
                text_override=COMPACT_SUMMARY_PROMPT,
                record=record,
            )
            return
        await self._send_message(
            message.chat_id,
            "Compact is not available via the app-server. Use /new or /compact soft for a summary.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_rollout(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = self._router.get_topic(
            self._resolve_topic_key(message.chat_id, message.thread_id)
        )
        if record is None or not record.active_thread_id:
            await self._send_message(
                message.chat_id,
                "No active thread to inspect.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record.rollout_path:
            await self._send_message(
                message.chat_id,
                f"Rollout path: {record.rollout_path}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            threads = await self._client.thread_list(
                cursor=None,
                limit=DEFAULT_THREAD_LIST_LIMIT,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.rollout.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to look up rollout path; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        entry = _find_thread_entry(threads, record.active_thread_id)
        rollout_path = _extract_rollout_path(entry) if entry else None
        if rollout_path:
            self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_rollout_path(record, rollout_path),
            )
            await self._send_message(
                message.chat_id,
                f"Rollout path: {rollout_path}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Rollout path not found for this thread.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _start_update(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        update_target: str,
        reply_to: Optional[int] = None,
        callback: Optional[TelegramCallbackQuery] = None,
        selection_key: Optional[str] = None,
    ) -> None:
        repo_url = (self._update_repo_url or DEFAULT_UPDATE_REPO_URL).strip()
        if not repo_url:
            repo_url = DEFAULT_UPDATE_REPO_URL
        update_dir = Path.home() / ".codex-autorunner" / "update_cache"
        try:
            _spawn_update_process(
                repo_url=repo_url,
                update_dir=update_dir,
                logger=self._logger,
                update_target=update_target,
            )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.update.started",
                chat_id=chat_id,
                thread_id=thread_id,
                update_target=update_target,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                update_target=update_target,
                exc=exc,
            )
            failure = _with_conversation_id(
                "Update failed to start; check logs for details.",
                chat_id=chat_id,
                thread_id=thread_id,
            )
            if callback and selection_key:
                await self._answer_callback(callback, "Update failed")
                await self._finalize_selection(selection_key, callback, failure)
            else:
                await self._send_message(
                    chat_id,
                    failure,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
            return
        message = (
            f"Update started ({update_target}). The selected service(s) will restart."
        )
        if callback and selection_key:
            await self._answer_callback(callback, "Update started")
            await self._finalize_selection(selection_key, callback, message)
            return
        await self._send_message(
            chat_id,
            message,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    async def _prompt_update_selection(
        self,
        message: TelegramMessage,
        *,
        prompt: str = UPDATE_PICKER_PROMPT,
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        state = SelectionState(items=list(UPDATE_TARGET_OPTIONS))
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        await self._send_message(
            message.chat_id,
            prompt,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_update(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        target_raw = argv[0] if argv else None
        if not target_raw:
            await self._prompt_update_selection(message)
            return
        try:
            update_target = _normalize_update_target(target_raw)
        except ValueError:
            await self._prompt_update_selection(
                message,
                prompt="Unknown update target. Select update target (buttons below).",
            )
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        self._update_options.pop(key, None)
        await self._start_update(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            update_target=update_target,
            reply_to=message.message_id,
        )

    async def _handle_logout(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        try:
            await self._client.request("account/logout", params=None)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.logout.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Logout failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Logged out.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_feedback(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        reason = args.strip()
        if not reason:
            await self._send_message(
                message.chat_id,
                "Usage: /feedback <reason>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        record = self._router.get_topic(
            self._resolve_topic_key(message.chat_id, message.thread_id)
        )
        params: dict[str, Any] = {
            "classification": "bug",
            "reason": reason,
            "includeLogs": True,
        }
        if record and record.active_thread_id:
            params["threadId"] = record.active_thread_id
        try:
            result = await self._client.request("feedback/upload", params)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.feedback.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Feedback upload failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        report_id = None
        if isinstance(result, dict):
            report_id = result.get("threadId") or result.get("id")
        message_text = "Feedback sent."
        if isinstance(report_id, str):
            message_text = f"Feedback sent (report {report_id})."
        await self._send_message(
            message.chat_id,
            message_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_quit(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        await self._send_message(
            message.chat_id,
            "This command is not applicable in Telegram. Use /new to start fresh or /resume to switch threads.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method != "thread/tokenUsage/updated":
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = params.get("threadId")
        turn_id_raw = params.get("turnId")
        turn_id = None
        if isinstance(turn_id_raw, (str, int)) and not isinstance(turn_id_raw, bool):
            turn_id = str(turn_id_raw).strip()
        token_usage = params.get("tokenUsage")
        if not isinstance(thread_id, str) or not isinstance(token_usage, dict):
            return
        self._token_usage_by_thread[thread_id] = token_usage
        self._token_usage_by_thread.move_to_end(thread_id)
        while len(self._token_usage_by_thread) > TOKEN_USAGE_CACHE_LIMIT:
            self._token_usage_by_thread.popitem(last=False)
        if turn_id:
            self._token_usage_by_turn[turn_id] = token_usage
            self._token_usage_by_turn.move_to_end(turn_id)
            while len(self._token_usage_by_turn) > TOKEN_USAGE_TURN_CACHE_LIMIT:
                self._token_usage_by_turn.popitem(last=False)

    async def _handle_approval_request(self, message: dict[str, Any]) -> ApprovalDecision:
        req_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        turn_id = params.get("turnId") if isinstance(params, dict) else None
        if not req_id or not turn_id:
            return "cancel"
        ctx = self._turn_contexts.get(str(turn_id))
        if ctx is None:
            return "cancel"
        request_id = str(req_id)
        prompt = _format_approval_prompt(message)
        created_at = now_iso()
        approval_record = PendingApprovalRecord(
            request_id=request_id,
            turn_id=str(turn_id),
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            message_id=None,
            prompt=prompt,
            created_at=created_at,
        )
        self._store.upsert_pending_approval(approval_record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.requested",
            request_id=request_id,
            turn_id=turn_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
        )
        try:
            keyboard = build_approval_keyboard(request_id, include_session=False)
        except ValueError:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.callback_too_long",
                request_id=request_id,
            )
            self._store.clear_pending_approval(request_id)
            return "cancel"
        payload_text, parse_mode = self._prepare_message(prompt)
        try:
            response = await self._bot.send_message(
                ctx.chat_id,
                payload_text,
                message_thread_id=ctx.thread_id,
                reply_to_message_id=ctx.reply_to_message_id,
                reply_markup=keyboard,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.send_failed",
                request_id=request_id,
                turn_id=turn_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                exc=exc,
            )
            self._store.clear_pending_approval(request_id)
            try:
                await self._send_message(
                    ctx.chat_id,
                    "Approval prompt failed to send; canceling approval. "
                    "Please retry or use /interrupt.",
                    thread_id=ctx.thread_id,
                    reply_to=ctx.reply_to_message_id,
                )
            except Exception:
                pass
            return "cancel"
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if isinstance(message_id, int):
            approval_record.message_id = message_id
            self._store.upsert_pending_approval(approval_record)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        pending = PendingApproval(
            request_id=request_id,
            turn_id=str(turn_id),
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            message_id=message_id if isinstance(message_id, int) else None,
            created_at=created_at,
            future=future,
        )
        self._pending_approvals[request_id] = pending
        runtime = self._router.runtime_for(ctx.topic_key)
        runtime.pending_request_id = request_id
        try:
            return await asyncio.wait_for(
                future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._pending_approvals.pop(request_id, None)
            self._store.clear_pending_approval(request_id)
            runtime.pending_request_id = None
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.timeout",
                request_id=request_id,
                turn_id=turn_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                timeout_seconds=DEFAULT_APPROVAL_TIMEOUT_SECONDS,
            )
            if pending.message_id is not None:
                await self._edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    "Approval timed out.",
                    reply_markup={"inline_keyboard": []},
                )
            return "cancel"
        except asyncio.CancelledError:
            self._pending_approvals.pop(request_id, None)
            self._store.clear_pending_approval(request_id)
            runtime.pending_request_id = None
            raise

    async def _handle_approval_callback(
        self, callback: TelegramCallbackQuery, parsed: ApprovalCallback
    ) -> None:
        self._store.clear_pending_approval(parsed.request_id)
        pending = self._pending_approvals.pop(parsed.request_id, None)
        if pending is None:
            await self._answer_callback(callback, "Approval already handled")
            return
        if not pending.future.done():
            pending.future.set_result(parsed.decision)
        ctx = self._turn_contexts.get(pending.turn_id)
        if ctx:
            runtime = self._router.runtime_for(ctx.topic_key)
        else:
            runtime = self._router.runtime_for(
                self._resolve_topic_key(pending.chat_id, pending.thread_id)
            )
        runtime.pending_request_id = None
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.decision",
            request_id=parsed.request_id,
            decision=parsed.decision,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            message_id=callback.message_id,
        )
        await self._answer_callback(callback, f"Decision: {parsed.decision}")
        if pending.message_id is not None:
            try:
                await self._edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    _format_approval_decision(parsed.decision),
                    reply_markup={"inline_keyboard": []},
                )
            except Exception:
                return

    def _selection_prompt(self, base: str, state: SelectionState) -> str:
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        return _format_selection_prompt(base, state.page, total_pages)

    def _page_button(
        self, kind: str, state: SelectionState
    ) -> Optional[tuple[str, str]]:
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        if total_pages <= 1:
            return None
        next_page = (state.page + 1) % total_pages
        return ("More...", encode_page_callback(kind, next_page))

    def _build_resume_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_resume_keyboard(
            options,
            page_button=self._page_button("resume", state),
            include_cancel=True,
        )

    def _build_bind_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_bind_keyboard(
            options,
            page_button=self._page_button("bind", state),
            include_cancel=True,
        )

    def _build_update_keyboard(self, state: SelectionState) -> dict[str, Any]:
        options = list(state.items)
        return build_update_keyboard(options, include_cancel=True)

    def _build_model_keyboard(self, state: ModelPickerState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_model_keyboard(
            options,
            page_button=self._page_button("model", state),
            include_cancel=True,
        )

    def _build_effort_keyboard(self, option: ModelOption) -> dict[str, Any]:
        options = []
        for effort in option.efforts:
            label = effort
            if option.default_effort and effort == option.default_effort:
                label = f"{effort} (default)"
            options.append((effort, label))
        return build_effort_keyboard(options, include_cancel=True)

    def _render_message(self, text: str) -> tuple[str, Optional[str]]:
        parse_mode = self._config.parse_mode
        if not parse_mode:
            return text, None
        if parse_mode == "HTML":
            return _format_telegram_html(text), parse_mode
        if parse_mode in ("Markdown", "MarkdownV2"):
            return _format_telegram_markdown(text, parse_mode), parse_mode
        return text, parse_mode

    def _prepare_message(self, text: str) -> tuple[str, Optional[str]]:
        rendered, parse_mode = self._render_message(text)
        # Avoid parse_mode when chunking to keep markup intact.
        if parse_mode and len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return rendered, parse_mode
        return text, None

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        try:
            payload_text, parse_mode = self._prepare_message(text)
            await self._bot.edit_message_text(
                chat_id,
                message_id,
                payload_text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception:
            return False
        return True

    async def _delete_message(self, chat_id: int, message_id: Optional[int]) -> bool:
        if message_id is None:
            return False
        try:
            return bool(await self._bot.delete_message(chat_id, message_id))
        except Exception:
            return False

    async def _edit_callback_message(
        self,
        callback: TelegramCallbackQuery,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        if callback.chat_id is None or callback.message_id is None:
            return False
        return await self._edit_message_text(
            callback.chat_id,
            callback.message_id,
            text,
            reply_markup=reply_markup,
        )

    def _format_voice_transcript_message(self, text: str, agent_status: str) -> str:
        header = "User:\n"
        footer = f"\n\nAgent:\n{agent_status}"
        max_len = TELEGRAM_MAX_MESSAGE_LENGTH
        available = max_len - len(header) - len(footer)
        if available <= 0:
            return f"{header}{footer.lstrip()}"
        transcript = text
        truncation_note = "\n\n...(truncated)"
        if len(transcript) > available:
            remaining = available - len(truncation_note)
            if remaining < 0:
                remaining = 0
            transcript = transcript[:remaining].rstrip()
            transcript = f"{transcript}{truncation_note}"
        return f"{header}{transcript}{footer}"

    async def _send_voice_transcript_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> Optional[int]:
        payload_text, parse_mode = self._prepare_message(text)
        response = await self._bot.send_message(
            chat_id,
            payload_text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _finalize_voice_transcript(
        self,
        chat_id: int,
        message_id: Optional[int],
        transcript_text: Optional[str],
    ) -> None:
        if message_id is None or transcript_text is None:
            return
        final_message = self._format_voice_transcript_message(
            transcript_text,
            "Reply below.",
        )
        await self._edit_message_text(chat_id, message_id, final_message)

    async def _send_placeholder(
        self,
        chat_id: int,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> Optional[int]:
        try:
            payload_text, parse_mode = self._prepare_message(WORKING_PLACEHOLDER)
            response = await self._bot.send_message(
                chat_id,
                payload_text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.placeholder.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to,
                exc=exc,
            )
            return None
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
    ) -> bool:
        return await self._send_message_with_outbox(
            chat_id,
            response,
            thread_id=thread_id,
            reply_to=reply_to,
            placeholder_id=placeholder_id,
        )

    async def _send_turn_metrics(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        elapsed_seconds: Optional[float],
        token_usage: Optional[dict[str, Any]],
    ) -> bool:
        metrics = _format_turn_metrics(token_usage, elapsed_seconds)
        if not metrics:
            return False
        return await self._send_message_with_outbox(
            chat_id,
            metrics,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    async def _update_selection_message(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        text: str,
        reply_markup: dict[str, Any],
    ) -> None:
        if await self._edit_callback_message(
            callback, text, reply_markup=reply_markup
        ):
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._send_message(
            chat_id,
            text,
            thread_id=thread_id,
            reply_markup=reply_markup,
        )

    async def _finalize_selection(
        self,
        key: str,
        callback: Optional[TelegramCallbackQuery],
        text: str,
    ) -> None:
        if callback and await self._edit_callback_message(
            callback, text, reply_markup={"inline_keyboard": []}
        ):
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._send_message(chat_id, text, thread_id=thread_id)

    async def _handle_selection_cancel(
        self,
        key: str,
        parsed: CancelCallback,
        callback: TelegramCallbackQuery,
    ) -> None:
        if parsed.kind == "resume":
            self._resume_options.pop(key, None)
            text = "Resume selection cancelled."
        elif parsed.kind == "bind":
            self._bind_options.pop(key, None)
            text = "Bind selection cancelled."
        elif parsed.kind == "model":
            self._model_options.pop(key, None)
            self._model_pending.pop(key, None)
            text = "Model selection cancelled."
        elif parsed.kind == "update":
            self._update_options.pop(key, None)
            text = "Update cancelled."
        else:
            await self._answer_callback(callback, "Selection expired")
            return
        await self._answer_callback(callback, "Cancelled")
        await self._finalize_selection(key, callback, text)

    async def _handle_selection_page(
        self,
        key: str,
        parsed: PageCallback,
        callback: TelegramCallbackQuery,
    ) -> None:
        if parsed.kind == "resume":
            state = self._resume_options.get(key)
            prompt_base = RESUME_PICKER_PROMPT
            build_keyboard = self._build_resume_keyboard
        elif parsed.kind == "bind":
            state = self._bind_options.get(key)
            prompt_base = BIND_PICKER_PROMPT
            build_keyboard = self._build_bind_keyboard
        elif parsed.kind == "model":
            state = self._model_options.get(key)
            prompt_base = MODEL_PICKER_PROMPT
            build_keyboard = self._build_model_keyboard
        else:
            await self._answer_callback(callback, "Selection expired")
            return
        if not state:
            await self._answer_callback(callback, "Selection expired")
            return
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        if total_pages <= 1:
            await self._answer_callback(callback, "No more pages")
            return
        page = parsed.page % total_pages
        state.page = page
        prompt = _format_selection_prompt(prompt_base, page, total_pages)
        keyboard = build_keyboard(state)
        await self._update_selection_message(key, callback, prompt, keyboard)
        await self._answer_callback(callback, f"Page {page + 1}/{total_pages}")

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        payload_text, parse_mode = self._prepare_message(text)
        await self._bot.send_message_chunks(
            chat_id,
            payload_text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def _answer_callback(
        self, callback: Optional[TelegramCallbackQuery], text: str
    ) -> None:
        if callback is None:
            return
        await self._bot.answer_callback_query(callback.callback_id, text=text)

    def _list_manifest_repos(self) -> list[str]:
        if not self._manifest_path or not self._hub_root:
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except Exception:
            return []
        repo_ids = [repo.id for repo in manifest.repos if repo.enabled]
        return repo_ids

    def _resolve_workspace(self, arg: str) -> Optional[tuple[str, Optional[str]]]:
        arg = (arg or "").strip()
        if not arg:
            return None
        if self._manifest_path and self._hub_root:
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                repo = manifest.get(arg)
                if repo:
                    workspace = (self._hub_root / repo.path).resolve()
                    return str(workspace), repo.id
            except Exception:
                pass
        path = Path(arg)
        if not path.is_absolute():
            path = (self._config.root / path).resolve()
        if path.exists():
            return str(path), None
        return None


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_thread_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else None
    workspace_path = _extract_thread_path(payload)
    if not workspace_path and isinstance(thread, dict):
        workspace_path = _extract_thread_path(thread)
    rollout_path = None
    if isinstance(thread, dict):
        rollout_path = thread.get("path") if isinstance(thread.get("path"), str) else None
    if rollout_path is None and isinstance(payload.get("path"), str):
        rollout_path = payload.get("path")
    model = None
    for key in ("model", "modelId"):
        value = payload.get(key)
        if isinstance(value, str):
            model = value
            break
    if model is None and isinstance(thread, dict):
        for key in ("model", "modelId"):
            value = thread.get(key)
            if isinstance(value, str):
                model = value
                break
    effort = payload.get("reasoningEffort") or payload.get("effort")
    if not isinstance(effort, str) and isinstance(thread, dict):
        effort = thread.get("reasoningEffort") or thread.get("effort")
    if not isinstance(effort, str):
        effort = None
    summary = payload.get("summary") or payload.get("summaryMode")
    if not isinstance(summary, str) and isinstance(thread, dict):
        summary = thread.get("summary") or thread.get("summaryMode")
    if not isinstance(summary, str):
        summary = None
    approval_policy = payload.get("approvalPolicy") or payload.get("approval_policy")
    if not isinstance(approval_policy, str) and isinstance(thread, dict):
        approval_policy = thread.get("approvalPolicy") or thread.get("approval_policy")
    if not isinstance(approval_policy, str):
        approval_policy = None
    sandbox_policy = payload.get("sandboxPolicy") or payload.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)) and isinstance(thread, dict):
        sandbox_policy = thread.get("sandboxPolicy") or thread.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)):
        sandbox_policy = None
    return {
        "thread_id": _extract_thread_id(payload),
        "workspace_path": workspace_path,
        "rollout_path": rollout_path,
        "model": model,
        "effort": effort,
        "summary": summary,
        "approval_policy": approval_policy,
        "sandbox_policy": sandbox_policy,
    }


def _normalize_approval_preset(raw: str) -> Optional[str]:
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if cleaned in ("readonly", "read-only", "read_only"):
        return "read-only"
    if cleaned in ("fullaccess", "full-access", "full_access", "full"):
        return "full-access"
    if cleaned in ("auto", "agent"):
        return "auto"
    return None


def _clear_policy_overrides(record: "TelegramTopicRecord") -> None:
    record.approval_policy = None
    record.sandbox_policy = None


def _set_policy_overrides(
    record: "TelegramTopicRecord",
    *,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[Any] = None,
) -> None:
    if approval_policy is not None:
        record.approval_policy = approval_policy
    if sandbox_policy is not None:
        record.sandbox_policy = sandbox_policy


def _set_model_overrides(
    record: "TelegramTopicRecord",
    model: Optional[str],
    *,
    effort: Optional[str] = None,
    clear_effort: bool = False,
) -> None:
    record.model = model
    if effort is not None:
        record.effort = effort
    elif clear_effort:
        record.effort = None


def _set_rollout_path(record: "TelegramTopicRecord", rollout_path: str) -> None:
    record.rollout_path = rollout_path


def _format_conversation_id(chat_id: int, thread_id: Optional[int]) -> str:
    return topic_key(chat_id, thread_id)


def _with_conversation_id(
    message: str, *, chat_id: int, thread_id: Optional[int]
) -> str:
    conversation_id = _format_conversation_id(chat_id, thread_id)
    return f"{message} (conversation {conversation_id})"


def _format_persist_note(message: str, *, persist: bool) -> str:
    if not persist:
        return message
    return f"{message} (Persistence is not supported in Telegram; applied to this topic only.)"


def _format_sandbox_policy(sandbox_policy: Any) -> str:
    if sandbox_policy is None:
        return "default"
    if isinstance(sandbox_policy, str):
        return sandbox_policy
    if isinstance(sandbox_policy, dict):
        sandbox_type = sandbox_policy.get("type")
        if isinstance(sandbox_type, str):
            suffix = ""
            if "networkAccess" in sandbox_policy:
                suffix = f", network={sandbox_policy.get('networkAccess')}"
            return f"{sandbox_type}{suffix}"
    return str(sandbox_policy)


def _format_token_usage(token_usage: Optional[dict[str, Any]]) -> list[str]:
    if not token_usage:
        return []
    lines: list[str] = []
    total = token_usage.get("total") if isinstance(token_usage, dict) else None
    last = token_usage.get("last") if isinstance(token_usage, dict) else None
    if isinstance(total, dict):
        total_line = _format_token_row("Token usage (total)", total)
        if total_line:
            lines.append(total_line)
    if isinstance(last, dict):
        last_line = _format_token_row("Token usage (last)", last)
        if last_line:
            lines.append(last_line)
    context = token_usage.get("modelContextWindow") if isinstance(token_usage, dict) else None
    if isinstance(context, int):
        lines.append(f"Context window: {context}")
    return lines


def _extract_rate_limits(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    for key in ("rateLimits", "rate_limits", "limits"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    if "primary" in payload or "secondary" in payload:
        return payload
    return None


def _format_rate_limits(rate_limits: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(rate_limits, dict):
        return []
    parts: list[str] = []
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict):
            continue
        used_value = entry.get("used_percent", entry.get("usedPercent"))
        used = _coerce_number(used_value)
        if used is None:
            used = _compute_used_percent(entry)
        used_text = _format_percent(used)
        window_minutes = _coerce_int(entry.get("window_minutes", entry.get("windowMinutes")))
        if window_minutes is None:
            window_seconds = _coerce_int(entry.get("window_seconds", entry.get("windowSeconds")))
            if window_seconds is not None:
                window_minutes = max(int(round(window_seconds / 60)), 1)
        label = _format_rate_limit_window(window_minutes) or key
        if used_text:
            parts.append(f"{label} used {used_text}")
    if not parts:
        return []
    return [f"Rate limits: {', '.join(parts)}"]


def _compute_used_percent(entry: dict[str, Any]) -> Optional[float]:
    remaining = _coerce_number(entry.get("remaining"))
    limit = _coerce_number(entry.get("limit"))
    if remaining is None or limit is None or limit <= 0:
        return None
    used = (limit - remaining) / limit * 100
    return max(min(used, 100.0), 0.0)


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> Optional[int]:
    number = _coerce_number(value)
    if number is None:
        return None
    return int(number)


def _format_percent(value: Any) -> Optional[str]:
    number = _coerce_number(value)
    if number is None:
        return None
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _format_rate_limit_window(window_minutes: Optional[int]) -> Optional[str]:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return None
    if window_minutes == 300:
        return "5h"
    if window_minutes == 10080:
        return "weekly"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def _extract_usage_value(
    token_usage: Optional[dict[str, Any]],
    section: str,
    key: str,
) -> Optional[int]:
    if not isinstance(token_usage, dict):
        return None
    section_value = token_usage.get(section)
    if not isinstance(section_value, dict):
        return None
    value = section_value.get(key)
    if isinstance(value, int):
        return value
    return None


def _context_remaining_percent(total_tokens: int, context_window: int) -> Optional[int]:
    effective_window = context_window - CONTEXT_BASELINE_TOKENS
    if effective_window <= 0:
        return None
    used = max(total_tokens - CONTEXT_BASELINE_TOKENS, 0)
    remaining = max(effective_window - used, 0)
    percent = round(max(min(remaining / effective_window * 100, 100), 0))
    return int(percent)


def _format_context_metrics(token_usage: Optional[dict[str, Any]]) -> Optional[str]:
    total_tokens = _extract_usage_value(token_usage, "total", "totalTokens")
    last_tokens = _extract_usage_value(token_usage, "last", "totalTokens")
    if total_tokens is None and last_tokens is None:
        return None
    used_tokens = total_tokens if total_tokens is not None else last_tokens
    if used_tokens is None:
        return None
    context_window = token_usage.get("modelContextWindow") if isinstance(token_usage, dict) else None
    if not isinstance(context_window, int):
        context_window = None
    if context_window is None:
        return f"Context: used {used_tokens:,} tokens."
    remaining = max(context_window - used_tokens, 0)
    percent = _context_remaining_percent(used_tokens, context_window)
    if percent is None:
        return (
            f"Context: used {used_tokens:,} tokens; remaining {remaining:,} of {context_window:,}."
        )
    return (
        "Context: used "
        f"{used_tokens:,} tokens; remaining {remaining:,} of {context_window:,} "
        f"({percent}% remaining)."
    )


def _format_tui_token_usage(token_usage: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    total = token_usage.get("total")
    usage = last if isinstance(last, dict) else total if isinstance(total, dict) else None
    if not isinstance(usage, dict):
        return None
    total_tokens = usage.get("totalTokens")
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if not isinstance(total_tokens, int):
        return None
    parts = [f"Token usage: total {total_tokens}"]
    if isinstance(input_tokens, int):
        parts.append(f"input {input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"output {output_tokens}")
    context_window = token_usage.get("modelContextWindow")
    if isinstance(context_window, int) and context_window > 0:
        remaining = max(context_window - total_tokens, 0)
        percent = round(remaining / context_window * 100)
        parts.append(f"{percent}% left")
    return " ".join(parts)


def _format_turn_metrics(
    token_usage: Optional[dict[str, Any]],
    elapsed_seconds: Optional[float],
) -> Optional[str]:
    lines: list[str] = []
    if elapsed_seconds is not None:
        lines.append(f"Turn time: {elapsed_seconds:.1f}s")
    token_line = _format_tui_token_usage(token_usage)
    if token_line:
        lines.append(token_line)
    if not lines:
        return None
    return "\n".join(lines)


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _approval_age_seconds(created_at: Optional[str]) -> Optional[int]:
    dt = _parse_iso_timestamp(created_at)
    if dt is None:
        return None
    return max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)


def _format_token_row(label: str, usage: dict[str, Any]) -> Optional[str]:
    total_tokens = usage.get("totalTokens")
    input_tokens = usage.get("inputTokens")
    cached_input_tokens = usage.get("cachedInputTokens")
    output_tokens = usage.get("outputTokens")
    reasoning_tokens = usage.get("reasoningTokens")
    if reasoning_tokens is None:
        reasoning_tokens = usage.get("reasoningOutputTokens")
    parts: list[str] = []
    if isinstance(total_tokens, int):
        parts.append(f"total={total_tokens}")
    if isinstance(input_tokens, int):
        parts.append(f"in={input_tokens}")
    if isinstance(cached_input_tokens, int):
        parts.append(f"cached={cached_input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"out={output_tokens}")
    if isinstance(reasoning_tokens, int):
        parts.append(f"reasoning={reasoning_tokens}")
    if not parts:
        return None
    return f"{label}: " + " ".join(parts)


def _coerce_model_entries(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in ("data", "models", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _coerce_model_options(result: Any) -> list[ModelOption]:
    entries = _coerce_model_entries(result)
    options: list[ModelOption] = []
    for entry in entries:
        model = entry.get("model") or entry.get("id")
        if not isinstance(model, str) or not model:
            continue
        display_name = entry.get("displayName")
        label = model
        if isinstance(display_name, str) and display_name and display_name != model:
            label = f"{model} ({display_name})"
        default_effort = entry.get("defaultReasoningEffort")
        if not isinstance(default_effort, str):
            default_effort = None
        efforts_raw = entry.get("supportedReasoningEfforts")
        efforts: list[str] = []
        if isinstance(efforts_raw, list):
            for effort in efforts_raw:
                if isinstance(effort, dict):
                    value = effort.get("reasoningEffort")
                    if isinstance(value, str):
                        efforts.append(value)
                elif isinstance(effort, str):
                    efforts.append(effort)
        if default_effort and default_effort not in efforts:
            efforts.append(default_effort)
        efforts = [effort for effort in efforts if effort]
        if not efforts:
            efforts = sorted(VALID_REASONING_EFFORTS)
        efforts = list(dict.fromkeys(efforts))
        if default_effort:
            label = f"{label} (default {default_effort})"
        options.append(
            ModelOption(
                model_id=model,
                label=label,
                efforts=tuple(efforts),
                default_effort=default_effort,
            )
        )
    return options


def _format_model_list(result: Any) -> str:
    entries = _coerce_model_entries(result)
    if not entries:
        return "No models found."
    lines = ["Available models:"]
    for entry in entries[:DEFAULT_MODEL_LIST_LIMIT]:
        model = entry.get("model") or entry.get("id") or "(unknown)"
        display_name = entry.get("displayName")
        label = str(model)
        if isinstance(display_name, str) and display_name and display_name != model:
            label = f"{model} ({display_name})"
        efforts = entry.get("supportedReasoningEfforts")
        effort_values: list[str] = []
        if isinstance(efforts, list):
            for effort in efforts:
                if isinstance(effort, dict):
                    value = effort.get("reasoningEffort")
                    if isinstance(value, str):
                        effort_values.append(value)
                elif isinstance(effort, str):
                    effort_values.append(effort)
        if effort_values:
            label = f"{label} [effort: {', '.join(effort_values)}]"
        default_effort = entry.get("defaultReasoningEffort")
        if isinstance(default_effort, str):
            label = f"{label} (default {default_effort})"
        lines.append(label)
    if len(entries) > DEFAULT_MODEL_LIST_LIMIT:
        lines.append(f"...and {len(entries) - DEFAULT_MODEL_LIST_LIMIT} more.")
    lines.append("Use /model <id> [effort] to set.")
    return "\n".join(lines)


def _format_feature_flags(result: Any) -> str:
    config = result.get("config") if isinstance(result, dict) else None
    if config is None and isinstance(result, dict):
        config = result
    if not isinstance(config, dict):
        return "No feature flags found."
    features = config.get("features")
    if not isinstance(features, dict) or not features:
        return "No feature flags found."
    lines = ["Feature flags:"]
    for key in sorted(features.keys()):
        value = features.get(key)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_skills_list(result: Any, workspace_path: Optional[str]) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    skills: list[tuple[str, str]] = []
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(workspace_path, str) and isinstance(cwd, str):
            if Path(cwd).expanduser().resolve() != Path(workspace_path).expanduser().resolve():
                continue
        items = entry.get("skills")
        if isinstance(items, list):
            for skill in items:
                if not isinstance(skill, dict):
                    continue
                name = skill.get("name")
                if not isinstance(name, str) or not name:
                    continue
                description = skill.get("shortDescription") or skill.get("description")
                desc_text = (
                    description.strip() if isinstance(description, str) and description else ""
                )
                skills.append((name, desc_text))
    if not skills:
        return "No skills found."
    lines = ["Skills:"]
    for name, desc in skills[:DEFAULT_SKILLS_LIST_LIMIT]:
        if desc:
            lines.append(f"{name} - {desc}")
        else:
            lines.append(name)
    if len(skills) > DEFAULT_SKILLS_LIST_LIMIT:
        lines.append(f"...and {len(skills) - DEFAULT_SKILLS_LIST_LIMIT} more.")
    lines.append("Use $<SkillName> in your next message to invoke a skill.")
    return "\n".join(lines)


def _format_mcp_list(result: Any) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    if not entries:
        return "No MCP servers found."
    lines = ["MCP servers:"]
    for entry in entries:
        name = entry.get("name") or "(unknown)"
        auth = entry.get("authStatus") or "unknown"
        tools = entry.get("tools")
        tool_names: list[str] = []
        if isinstance(tools, dict):
            tool_names = sorted(tools.keys())
        elif isinstance(tools, list):
            tool_names = [str(item) for item in tools]
        line = f"{name} ({auth})"
        if tool_names:
            line = f"{line} - tools: {', '.join(tool_names)}"
        lines.append(line)
    return "\n".join(lines)


def _format_help_text(command_specs: dict[str, CommandSpec]) -> str:
    order = [
        "bind",
        "new",
        "resume",
        "review",
        "model",
        "approvals",
        "status",
        "diff",
        "mention",
        "skills",
        "mcp",
        "experimental",
        "init",
        "compact",
        "rollout",
        "feedback",
        "logout",
        "interrupt",
        "quit",
        "exit",
        "help",
    ]
    lines = ["Commands:"]
    for name in order:
        spec = command_specs.get(name)
        if spec:
            lines.append(f"/{name} - {spec.description}")
    return "\n".join(lines)


def _render_command_output(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        stdout = result.get("stdout") or result.get("stdOut") or result.get("output")
        stderr = result.get("stderr") or result.get("stdErr")
        if isinstance(stdout, str) and isinstance(stderr, str):
            if stdout and stderr:
                return stdout.rstrip("\n") + "\n" + stderr
            if stdout:
                return stdout
            return stderr
        if isinstance(stdout, str):
            return stdout
        if isinstance(stderr, str):
            return stderr
    return ""


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data


def _find_thread_entry(payload: Any, thread_id: str) -> Optional[dict[str, Any]]:
    for entry in _coerce_thread_list(payload):
        if entry.get("id") == thread_id:
            return entry
    return None


def _extract_rollout_path(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("rollout_path", "rolloutPath", "path"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    thread = entry.get("thread")
    if isinstance(thread, dict):
        value = thread.get("path")
        if isinstance(value, str):
            return value
    return None


def _parse_command(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [part for part in shlex.split(raw) if part]
    return []


def _app_server_env(command: Sequence[str], cwd: Path) -> dict[str, str]:
    extra_paths: list[str] = []
    if command:
        binary = command[0]
        resolved = resolve_executable(binary)
        candidate: Optional[Path] = Path(resolved) if resolved else None
        if candidate is None:
            candidate = Path(binary).expanduser()
            if not candidate.is_absolute():
                candidate = (cwd / candidate).resolve()
        if candidate.exists():
            extra_paths.append(str(candidate.parent))
    return subprocess_env(extra_paths=extra_paths)


def _parse_int_list(raw: Any) -> list[int]:
    values: list[int] = []
    if raw is None:
        return values
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, str):
        parts = [part for part in re.split(r"[,\s]+", raw.strip()) if part]
        for part in parts:
            try:
                values.append(int(part))
            except ValueError:
                continue
        return values
    if isinstance(raw, Iterable):
        for item in raw:
            values.extend(_parse_int_list(item))
    return values


_THREAD_PATH_KEYS = (
    "cwd",
    "workspace",
    "workspace_path",
    "workspacePath",
    "projectRoot",
    "project_root",
    "repoPath",
    "repo_path",
    "root",
    "rootPath",
)
_THREAD_PATH_CONTAINERS = ("workspace", "project", "repo", "metadata", "context", "config")


def _coerce_thread_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _normalize_thread_entries(payload)
    if isinstance(payload, dict):
        for key in ("threads", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return _normalize_thread_entries(value)
            if isinstance(value, dict):
                return _normalize_thread_mapping(value)
        if any(key in payload for key in ("id", "threadId", "thread_id")):
            return _normalize_thread_entries([payload])
    return []


def _normalize_thread_entries(entries: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            item = dict(entry)
            if "id" not in item:
                for key in ("threadId", "thread_id"):
                    value = item.get(key)
                    if isinstance(value, str):
                        item["id"] = value
                        break
            normalized.append(item)
        elif isinstance(entry, str):
            normalized.append({"id": entry})
    return normalized


def _normalize_thread_mapping(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for key, value in mapping.items():
        if not isinstance(key, str):
            continue
        item = dict(value) if isinstance(value, dict) else {}
        item.setdefault("id", key)
        normalized.append(item)
    return normalized


def _extract_thread_path(entry: dict[str, Any]) -> Optional[str]:
    for key in _THREAD_PATH_KEYS:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    for container_key in _THREAD_PATH_CONTAINERS:
        nested = entry.get(container_key)
        if isinstance(nested, dict):
            for key in _THREAD_PATH_KEYS:
                value = nested.get(key)
                if isinstance(value, str):
                    return value
    return None


def _partition_threads(
    threads: Any, workspace_path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not isinstance(threads, list):
        return [], [], False
    workspace = Path(workspace_path).expanduser().resolve()
    filtered: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    saw_path = False
    for entry in threads:
        if not isinstance(entry, dict):
            continue
        cwd = _extract_thread_path(entry)
        if not isinstance(cwd, str):
            unscoped.append(entry)
            continue
        saw_path = True
        try:
            candidate = Path(cwd).expanduser().resolve()
        except Exception:
            continue
        if _path_within(workspace, candidate):
            filtered.append(entry)
    return filtered, unscoped, saw_path


def _filter_threads(
    threads: Any,
    workspace_path: str,
    *,
    assume_scoped: bool = False,
    allow_unscoped: bool = True,
) -> list[dict[str, Any]]:
    filtered, unscoped, saw_path = _partition_threads(threads, workspace_path)
    if filtered or saw_path or not assume_scoped:
        return filtered
    if allow_unscoped:
        return unscoped
    return []


def _path_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _compact_preview(text: Any, limit: int = 40) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview or "(no preview)"


def _compose_agent_response(messages: list[str]) -> str:
    cleaned = [msg.strip() for msg in messages if isinstance(msg, str) and msg.strip()]
    if not cleaned:
        return "(No agent response.)"
    return "\n\n".join(cleaned)


def _compose_interrupt_response(agent_text: str) -> str:
    base = "Interrupted."
    if agent_text and agent_text != "(No agent response.)":
        return f"{base}\n\n{agent_text}"
    return base


def _format_approval_prompt(message: dict[str, Any]) -> str:
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    lines = ["Approval required"]
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        if command:
            lines.append(f"Command: {command}")
    elif method == "item/fileChange/requestApproval":
        files = _extract_files(params)
        if files:
            if len(files) == 1:
                lines.append(f"File: {files[0]}")
            else:
                lines.append("Files:")
                lines.extend([f"- {path}" for path in files[:10]])
                if len(files) > 10:
                    lines.append("- ...")
    return "\n".join(lines)


def _format_approval_decision(decision: str) -> str:
    return f"Approval {decision}."


def _extract_files(params: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "fileChanges", "paths"):
        payload = params.get(key)
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, str) and entry:
                    files.append(entry)
                elif isinstance(entry, dict):
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    if isinstance(path, str) and path:
                        files.append(path)
    return files


def _normalize_parse_mode(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return PARSE_MODE_ALIASES.get(cleaned.lower(), cleaned)


def _format_telegram_html(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _CODE_BLOCK_RE.finditer(text):
        parts.append(_format_telegram_inline(text[last : match.start()]))
        code = match.group(1)
        parts.append("<pre><code>")
        parts.append(html.escape(code, quote=False))
        parts.append("</code></pre>")
        last = match.end()
    parts.append(_format_telegram_inline(text[last:]))
    return "".join(parts)


def _format_telegram_inline(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def _replace_code(match: re.Match[str]) -> str:
        placeholders.append(html.escape(match.group(1), quote=False))
        return f"\x00CODE{len(placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_replace_code, text)
    escaped = html.escape(text, quote=False)
    escaped = _BOLD_RE.sub(lambda match: f"<b>{match.group(1)}</b>", escaped)
    for idx, code in enumerate(placeholders):
        token = f"\x00CODE{idx}\x00"
        escaped = escaped.replace(token, f"<code>{code}</code>")
    return escaped


def _escape_markdown_text(text: str, *, version: str) -> str:
    if not text:
        return ""
    if version == "MarkdownV2":
        return _MARKDOWN_V2_ESCAPE_RE.sub(r"\\\1", text)
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


def _escape_markdown_code(text: str, *, version: str) -> str:
    if not text:
        return ""
    if version == "MarkdownV2":
        return text.replace("\\", "\\\\").replace("`", "\\`")
    return text.replace("`", "\\`")


def _format_telegram_markdown(text: str, version: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _CODE_BLOCK_RE.finditer(text):
        parts.append(_format_telegram_markdown_inline(text[last : match.start()], version))
        code = _escape_markdown_code(match.group(1), version=version)
        parts.append(f"```\n{code}\n```")
        last = match.end()
    parts.append(_format_telegram_markdown_inline(text[last:], version))
    return "".join(parts)


def _format_telegram_markdown_inline(text: str, version: str) -> str:
    if not text:
        return ""
    code_placeholders: list[str] = []
    bold_placeholders: list[str] = []

    def _replace_code(match: re.Match[str]) -> str:
        code_placeholders.append(_escape_markdown_code(match.group(1), version=version))
        return f"\x00CODE{len(code_placeholders) - 1}\x00"

    def _replace_bold(match: re.Match[str]) -> str:
        bold_placeholders.append(
            _escape_markdown_text(match.group(1), version=version)
        )
        return f"\x00BOLD{len(bold_placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_replace_code, text)
    text = _BOLD_RE.sub(_replace_bold, text)
    escaped = _escape_markdown_text(text, version=version)
    for idx, bold in enumerate(bold_placeholders):
        token = f"\x00BOLD{idx}\x00"
        escaped = escaped.replace(token, f"*{bold}*")
    for idx, code in enumerate(code_placeholders):
        token = f"\x00CODE{idx}\x00"
        escaped = escaped.replace(token, f"`{code}`")
    return escaped


def _split_topic_key(key: str) -> tuple[int, Optional[int]]:
    parts = key.split(":", 2)
    chat_raw = parts[0] if parts else ""
    thread_raw = parts[1] if len(parts) > 1 else ""
    chat_id = int(chat_raw)
    thread_id = None
    if thread_raw and thread_raw != "root":
        thread_id = int(thread_raw)
    return chat_id, thread_id


def _page_count(total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def _page_slice(
    items: Sequence[tuple[str, str]],
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    start = page * page_size
    end = start + page_size
    return list(items[start:end])


def _selection_contains(items: Sequence[tuple[str, str]], value: str) -> bool:
    return any(item_id == value for item_id, _ in items)


def _format_selection_prompt(base: str, page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return base
    trimmed = base.rstrip(".")
    return f"{trimmed} (page {page + 1}/{total_pages})."
