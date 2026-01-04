from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import html
import json
import logging
import os
import random
import re
import secrets
import shlex
import socket
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

import httpx

from .app_server_client import (
    ApprovalDecision,
    CodexAppServerClient,
    CodexAppServerDisconnected,
    CodexAppServerError,
)
from .logging_utils import log_event
from .lock_utils import process_alive
from .manifest import load_manifest
from .routes.system import _normalize_update_target, _spawn_update_process
from .telegram_adapter import (
    ApprovalCallback,
    BindCallback,
    CancelCallback,
    EffortCallback,
    UpdateCallback,
    UpdateConfirmCallback,
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
    build_update_confirm_keyboard,
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
    PendingVoiceRecord,
    PendingApprovalRecord,
    ThreadSummary,
    TelegramStateStore,
    TopicRouter,
    normalize_approval_mode,
    parse_topic_key,
    TOPIC_ROOT,
    topic_key,
)
from .utils import RepoNotFoundError, find_repo_root, resolve_executable, subprocess_env
from .voice import VoiceConfig, VoiceService, VoiceServiceError

DEFAULT_ALLOWED_UPDATES = ("message", "edited_message", "callback_query")
DEFAULT_POLL_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 10
THREAD_LIST_PAGE_LIMIT = 100
THREAD_LIST_MAX_PAGES = 5
DEFAULT_MODEL_LIST_LIMIT = 25
DEFAULT_MCP_LIST_LIMIT = 50
DEFAULT_SKILLS_LIST_LIMIT = 50
MAX_TOPIC_THREAD_HISTORY = 50
RESUME_BUTTON_PREVIEW_LIMIT = 60
RESUME_PREVIEW_USER_LIMIT = 1000
RESUME_PREVIEW_ASSISTANT_LIMIT = 1000
RESUME_PREVIEW_SCAN_LINES = 200
RESUME_MISSING_IDS_LOG_LIMIT = 10
RESUME_REFRESH_LIMIT = 10
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
VOICE_RETRY_INTERVAL_SECONDS = 5.0
VOICE_RETRY_INITIAL_SECONDS = 2.0
VOICE_RETRY_MAX_SECONDS = 300.0
VOICE_RETRY_JITTER_RATIO = 0.2
VOICE_MAX_ATTEMPTS = 20
VOICE_RETRY_AFTER_BUFFER_SECONDS = 1.0
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
DEFAULT_UPDATE_REPO_REF = "main"
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
TRACE_MESSAGE_TOKENS = (
    "failed",
    "error",
    "denied",
    "unknown",
    "not bound",
    "not found",
    "invalid",
    "unsupported",
    "disabled",
    "missing",
    "mismatch",
    "different workspace",
    "no previous",
    "no resumable",
    "no workspace-tagged",
    "not applicable",
    "selection expired",
    "timed out",
    "timeout",
    "aborted",
    "cancelled",
)
PLACEHOLDER_TEXT = "Working..."
STREAM_PREVIEW_PREFIX = ""
THINKING_PREVIEW_MAX_LEN = 80
THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS = 1.0
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


class TelegramBotLockError(Exception):
    """Raised when another telegram bot instance already holds the lock."""


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
    debug_prefix_context: bool
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
        debug_raw = cfg.get("debug") if isinstance(cfg.get("debug"), dict) else {}
        debug_prefix_context = bool(debug_raw.get("prefix_context", False))
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
            debug_prefix_context=debug_prefix_context,
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


TurnKey = tuple[str, str]


@dataclass
class PendingApproval:
    request_id: str
    turn_id: str
    codex_thread_id: Optional[str]
    chat_id: int
    thread_id: Optional[int]
    topic_key: Optional[str]
    message_id: Optional[int]
    created_at: str
    future: asyncio.Future[ApprovalDecision]


@dataclass
class TurnContext:
    topic_key: str
    chat_id: int
    thread_id: Optional[int]
    codex_thread_id: Optional[str]
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
    topic_key: str
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
        self._turn_contexts: dict[TurnKey, TurnContext] = {}
        self._reasoning_buffers: dict[str, str] = {}
        self._turn_preview_text: dict[TurnKey, str] = {}
        self._turn_preview_updated_at: dict[TurnKey, float] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._update_options: dict[str, SelectionState] = {}
        self._update_confirm_options: dict[str, bool] = {}
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
        self._voice_inflight: set[str] = set()
        self._voice_lock: Optional[asyncio.Lock] = None
        self._voice_task: Optional[asyncio.Task[None]] = None
        self._command_specs = self._build_command_specs()
        self._instance_lock_path: Optional[Path] = None

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
        except FileExistsError:
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
                )
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
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
                )
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
        self._outbox_inflight = set()
        self._outbox_lock = asyncio.Lock()
        self._voice_inflight = set()
        self._voice_lock = asyncio.Lock()
        try:
            await self._start_app_server_with_backoff()
            await self._prime_bot_identity()
            await self._restore_pending_approvals()
            await self._restore_outbox()
            await self._restore_pending_voice()
            self._prime_poller_offset()
            self._outbox_task = asyncio.create_task(self._outbox_loop())
            self._voice_task = asyncio.create_task(self._voice_loop())
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
                    self._spawn_task(self._dispatch_update(update))
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
                    await self._client.close()
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

    async def _restore_pending_voice(self) -> None:
        records = self._store.list_pending_voice()
        if not records:
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.voice.restore",
            count=len(records),
        )
        await self._flush_pending_voice(records)

    async def _voice_loop(self) -> None:
        while True:
            await asyncio.sleep(VOICE_RETRY_INTERVAL_SECONDS)
            try:
                records = self._store.list_pending_voice()
                if records:
                    await self._flush_pending_voice(records)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.voice.flush_failed",
                    exc=exc,
                )

    async def _flush_pending_voice(self, records: list[PendingVoiceRecord]) -> None:
        for record in records:
            if record.attempts >= VOICE_MAX_ATTEMPTS:
                await self._give_up_voice(
                    record,
                    "Voice transcription failed after retries. Please resend.",
                )
                continue
            await self._attempt_pending_voice(record.record_id)

    async def _attempt_pending_voice(self, record_id: str) -> bool:
        record = self._store.get_pending_voice(record_id)
        if record is None:
            return False
        if not self._voice_ready_for_attempt(record):
            return False
        if not await self._mark_voice_inflight(record.record_id):
            return False
        inflight_id = record.record_id
        try:
            record = self._store.get_pending_voice(record.record_id)
            if record is None:
                return False
            if not self._voice_ready_for_attempt(record):
                return False
            done = await self._process_pending_voice(record)
        except Exception as exc:
            retry_after = _extract_retry_after_seconds(exc)
            await self._record_voice_failure(record, exc, retry_after=retry_after)
            return False
        finally:
            await self._clear_voice_inflight(inflight_id)
        if done:
            self._store.delete_pending_voice(record.record_id)
        return done

    async def _process_pending_voice(self, record: PendingVoiceRecord) -> bool:
        if not self._voice_service or not self._voice_config or not self._voice_config.enabled:
            await self._send_message(
                record.chat_id,
                "Voice transcription is disabled.",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        max_bytes = self._config.media.max_voice_bytes
        if record.file_size and record.file_size > max_bytes:
            await self._send_message(
                record.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        if record.transcript_text:
            await self._deliver_voice_transcript(record, record.transcript_text)
            self._remove_voice_file(record)
            return True
        path = self._resolve_voice_download_path(record)
        if path is None:
            data, file_path, file_size = await self._download_telegram_file(
                record.file_id
            )
            if file_size and file_size > max_bytes:
                await self._send_message(
                    record.chat_id,
                    f"Voice note too large (max {max_bytes} bytes).",
                    thread_id=record.thread_id,
                    reply_to=record.message_id,
                )
                return True
            if len(data) > max_bytes:
                await self._send_message(
                    record.chat_id,
                    f"Voice note too large (max {max_bytes} bytes).",
                    thread_id=record.thread_id,
                    reply_to=record.message_id,
                )
                return True
            path = self._persist_voice_payload(record, data, file_path=file_path)
            record.download_path = str(path)
            if file_size is not None:
                record.file_size = file_size
            else:
                record.file_size = len(data)
            self._store.update_pending_voice(record)
        data = path.read_bytes()
        try:
            result = await asyncio.to_thread(
                self._voice_service.transcribe,
                data,
                client="telegram",
                filename=record.file_name or path.name,
                content_type=record.mime_type,
            )
        except VoiceServiceError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.voice.transcribe_failed",
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                message_id=record.message_id,
                reason=exc.reason,
            )
            await self._send_message(
                record.chat_id,
                exc.detail,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        transcript = ""
        if isinstance(result, dict):
            transcript = str(result.get("text") or "")
        transcript = transcript.strip()
        if not transcript:
            await self._send_message(
                record.chat_id,
                "Voice note transcribed to empty text.",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        combined = record.caption.strip()
        if combined:
            combined = f"{combined}\n\n{transcript}"
        else:
            combined = transcript
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.transcribed",
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            message_id=record.message_id,
            text_len=len(transcript),
        )
        record.transcript_text = combined
        self._store.update_pending_voice(record)
        await self._deliver_voice_transcript(record, combined)
        self._remove_voice_file(record)
        return True

    async def _deliver_voice_transcript(
        self,
        record: PendingVoiceRecord,
        transcript_text: str,
    ) -> None:
        if record.transcript_message_id is None:
            transcript_message = self._format_voice_transcript_message(
                transcript_text,
                PLACEHOLDER_TEXT,
            )
            record.transcript_message_id = await self._send_voice_transcript_message(
                record.chat_id,
                transcript_message,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._store.update_pending_voice(record)
        if record.transcript_message_id is None:
            raise RuntimeError("Failed to send voice transcript message")
        await self._update_voice_progress_message(record, "Voice note transcribed.")
        message = TelegramMessage(
            update_id=0,
            message_id=record.message_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            from_user_id=None,
            text=None,
            date=None,
            is_topic_message=record.thread_id is not None,
        )
        key = self._resolve_topic_key(record.chat_id, record.thread_id)
        runtime = self._router.runtime_for(key)
        if self._config.concurrency.per_topic_queue:
            await runtime.queue.enqueue(
                lambda: self._handle_normal_message(
                    message,
                    runtime,
                    text_override=transcript_text,
                    send_placeholder=True,
                    transcript_message_id=record.transcript_message_id,
                    transcript_text=transcript_text,
                )
            )
        else:
            await self._handle_normal_message(
                message,
                runtime,
                text_override=transcript_text,
                send_placeholder=True,
                transcript_message_id=record.transcript_message_id,
                transcript_text=transcript_text,
            )

    async def _record_voice_failure(
        self,
        record: PendingVoiceRecord,
        exc: Exception,
        *,
        retry_after: Optional[int],
    ) -> None:
        record.attempts += 1
        record.last_error = str(exc)[:500]
        record.last_attempt_at = now_iso()
        delay = self._voice_retry_delay(record.attempts, retry_after=retry_after)
        record.next_attempt_at = _format_future_time(delay)
        self._store.update_pending_voice(record)
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.voice.retry",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            attempts=record.attempts,
            retry_after=retry_after,
            next_attempt_at=record.next_attempt_at,
            exc=exc,
        )
        if record.attempts == 1 and record.progress_message_id is None:
            progress_id = await self._send_voice_progress_message(
                record,
                "Queued voice note, retrying download...",
            )
            if progress_id is not None:
                record.progress_message_id = progress_id
                self._store.update_pending_voice(record)
        if record.attempts >= VOICE_MAX_ATTEMPTS:
            await self._give_up_voice(
                record,
                "Voice transcription failed after retries. Please resend.",
            )

    async def _give_up_voice(self, record: PendingVoiceRecord, message: str) -> None:
        if record.progress_message_id is not None:
            await self._edit_message_text(
                record.chat_id,
                record.progress_message_id,
                message,
            )
        else:
            await self._send_message(
                record.chat_id,
                message,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
        self._remove_voice_file(record)
        self._store.delete_pending_voice(record.record_id)
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.voice.gave_up",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            attempts=record.attempts,
        )

    async def _send_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> Optional[int]:
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            reply_to=record.message_id,
            workspace_path=record.workspace_path,
        )
        try:
            response = await self._bot.send_message(
                record.chat_id,
                payload_text,
                message_thread_id=record.thread_id,
                reply_to_message_id=record.message_id,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.voice.progress_failed",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                exc=exc,
            )
            return None
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _update_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> None:
        if record.progress_message_id is None:
            return
        await self._edit_message_text(
            record.chat_id,
            record.progress_message_id,
            text,
        )

    async def _mark_voice_inflight(self, record_id: str) -> bool:
        if self._voice_lock is None:
            self._voice_lock = asyncio.Lock()
        async with self._voice_lock:
            if record_id in self._voice_inflight:
                return False
            self._voice_inflight.add(record_id)
            return True

    async def _clear_voice_inflight(self, record_id: str) -> None:
        if self._voice_lock is None:
            return
        async with self._voice_lock:
            self._voice_inflight.discard(record_id)

    def _voice_ready_for_attempt(self, record: PendingVoiceRecord) -> bool:
        next_attempt = _parse_iso_timestamp(record.next_attempt_at)
        if next_attempt is None:
            return True
        return datetime.now(timezone.utc) >= next_attempt

    def _voice_retry_delay(
        self, attempts: int, *, retry_after: Optional[int]
    ) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after) + VOICE_RETRY_AFTER_BUFFER_SECONDS
        delay = VOICE_RETRY_INITIAL_SECONDS * (2 ** max(attempts - 1, 0))
        delay = min(delay, VOICE_RETRY_MAX_SECONDS)
        jitter = delay * VOICE_RETRY_JITTER_RATIO
        if jitter:
            delay += random.uniform(0, jitter)
        return delay

    def _resolve_voice_download_path(
        self, record: PendingVoiceRecord
    ) -> Optional[Path]:
        if not record.download_path:
            return None
        path = Path(record.download_path)
        if path.exists():
            return path
        record.download_path = None
        self._store.update_pending_voice(record)
        return None

    def _persist_voice_payload(
        self,
        record: PendingVoiceRecord,
        data: bytes,
        *,
        file_path: Optional[str],
    ) -> Path:
        workspace_path = record.workspace_path or str(self._config.root)
        storage_dir = self._voice_storage_dir(workspace_path)
        storage_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(6)
        ext = self._choose_voice_extension(
            record.file_name,
            record.mime_type,
            file_path=file_path,
        )
        name = f"telegram-voice-{int(time.time())}-{token}{ext}"
        path = storage_dir / name
        path.write_bytes(data)
        return path

    def _voice_storage_dir(self, workspace_path: str) -> Path:
        return (
            Path(workspace_path)
            / ".codex-autorunner"
            / "uploads"
            / "telegram-voice"
        )

    def _choose_voice_extension(
        self,
        file_name: Optional[str],
        mime_type: Optional[str],
        *,
        file_path: Optional[str],
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                suffix = Path(candidate).suffix
                if suffix:
                    return suffix
        if mime_type == "audio/ogg":
            return ".ogg"
        if mime_type == "audio/opus":
            return ".opus"
        if mime_type == "audio/mpeg":
            return ".mp3"
        if mime_type == "audio/wav":
            return ".wav"
        return ".dat"

    def _remove_voice_file(self, record: PendingVoiceRecord) -> None:
        if not record.download_path:
            return
        path = Path(record.download_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.voice.cleanup_failed",
                record_id=record.record_id,
                path=str(path),
            )

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
        if normalized_path:
            try:
                normalized_path = str(Path(normalized_path).expanduser().resolve())
            except Exception:
                pass
        if normalized_repo and normalized_path:
            return f"{normalized_repo}@{normalized_path}"
        if normalized_repo:
            return normalized_repo
        if normalized_path:
            return normalized_path
        return None

    def _turn_key(
        self, thread_id: Optional[str], turn_id: Optional[str]
    ) -> Optional[TurnKey]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        return (thread_id, turn_id)

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnKey]:
        if not isinstance(turn_id, str) or not turn_id:
            return None
        key: Optional[TurnKey] = None
        if thread_id is not None:
            if not isinstance(thread_id, str) or not thread_id:
                return None
            key = (thread_id, turn_id)
            if self._turn_contexts.get(key) is not None:
                return key
        matches = [
            candidate_key
            for candidate_key in self._turn_contexts
            if candidate_key[1] == turn_id
        ]
        if len(matches) == 1:
            candidate = matches[0]
            if key is not None and candidate != key:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.turn.thread_mismatch",
                    turn_id=turn_id,
                    requested_thread_id=thread_id,
                    actual_thread_id=candidate[0],
                )
            return candidate
        if len(matches) > 1:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.ambiguous",
                turn_id=turn_id,
                matches=len(matches),
            )
        return None

    def _resolve_turn_context(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnContext]:
        key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if key is None:
            return None
        return self._turn_contexts.get(key)

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
        key = None
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user_id
            thread_id = update.message.thread_id
            message_id = update.message.message_id
            is_topic = update.message.is_topic_message
            is_edited = update.message.is_edited
            key = self._resolve_topic_key(chat_id, thread_id)
        elif update.callback:
            chat_id = update.callback.chat_id
            user_id = update.callback.from_user_id
            thread_id = update.callback.thread_id
            message_id = update.callback.message_id
            if chat_id is not None:
                key = self._resolve_topic_key(chat_id, thread_id)
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
        if (
            update.update_id is not None
            and key
            and not self._should_process_update(key, update.update_id)
        ):
            log_event(
                self._logger,
                logging.INFO,
                "telegram.update.duplicate",
                update_id=update.update_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
            )
            return
        if not allowlist_allows(update, self._allowlist):
            self._log_denied(update)
            return
        if update.callback:
            if key:
                self._enqueue_topic_work(
                    key,
                    lambda: self._handle_callback(update.callback),
                    force_queue=True,
                )
                return
            await self._handle_callback(update.callback)
            return
        if update.message:
            if key:
                if self._should_bypass_topic_queue(update.message):
                    await self._handle_message(update.message)
                    return
                self._enqueue_topic_work(
                    key,
                    lambda: self._handle_message(update.message),
                    force_queue=True,
                )
                return
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

    def _should_bypass_topic_queue(self, message: TelegramMessage) -> bool:
        raw_text = message.text or ""
        raw_caption = message.caption or ""
        text_candidate = raw_text if raw_text.strip() else raw_caption
        if not text_candidate:
            return False
        trimmed_text = text_candidate.strip()
        if not trimmed_text:
            return False
        if is_interrupt_alias(trimmed_text):
            return True
        entities = message.entities if raw_text.strip() else message.caption_entities
        command = parse_command(
            text_candidate, entities=entities, bot_username=self._bot_username
        )
        if not command:
            return False
        spec = self._command_specs.get(command.name)
        return bool(spec and spec.allow_during_turn)

    async def _handle_edited_message(self, message: TelegramMessage) -> None:
        text = (message.text or "").strip()
        if not text:
            text = (message.caption or "").strip()
        if not text:
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        runtime = self._router.runtime_for(key)
        turn_key = runtime.current_turn_key
        if not turn_key:
            return
        ctx = self._turn_contexts.get(turn_key)
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

    async def _handle_message_inner(
        self, message: TelegramMessage, *, topic_key: Optional[str] = None
    ) -> None:
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
        key = (
            topic_key
            if isinstance(topic_key, str) and topic_key
            else self._resolve_topic_key(message.chat_id, message.thread_id)
        )
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

    def _coalesce_key_for_topic(self, key: str, user_id: Optional[int]) -> str:
        if user_id is None:
            return f"{key}:user:unknown"
        return f"{key}:user:{user_id}"

    def _coalesce_key(self, message: TelegramMessage) -> str:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        return self._coalesce_key_for_topic(key, message.from_user_id)

    async def _buffer_coalesced_message(self, message: TelegramMessage, text: str) -> None:
        topic_key = self._resolve_topic_key(message.chat_id, message.thread_id)
        key = self._coalesce_key_for_topic(topic_key, message.from_user_id)
        lock = self._coalesce_locks.setdefault(key, asyncio.Lock())
        async with lock:
            buffer = self._coalesced_buffers.get(key)
            if buffer is None:
                buffer = _CoalescedBuffer(
                    message=message, parts=[text], topic_key=topic_key
                )
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
        await self._handle_message_inner(combined_message, topic_key=buffer.topic_key)

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
                _with_conversation_id(
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
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
        elif isinstance(parsed, UpdateConfirmCallback):
            if key:
                await self._handle_update_confirm_callback(key, callback, parsed)
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

    async def _handle_update_confirm_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: UpdateConfirmCallback,
    ) -> None:
        if not self._update_confirm_options.get(key):
            await self._answer_callback(callback, "Selection expired")
            return
        self._update_confirm_options.pop(key, None)
        if parsed.decision != "yes":
            await self._answer_callback(callback, "Cancelled")
            await self._finalize_selection(key, callback, "Update cancelled.")
            return
        await self._prompt_update_selection_from_callback(key, callback)
        await self._answer_callback(callback, "Select update target")

    def _enqueue_topic_work(
        self, key: str, work: Any, *, force_queue: bool = False
    ) -> None:
        runtime = self._router.runtime_for(key)
        if force_queue or self._config.concurrency.per_topic_queue:
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
            "debug": CommandSpec(
                "debug",
                "show topic debug info",
                self._handle_debug,
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
                "Active thread missing workspace metadata; starting a new thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        try:
            workspace_root = Path(record.workspace_path or "").expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Active thread has invalid workspace metadata; starting a new thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        if not _paths_compatible(workspace_root, resumed_root):
            log_event(
                self._logger,
                logging.INFO,
                "telegram.thread.workspace_mismatch",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                workspace_path=str(workspace_root),
                resumed_path=str(resumed_root),
            )
            return self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        return self._apply_thread_result(
            message.chat_id, message.thread_id, result, active_thread_id=thread_id
        )

    def _find_thread_conflict(self, thread_id: str, *, key: str) -> Optional[str]:
        return self._store.find_active_thread(thread_id, exclude_key=key)

    async def _handle_thread_conflict(
        self,
        message: TelegramMessage,
        thread_id: str,
        conflict_key: str,
    ) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.thread.conflict",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            conflict_topic=conflict_key,
        )
        await self._send_message(
            message.chat_id,
            "That Codex thread is already active in another topic. "
            "Use /new here or continue in the other topic.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
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
        user_preview, assistant_preview = _extract_thread_preview_parts(result)
        last_used_at = now_iso()

        def apply(record: "TelegramTopicRecord") -> None:
            if active_thread_id:
                record.active_thread_id = active_thread_id
                if active_thread_id in record.thread_ids:
                    record.thread_ids.remove(active_thread_id)
                record.thread_ids.insert(0, active_thread_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    active_thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=info.get("workspace_path"),
                    rollout_path=info.get("rollout_path"),
                )
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
            key = self._resolve_topic_key(message.chat_id, message.thread_id)
            conflict_key = self._find_thread_conflict(thread_id, key=key)
            if conflict_key:
                self._router.set_active_thread(message.chat_id, message.thread_id, None)
                await self._handle_thread_conflict(message, thread_id, conflict_key)
                return None
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return None
            record = verified
            thread_id = record.active_thread_id
            if thread_id:
                return thread_id
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
            conflict_key = self._find_thread_conflict(
                record.active_thread_id,
                key=key,
            )
            if conflict_key:
                self._router.set_active_thread(message.chat_id, message.thread_id, None)
                await self._handle_thread_conflict(
                    message,
                    record.active_thread_id,
                    conflict_key,
                )
                return
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return
            record = verified
        thread_id = record.active_thread_id
        turn_handle = None
        turn_key: Optional[TurnKey] = None
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
            if thread_id:
                user_preview = _preview_from_text(
                    prompt_text, RESUME_PREVIEW_USER_LIMIT
                )
                self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        user_preview=user_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
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
                turn_key = self._turn_key(thread_id, turn_handle.turn_id)
                runtime.current_turn_id = turn_handle.turn_id
                runtime.current_turn_key = turn_key
                ctx = TurnContext(
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                if turn_key is None or not self._register_turn_context(
                    turn_key, turn_handle.turn_id, ctx
                ):
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                    runtime.interrupt_requested = False
                    await self._send_message(
                        message.chat_id,
                        "Turn collision detected; please retry.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    if placeholder_id is not None:
                        await self._delete_message(message.chat_id, placeholder_id)
                    return
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex turn failed; check logs for details."
            if isinstance(exc, CodexAppServerDisconnected):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.disconnected_during_turn",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    turn_id=turn_handle.turn_id if turn_handle else None,
                )
                failure_message = (
                    "Codex app-server disconnected; retrying. "
                    "Please resend your message."
                )
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
                    failure_message,
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
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False

        response = _compose_agent_response(result.agent_messages)
        if thread_id and response and response != "(No agent response.)":
            assistant_preview = _preview_from_text(
                response, RESUME_PREVIEW_ASSISTANT_LIMIT
            )
            if assistant_preview:
                self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        assistant_preview=assistant_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
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
        pending = PendingVoiceRecord(
            record_id=secrets.token_hex(8),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_name=candidate.file_name,
            caption=caption_text,
            file_size=candidate.file_size,
            mime_type=candidate.mime_type,
            duration=candidate.duration,
            workspace_path=record.workspace_path,
            created_at=now_iso(),
        )
        self._store.enqueue_pending_voice(pending)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.queued",
            record_id=pending.record_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
        )
        self._spawn_task(self._attempt_pending_voice(pending.record_id))

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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        pending_request_ids = [
            request_id
            for request_id, pending in self._pending_approvals.items()
            if (pending.topic_key == key)
            or (
                pending.topic_key is None
                and pending.chat_id == message.chat_id
                and pending.thread_id == message.thread_id
            )
        ]
        for request_id in pending_request_ids:
            pending = self._pending_approvals.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result("cancel")
            self._store.clear_pending_approval(request_id)
        if pending_request_ids:
            runtime.pending_request_id = None
        if not turn_id:
            pending = self._store.pending_approvals_for_key(key)
            if pending:
                self._store.clear_pending_approvals_for_key(key)
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
        payload_text, parse_mode = self._prepare_outgoing_text(
            "Interrupt requested.",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
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
        refresh = False
        remaining: list[str] = []
        for arg in argv:
            lowered = arg.lower()
            if lowered in ("--all", "all", "--unscoped", "unscoped"):
                show_unscoped = True
                continue
            if lowered in ("--refresh", "refresh"):
                refresh = True
                continue
            remaining.append(arg)
        if argv:
            trimmed = " ".join(remaining).strip()
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
            if remaining and remaining[0].lower() in ("list", "ls"):
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
        if not show_unscoped and not record.thread_ids:
            await self._send_message(
                message.chat_id,
                "No previous threads found for this topic. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        threads: list[dict[str, Any]] = []
        entries_by_id: dict[str, dict[str, Any]] = {}
        list_failed = False
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        if show_unscoped:
            state = self._store.load()
            local_thread_ids, local_previews, local_thread_topics = _local_workspace_threads(
                state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        if show_unscoped or record.thread_ids:
            limit = _resume_thread_list_limit(record.thread_ids)
            needed_ids = None if show_unscoped else set(record.thread_ids)
            try:
                threads, _ = await self._list_threads_paginated(
                    limit=limit,
                    max_pages=THREAD_LIST_MAX_PAGES,
                    needed_ids=needed_ids,
                )
            except Exception as exc:
                list_failed = True
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                if show_unscoped and not local_thread_ids:
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
        entries_by_id = {
            entry.get("id"): entry
            for entry in threads
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        candidates: list[dict[str, Any]] = []
        unscoped: list[dict[str, Any]] = []
        saw_path = False
        if show_unscoped:
            if threads:
                filtered, unscoped, saw_path = _partition_threads(
                    threads, record.workspace_path
                )
                seen_ids = {
                    entry.get("id")
                    for entry in filtered
                    if isinstance(entry.get("id"), str)
                }
                candidates = filtered + [
                    entry for entry in unscoped if entry.get("id") not in seen_ids
                ]
            if not candidates and not local_thread_ids:
                if unscoped and not saw_path:
                    await self._send_message(
                        message.chat_id,
                        _with_conversation_id(
                            "No workspace-tagged threads available. Use /resume --all to list "
                            "unscoped threads.",
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                        ),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "No previous threads found for this workspace. "
                        "If threads exist, update the app-server to include cwd metadata or use /new.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        missing_ids: list[str] = []
        if show_unscoped:
            for thread_id in local_thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        else:
            for thread_id in record.thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        if refresh and missing_ids:
            refreshed = await self._refresh_thread_summaries(
                missing_ids,
                topic_keys_by_thread=local_thread_topics if show_unscoped else None,
                default_topic_key=key,
            )
            if refreshed:
                if show_unscoped:
                    state = self._store.load()
                    local_thread_ids, local_previews, local_thread_topics = _local_workspace_threads(
                        state, record.workspace_path, current_key=key
                    )
                    for thread_id in record.thread_ids:
                        local_thread_topics.setdefault(thread_id, set()).add(key)
                        if thread_id not in local_thread_ids:
                            local_thread_ids.append(thread_id)
                        cached_preview = _thread_summary_preview(record, thread_id)
                        if cached_preview:
                            local_previews.setdefault(thread_id, cached_preview)
                else:
                    record = self._router.get_topic(key) or record
        items: list[tuple[str, str]] = []
        seen_item_ids: set[str] = set()
        if show_unscoped:
            for entry in candidates:
                thread_id = entry.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                if thread_id in seen_item_ids:
                    continue
                seen_item_ids.add(thread_id)
                label = _format_thread_preview(entry)
                if label == "(no preview)":
                    cached_preview = local_previews.get(thread_id)
                    if cached_preview:
                        label = cached_preview
                items.append((thread_id, label))
            for thread_id in local_thread_ids:
                if thread_id in seen_item_ids:
                    continue
                seen_item_ids.add(thread_id)
                cached_preview = local_previews.get(thread_id)
                label = (
                    cached_preview
                    if cached_preview
                    else _format_missing_thread_label(thread_id, None)
                )
                items.append((thread_id, label))
        else:
            for thread_id in record.thread_ids:
                entry = entries_by_id.get(thread_id)
                if entry is None:
                    cached_preview = _thread_summary_preview(record, thread_id)
                    label = _format_missing_thread_label(thread_id, cached_preview)
                else:
                    label = _format_thread_preview(entry)
                    if label == "(no preview)":
                        cached_preview = _thread_summary_preview(record, thread_id)
                        if cached_preview:
                            label = cached_preview
                items.append((thread_id, label))
        if missing_ids:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.resume.missing_thread_metadata",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                stored_count=len(record.thread_ids),
                listed_count=len(entries_by_id) if not show_unscoped else len(threads),
                missing_ids=missing_ids[:RESUME_MISSING_IDS_LOG_LIMIT],
                list_failed=list_failed,
            )
        if not items:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No resumable threads found.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
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

    async def _refresh_thread_summaries(
        self,
        thread_ids: Sequence[str],
        *,
        topic_keys_by_thread: Optional[dict[str, set[str]]] = None,
        default_topic_key: Optional[str] = None,
    ) -> set[str]:
        refreshed: set[str] = set()
        if not thread_ids:
            return refreshed
        unique_ids: list[str] = []
        seen: set[str] = set()
        for thread_id in thread_ids:
            if not isinstance(thread_id, str) or not thread_id:
                continue
            if thread_id in seen:
                continue
            seen.add(thread_id)
            unique_ids.append(thread_id)
            if len(unique_ids) >= RESUME_REFRESH_LIMIT:
                break
        for thread_id in unique_ids:
            try:
                result = await self._client.thread_resume(thread_id)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.refresh_failed",
                    thread_id=thread_id,
                    exc=exc,
                )
                continue
            user_preview, assistant_preview = _extract_thread_preview_parts(result)
            info = _extract_thread_info(result)
            workspace_path = info.get("workspace_path")
            rollout_path = info.get("rollout_path")
            if (
                user_preview is None
                and assistant_preview is None
                and workspace_path is None
                and rollout_path is None
            ):
                continue
            last_used_at = now_iso() if user_preview or assistant_preview else None

            def apply(record: "TelegramTopicRecord") -> None:
                _set_thread_summary(
                    record,
                    thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=workspace_path,
                    rollout_path=rollout_path,
                )

            keys = (
                topic_keys_by_thread.get(thread_id)
                if topic_keys_by_thread is not None
                else None
            )
            if keys:
                for key in keys:
                    self._store.update_topic(key, apply)
            elif default_topic_key:
                self._store.update_topic(default_topic_key, apply)
            else:
                continue
            refreshed.add(thread_id)
        return refreshed

    async def _list_threads_paginated(
        self,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entries: list[dict[str, Any]] = []
        found_ids: set[str] = set()
        seen_ids: set[str] = set()
        cursor: Optional[str] = None
        page_count = max(1, max_pages)
        for _ in range(page_count):
            payload = await self._client.thread_list(cursor=cursor, limit=limit)
            page_entries = _coerce_thread_list(payload)
            for entry in page_entries:
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if isinstance(thread_id, str):
                    if thread_id in seen_ids:
                        continue
                    seen_ids.add(thread_id)
                    found_ids.add(thread_id)
                entries.append(entry)
            if needed_ids is not None and needed_ids.issubset(found_ids):
                break
            cursor = _extract_thread_list_cursor(payload)
            if not cursor:
                break
        return entries, found_ids

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        chat_id, thread_id_val = _split_topic_key(key)
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
        result_preview = _format_thread_preview(result)
        if result_preview != "(no preview)":
            preview = result_preview
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not isinstance(resumed_path, str):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread metadata missing workspace path; resume aborted to avoid cross-worktree mixups.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
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
                _with_conversation_id(
                    "Thread workspace path is invalid; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not _paths_compatible(workspace_root, resumed_root):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread belongs to a different workspace; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        conflict_key = self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return
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
            message = f"{message}\nLast:\n{preview}"
        await self._finalize_selection(key, callback, message)

    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.ensure_topic(message.chat_id, message.thread_id)
        if runtime is None:
            runtime = self._router.runtime_for(key)
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
        pending = self._store.pending_approvals_for_key(key)
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

    async def _handle_debug(
        self, message: TelegramMessage, _args: str = "", _runtime: Optional[Any] = None
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        scope = None
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
            base_key = topic_key(chat_id, thread_id)
        except ValueError:
            base_key = key
        lines = [
            f"Topic key: {key}",
            f"Base key: {base_key}",
            f"Scope: {scope or 'none'}",
        ]
        if record is None:
            lines.append("Record: missing")
            await self._send_message(
                message.chat_id,
                "\n".join(lines),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path = record.workspace_path or "unbound"
        canonical_path = "unbound"
        if record.workspace_path:
            try:
                canonical_path = str(Path(record.workspace_path).expanduser().resolve())
            except Exception:
                canonical_path = "invalid"
        lines.extend(
            [
                f"Workspace: {workspace_path}",
                f"Workspace (canonical): {canonical_path}",
                f"Active thread: {record.active_thread_id or 'none'}",
                f"Thread IDs: {len(record.thread_ids)}",
                f"Cached summaries: {len(record.thread_summaries)}",
            ]
        )
        preview_ids = record.thread_ids[:3]
        if preview_ids:
            lines.append("Preview samples:")
            for thread_id in preview_ids:
                preview = _thread_summary_preview(record, thread_id)
                label = preview or "(no cached preview)"
                lines.append(f"{thread_id}: {_compact_preview(label, 120)}")
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
        if record.workspace_path:
            review_kwargs["cwd"] = record.workspace_path
        turn_handle = None
        turn_key: Optional[TurnKey] = None
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
                turn_key = self._turn_key(thread_id, turn_handle.turn_id)
                runtime.current_turn_id = turn_handle.turn_id
                runtime.current_turn_key = turn_key
                ctx = TurnContext(
                    topic_key=self._resolve_topic_key(
                        message.chat_id, message.thread_id
                    ),
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                if turn_key is None or not self._register_turn_context(
                    turn_key, turn_handle.turn_id, ctx
                ):
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                    runtime.interrupt_requested = False
                    await self._send_message(
                        message.chat_id,
                        "Turn collision detected; please retry.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    if placeholder_id is not None:
                        await self._delete_message(message.chat_id, placeholder_id)
                    return
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex review failed; check logs for details."
            if isinstance(exc, CodexAppServerDisconnected):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.disconnected_during_review",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    turn_id=turn_handle.turn_id if turn_handle else None,
                )
                failure_message = (
                    "Codex app-server disconnected; retrying. "
                    "Please resend your review command."
                )
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
                    failure_message,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
            return
        finally:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
        response = _compose_agent_response(result.agent_messages)
        if thread_id and response and response != "(No agent response.)":
            assistant_preview = _preview_from_text(
                response, RESUME_PREVIEW_ASSISTANT_LIMIT
            )
            if assistant_preview:
                self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        assistant_preview=assistant_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
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
        rollout_path = None
        try:
            result = await self._client.thread_resume(record.active_thread_id)
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
        rollout_path = _extract_thread_info(result).get("rollout_path")
        if not rollout_path:
            try:
                threads, _ = await self._list_threads_paginated(
                    limit=THREAD_LIST_PAGE_LIMIT,
                    max_pages=THREAD_LIST_MAX_PAGES,
                    needed_ids={record.active_thread_id},
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
            "Rollout path not available.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
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
        repo_ref = (self._update_repo_ref or DEFAULT_UPDATE_REPO_REF).strip()
        if not repo_ref:
            repo_ref = DEFAULT_UPDATE_REPO_REF
        update_dir = Path.home() / ".codex-autorunner" / "update_cache"
        try:
            _spawn_update_process(
                repo_url=repo_url,
                repo_ref=repo_ref,
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
                repo_ref=repo_ref,
                update_target=update_target,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                repo_ref=repo_ref,
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

    async def _prompt_update_selection_from_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        *,
        prompt: str = UPDATE_PICKER_PROMPT,
    ) -> None:
        state = SelectionState(items=list(UPDATE_TARGET_OPTIONS))
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        await self._update_selection_message(key, callback, prompt, keyboard)

    def _has_active_turns(self) -> bool:
        return bool(self._turn_contexts)

    async def _prompt_update_confirmation(self, message: TelegramMessage) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        self._update_confirm_options[key] = True
        await self._send_message(
            message.chat_id,
            "An active Codex turn is running. Updating will restart the service. Continue?",
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=build_update_confirm_keyboard(),
        )

    async def _handle_update(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        target_raw = argv[0] if argv else None
        if not target_raw:
            if self._has_active_turns():
                await self._prompt_update_confirmation(message)
            else:
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
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = _coerce_id(params.get("turnId"))
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
            return
        if method == "item/reasoning/summaryTextDelta":
            item_id = _coerce_id(params.get("itemId"))
            turn_id = _coerce_id(params.get("turnId"))
            thread_id = _extract_turn_thread_id(params)
            delta = params.get("delta")
            if not item_id or not turn_id or not isinstance(delta, str):
                return
            buffer = self._reasoning_buffers.get(item_id, "")
            buffer = f"{buffer}{delta}"
            self._reasoning_buffers[item_id] = buffer
            preview = _extract_first_bold_span(buffer)
            if preview:
                await self._update_placeholder_preview(
                    turn_id, preview, thread_id=thread_id
                )
            return
        if method == "item/reasoning/summaryPartAdded":
            item_id = _coerce_id(params.get("itemId"))
            if not item_id:
                return
            buffer = self._reasoning_buffers.get(item_id, "")
            buffer = f"{buffer}\n\n"
            self._reasoning_buffers[item_id] = buffer
            return
        if method == "item/completed":
            item = params.get("item") if isinstance(params, dict) else None
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                return
            item_id = _coerce_id(item.get("id") or params.get("itemId"))
            if item_id:
                self._reasoning_buffers.pop(item_id, None)
            return

    async def _update_placeholder_preview(
        self, turn_id: str, preview: str, *, thread_id: Optional[str] = None
    ) -> None:
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        normalized = " ".join(preview.split()).strip()
        if not normalized:
            return
        normalized = _truncate_text(normalized, THINKING_PREVIEW_MAX_LEN)
        if normalized == self._turn_preview_text.get(turn_key):
            return
        now = time.monotonic()
        last_updated = self._turn_preview_updated_at.get(turn_key, 0.0)
        if (now - last_updated) < THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS:
            return
        self._turn_preview_text[turn_key] = normalized
        self._turn_preview_updated_at[turn_key] = now
        if STREAM_PREVIEW_PREFIX:
            message_text = f"{STREAM_PREVIEW_PREFIX} {normalized}"
        else:
            message_text = normalized
        await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            message_text,
        )

    def _register_turn_context(
        self, turn_key: TurnKey, turn_id: str, ctx: TurnContext
    ) -> bool:
        existing = self._turn_contexts.get(turn_key)
        if existing and existing.topic_key != ctx.topic_key:
            log_event(
                self._logger,
                logging.ERROR,
                "telegram.turn.context.collision",
                turn_id=turn_id,
                existing_topic=existing.topic_key,
                new_topic=ctx.topic_key,
            )
            return False
        self._turn_contexts[turn_key] = ctx
        return True

    def _clear_thinking_preview(self, turn_key: TurnKey) -> None:
        self._turn_preview_text.pop(turn_key, None)
        self._turn_preview_updated_at.pop(turn_key, None)

    async def _handle_approval_request(self, message: dict[str, Any]) -> ApprovalDecision:
        req_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        turn_id = _coerce_id(params.get("turnId")) if isinstance(params, dict) else None
        if not req_id or not turn_id:
            return "cancel"
        codex_thread_id = _extract_turn_thread_id(params)
        ctx = self._resolve_turn_context(turn_id, thread_id=codex_thread_id)
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
            topic_key=ctx.topic_key,
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
        payload_text, parse_mode = self._prepare_outgoing_text(
            prompt,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            reply_to=ctx.reply_to_message_id,
            topic_key=ctx.topic_key,
            codex_thread_id=codex_thread_id,
        )
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
            codex_thread_id=codex_thread_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            topic_key=ctx.topic_key,
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
        ctx = self._resolve_turn_context(
            pending.turn_id, thread_id=pending.codex_thread_id
        )
        if ctx:
            runtime_key = ctx.topic_key
        elif pending.topic_key:
            runtime_key = pending.topic_key
        else:
            runtime_key = self._resolve_topic_key(pending.chat_id, pending.thread_id)
        runtime = self._router.runtime_for(runtime_key)
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
            (item_id, f"{idx}) {_compact_preview(label, RESUME_BUTTON_PREVIEW_LIMIT)}")
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

    def _build_debug_prefix(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int] = None,
        topic_key: Optional[str] = None,
        workspace_path: Optional[str] = None,
        codex_thread_id: Optional[str] = None,
    ) -> str:
        if not self._config.debug_prefix_context:
            return ""
        resolved_key = topic_key
        if not resolved_key:
            try:
                resolved_key = self._resolve_topic_key(chat_id, thread_id)
            except Exception:
                resolved_key = None
        scope = None
        if resolved_key:
            try:
                _, _, scope = parse_topic_key(resolved_key)
            except Exception:
                scope = None
        record = None
        if workspace_path is None or codex_thread_id is None:
            record = self._router.get_topic(resolved_key) if resolved_key else None
        if workspace_path is None and record is not None:
            workspace_path = record.workspace_path
        if codex_thread_id is None and record is not None:
            codex_thread_id = record.active_thread_id
        parts = [f"chat={chat_id}"]
        thread_label = str(thread_id) if thread_id is not None else TOPIC_ROOT
        parts.append(f"thread={thread_label}")
        if scope:
            parts.append(f"scope={scope}")
        if workspace_path:
            parts.append(f"cwd={workspace_path}")
        if codex_thread_id:
            parts.append(f"codex={codex_thread_id}")
        if reply_to is not None:
            parts.append(f"reply_to={reply_to}")
        return f"[{' '.join(parts)}] "

    def _prepare_outgoing_text(
        self,
        text: str,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int] = None,
        topic_key: Optional[str] = None,
        workspace_path: Optional[str] = None,
        codex_thread_id: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        prefix = self._build_debug_prefix(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
            topic_key=topic_key,
            workspace_path=workspace_path,
            codex_thread_id=codex_thread_id,
        )
        if prefix:
            text = f"{prefix}{text}"
        return self._prepare_message(text)

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
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
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
            payload_text, parse_mode = self._prepare_outgoing_text(
                PLACEHOLDER_TEXT,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to=reply_to,
            )
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
        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            if callback and await self._edit_callback_message(
                callback,
                "Selection complete.",
                reply_markup={"inline_keyboard": []},
            ):
                chat_id, thread_id = _split_topic_key(key)
                await self._send_message(chat_id, text, thread_id=thread_id)
                return
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
        elif parsed.kind == "update-confirm":
            self._update_confirm_options.pop(key, None)
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
        if _should_trace_message(text):
            text = _with_conversation_id(
                text,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
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


def _set_thread_summary(
    record: "TelegramTopicRecord",
    thread_id: str,
    *,
    user_preview: Optional[str] = None,
    assistant_preview: Optional[str] = None,
    last_used_at: Optional[str] = None,
    workspace_path: Optional[str] = None,
    rollout_path: Optional[str] = None,
) -> None:
    if not isinstance(thread_id, str) or not thread_id:
        return
    summary = record.thread_summaries.get(thread_id)
    if summary is None:
        summary = ThreadSummary()
    if user_preview is not None:
        summary.user_preview = user_preview
    if assistant_preview is not None:
        summary.assistant_preview = assistant_preview
    if last_used_at is not None:
        summary.last_used_at = last_used_at
    if workspace_path is not None:
        summary.workspace_path = workspace_path
    if rollout_path is not None:
        summary.rollout_path = rollout_path
    record.thread_summaries[thread_id] = summary
    if record.thread_ids:
        keep = set(record.thread_ids)
        for key in list(record.thread_summaries.keys()):
            if key not in keep:
                record.thread_summaries.pop(key, None)


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


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _extract_turn_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        if not isinstance(candidate, dict):
            continue
        for key in ("threadId", "thread_id"):
            thread_id = _coerce_id(candidate.get(key))
            if thread_id:
                return thread_id
        thread = candidate.get("thread")
        if isinstance(thread, dict):
            thread_id = _coerce_id(
                thread.get("id")
                or thread.get("threadId")
                or thread.get("thread_id")
            )
            if thread_id:
                return thread_id
    return None


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


def _format_future_time(delay_seconds: float) -> Optional[str]:
    if delay_seconds <= 0:
        return None
    dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_retry_after_seconds(exc: Exception) -> Optional[int]:
    current: Optional[BaseException] = exc
    while current is not None:
        if isinstance(current, httpx.HTTPStatusError):
            header = current.response.headers.get("Retry-After")
            if header and header.isdigit():
                return int(header)
            try:
                payload = current.response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                parameters = payload.get("parameters")
                if isinstance(parameters, dict):
                    retry_after = parameters.get("retry_after")
                    if isinstance(retry_after, int):
                        return retry_after
            message = str(payload.get("description")) if isinstance(payload, dict) else ""
            match = re.search(r"retry after (\d+)", message.lower())
            if match:
                return int(match.group(1))
        message = str(current)
        match = re.search(r"retry after (\d+)", message.lower())
        if match:
            return int(match.group(1))
        current = current.__cause__ or current.__context__
    return None


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


_THREAD_PATH_KEYS_PRIMARY = (
    "cwd",
    "workspace_path",
    "workspacePath",
    "repoPath",
    "repo_path",
    "projectRoot",
    "project_root",
)
_THREAD_PATH_CONTAINERS = ("workspace", "project", "repo", "metadata", "context", "config")
_THREAD_LIST_CURSOR_KEYS = ("nextCursor", "next_cursor", "next")


def _extract_thread_list_cursor(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in _THREAD_LIST_CURSOR_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return None


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
    for key in _THREAD_PATH_KEYS_PRIMARY:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    for container_key in _THREAD_PATH_CONTAINERS:
        nested = entry.get(container_key)
        if isinstance(nested, dict):
            for key in _THREAD_PATH_KEYS_PRIMARY:
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
        if _paths_compatible(workspace, candidate):
            filtered.append(entry)
    return filtered, unscoped, saw_path


def _local_workspace_threads(
    state: "TelegramState",
    workspace_path: Optional[str],
    *,
    current_key: str,
) -> tuple[list[str], dict[str, str], dict[str, set[str]]]:
    thread_ids: list[str] = []
    previews: dict[str, str] = {}
    topic_keys_by_thread: dict[str, set[str]] = {}
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return thread_ids, previews, topic_keys_by_thread
    workspace_key = workspace_path.strip()
    workspace_root: Optional[Path] = None
    try:
        workspace_root = Path(workspace_key).expanduser().resolve()
    except Exception:
        workspace_root = None

    def matches(candidate_path: Optional[str]) -> bool:
        if not isinstance(candidate_path, str) or not candidate_path.strip():
            return False
        candidate_path = candidate_path.strip()
        if workspace_root is not None:
            try:
                candidate_root = Path(candidate_path).expanduser().resolve()
            except Exception:
                return False
            return _paths_compatible(workspace_root, candidate_root)
        return candidate_path == workspace_key

    def add_record(key: str, record: "TelegramTopicRecord") -> None:
        if not matches(record.workspace_path):
            return
        for thread_id in record.thread_ids:
            topic_keys_by_thread.setdefault(thread_id, set()).add(key)
            if thread_id not in previews:
                preview = _thread_summary_preview(record, thread_id)
                if preview:
                    previews[thread_id] = preview
            if thread_id in seen:
                continue
            seen.add(thread_id)
            thread_ids.append(thread_id)

    seen: set[str] = set()
    current = state.topics.get(current_key)
    if current is not None:
        add_record(current_key, current)
    for key, record in state.topics.items():
        if key == current_key:
            continue
        add_record(key, record)
    return thread_ids, previews, topic_keys_by_thread


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


def _repo_root(path: Path) -> Optional[Path]:
    try:
        return find_repo_root(path)
    except RepoNotFoundError:
        return None


def _paths_compatible(workspace_root: Path, resumed_root: Path) -> bool:
    if _path_within(workspace_root, resumed_root):
        return True
    if _path_within(resumed_root, workspace_root):
        return True
    workspace_repo = _repo_root(workspace_root)
    resumed_repo = _repo_root(resumed_root)
    if workspace_repo is None or resumed_repo is None:
        return False
    if workspace_root != workspace_repo:
        return False
    return workspace_repo == resumed_repo


def _should_trace_message(text: str) -> bool:
    if not text:
        return False
    if "(conversation " in text:
        return False
    lowered = text.lower()
    return any(token in lowered for token in TRACE_MESSAGE_TOKENS)


def _compact_preview(text: Any, limit: int = 40) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview or "(no preview)"


def _format_preview(text: Any) -> str:
    preview = "" if text is None else str(text)
    return preview if preview.strip() else "(no preview)"


def _coerce_thread_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread")
    if isinstance(thread, dict):
        merged = dict(thread)
        for key, value in payload.items():
            if key != "thread" and key not in merged:
                merged[key] = value
        return merged
    return dict(payload)


def _normalize_preview_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _preview_from_text(text: Optional[str], limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    trimmed = text.strip()
    if not trimmed or trimmed == "(No agent response.)":
        return None
    return _truncate_text(_normalize_preview_text(trimmed), limit)


def _coerce_preview_field(entry: dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _tail_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            lines: list[bytes] = []
            while position > 0 and len(lines) <= max_lines:
                read_size = min(4096, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                lines = buffer.splitlines()
            return [
                line.decode("utf-8", errors="replace")
                for line in lines[-max_lines:]
            ]
    except OSError:
        return []


def _extract_text_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        text = payload.strip()
        return text if text else None
    if isinstance(payload, list):
        parts = []
        for item in payload:
            text = _extract_text_payload(item)
            if text:
                parts.append(text)
        if parts:
            return " ".join(parts)
        return None
    if isinstance(payload, dict):
        for key in ("text", "input_text", "output_text", "message", "value", "delta"):
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        content = payload.get("content")
        if content is not None:
            return _extract_text_payload(content)
    return None


def _iter_role_texts(
    payload: Any,
    *,
    default_role: Optional[str] = None,
    depth: int = 0,
) -> Iterable[tuple[str, str]]:
    if depth > 5:
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_role_texts(
                item, default_role=default_role, depth=depth + 1
            )
        return
    if not isinstance(payload, dict):
        return
    role = payload.get("role") if isinstance(payload.get("role"), str) else None
    type_value = payload.get("type") if isinstance(payload.get("type"), str) else None
    role_hint = role or default_role
    if not role_hint and type_value:
        lowered = type_value.lower()
        if lowered in ("user", "user_message", "input", "input_text", "prompt", "request"):
            role_hint = "user"
        elif lowered in (
            "assistant",
            "assistant_message",
            "output",
            "output_text",
            "response",
        ):
            role_hint = "assistant"
        else:
            tokens = [token for token in re.split(r"[._]+", lowered) if token]
            if any(
                token in ("user", "input", "prompt", "request") for token in tokens
            ):
                role_hint = "user"
            elif any(
                token in ("assistant", "output", "response", "completion")
                for token in tokens
            ):
                role_hint = "assistant"
    text = _extract_text_payload(payload)
    if role_hint in ("user", "assistant") and text:
        yield role_hint, text
    nested_payload = payload.get("payload")
    if nested_payload is not None:
        yield from _iter_role_texts(
            nested_payload, default_role=role_hint, depth=depth + 1
        )
    for key in ("input", "output", "messages", "items", "events"):
        if key in payload:
            yield from _iter_role_texts(
                payload[key],
                default_role="user" if key == "input" else "assistant",
                depth=depth + 1,
            )
    for key in ("request", "response", "message", "item", "turn", "event", "data"):
        if key in payload:
            next_role = role_hint
            if next_role is None:
                if key == "request":
                    next_role = "user"
                elif key == "response":
                    next_role = "assistant"
            yield from _iter_role_texts(
                payload[key], default_role=next_role, depth=depth + 1
            )


def _extract_rollout_preview(path: Path) -> tuple[Optional[str], Optional[str]]:
    lines = _tail_text_lines(path, RESUME_PREVIEW_SCAN_LINES)
    if not lines:
        return None, None
    last_user = None
    last_assistant = None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for role, text in _iter_role_texts(payload):
            if role == "assistant" and last_assistant is None:
                last_assistant = text
            elif role == "user" and last_user is None:
                last_user = text
            if last_user and last_assistant:
                return last_user, last_assistant
    return last_user, last_assistant


def _extract_turns_preview(turns: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(turns, list):
        return None, None
    last_user = None
    last_assistant = None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        candidates: list[Any] = []
        for key in ("items", "messages", "input", "output"):
            value = turn.get(key)
            if value is not None:
                candidates.append(value)
        if not candidates:
            candidates.append(turn)
        for candidate in candidates:
            if isinstance(candidate, list):
                iterable = reversed(candidate)
            else:
                iterable = (candidate,)
            for item in iterable:
                for role, text in _iter_role_texts(item):
                    if role == "assistant" and last_assistant is None:
                        last_assistant = text
                    elif role == "user" and last_user is None:
                        last_user = text
                    if last_user and last_assistant:
                        return last_user, last_assistant
    return last_user, last_assistant


def _extract_thread_preview_parts(entry: Any) -> tuple[Optional[str], Optional[str]]:
    entry = _coerce_thread_payload(entry)
    user_preview_keys = (
        "last_user_message",
        "lastUserMessage",
        "last_user",
        "lastUser",
        "last_user_text",
        "lastUserText",
        "user_preview",
        "userPreview",
    )
    assistant_preview_keys = (
        "last_assistant_message",
        "lastAssistantMessage",
        "last_assistant",
        "lastAssistant",
        "last_assistant_text",
        "lastAssistantText",
        "assistant_preview",
        "assistantPreview",
        "last_response",
        "lastResponse",
        "response_preview",
        "responsePreview",
    )
    user_preview = _coerce_preview_field(entry, user_preview_keys)
    assistant_preview = _coerce_preview_field(entry, assistant_preview_keys)
    if user_preview is None:
        preview = entry.get("preview")
        if isinstance(preview, str) and preview.strip():
            user_preview = preview.strip()
    turns = entry.get("turns")
    if turns and (not user_preview or not assistant_preview):
        turn_user, turn_assistant = _extract_turns_preview(turns)
        if not user_preview and turn_user:
            user_preview = turn_user
        if not assistant_preview and turn_assistant:
            assistant_preview = turn_assistant
    rollout_path = _extract_rollout_path(entry)
    if rollout_path and (not user_preview or not assistant_preview):
        path = Path(rollout_path)
        if path.exists():
            rollout_user, rollout_assistant = _extract_rollout_preview(path)
            if not user_preview and rollout_user:
                user_preview = rollout_user
            if not assistant_preview and rollout_assistant:
                assistant_preview = rollout_assistant
    if user_preview:
        user_preview = _truncate_text(
            _normalize_preview_text(user_preview), RESUME_PREVIEW_USER_LIMIT
        )
    if assistant_preview:
        assistant_preview = _truncate_text(
            _normalize_preview_text(assistant_preview),
            RESUME_PREVIEW_ASSISTANT_LIMIT,
        )
    return user_preview, assistant_preview


def _format_preview_parts(
    user_preview: Optional[str], assistant_preview: Optional[str]
) -> str:
    if user_preview and assistant_preview:
        return f"User: {user_preview}\nAssistant: {assistant_preview}"
    if user_preview:
        return f"User: {user_preview}"
    if assistant_preview:
        return f"Assistant: {assistant_preview}"
    return "(no preview)"


def _format_thread_preview(entry: Any) -> str:
    user_preview, assistant_preview = _extract_thread_preview_parts(entry)
    return _format_preview_parts(user_preview, assistant_preview)


def _format_summary_preview(summary: ThreadSummary) -> str:
    user_preview = _preview_from_text(
        summary.user_preview, RESUME_PREVIEW_USER_LIMIT
    )
    assistant_preview = _preview_from_text(
        summary.assistant_preview, RESUME_PREVIEW_ASSISTANT_LIMIT
    )
    return _format_preview_parts(user_preview, assistant_preview)


def _thread_summary_preview(
    record: "TelegramTopicRecord", thread_id: str
) -> Optional[str]:
    summary = record.thread_summaries.get(thread_id)
    if summary is None:
        return None
    preview = _format_summary_preview(summary)
    if preview == "(no preview)":
        return None
    return preview


def _format_missing_thread_label(thread_id: str, preview: Optional[str]) -> str:
    if preview:
        return preview
    prefix = thread_id[:8]
    suffix = "..." if len(thread_id) > 8 else ""
    return f"Thread {prefix}{suffix} (not indexed yet)"


def _resume_thread_list_limit(thread_ids: Sequence[str]) -> int:
    desired = max(DEFAULT_PAGE_SIZE, len(thread_ids) or DEFAULT_PAGE_SIZE)
    return min(THREAD_LIST_PAGE_LIMIT, desired)


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _extract_turn_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        if not isinstance(candidate, dict):
            continue
        for key in ("threadId", "thread_id"):
            thread_id = _coerce_id(candidate.get(key))
            if thread_id:
                return thread_id
        thread = candidate.get("thread")
        if isinstance(thread, dict):
            thread_id = _coerce_id(
                thread.get("id")
                or thread.get("threadId")
                or thread.get("thread_id")
            )
            if thread_id:
                return thread_id
    return None


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _extract_first_bold_span(text: str) -> Optional[str]:
    if not text:
        return None
    start = text.find("**")
    if start < 0:
        return None
    end = text.find("**", start + 2)
    if end < 0:
        return None
    content = text[start + 2 : end].strip()
    return content or None


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


def _telegram_lock_path(token: str) -> Path:
    if not isinstance(token, str) or not token:
        raise ValueError("token is required")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".codex-autorunner" / "locks" / f"telegram_bot_{digest}.lock"


def _read_lock_payload(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _lock_payload_summary(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("pid", "started_at", "host", "cwd", "config_root"):
        if key in payload:
            summary[key] = payload.get(key)
    return summary


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
