from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .adapter import TelegramAllowlist
from .state import APPROVAL_MODE_YOLO, normalize_approval_mode

DEFAULT_ALLOWED_UPDATES = ("message", "edited_message", "callback_query")
DEFAULT_POLL_TIMEOUT_SECONDS = 30
DEFAULT_SAFE_APPROVAL_POLICY = "on-request"
DEFAULT_YOLO_APPROVAL_POLICY = "never"
DEFAULT_YOLO_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_PARSE_MODE = "HTML"
DEFAULT_STATE_FILE = ".codex-autorunner/telegram_state.json"
DEFAULT_APP_SERVER_COMMAND = ["codex", "app-server"]
DEFAULT_APP_SERVER_MAX_HANDLES = 20
DEFAULT_APP_SERVER_IDLE_TTL_SECONDS = 3600
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
DEFAULT_MEDIA_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_MAX_VOICE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_IMAGE_PROMPT = "Describe the image."
DEFAULT_SHELL_TIMEOUT_MS = 120_000
DEFAULT_SHELL_MAX_OUTPUT_CHARS = 3800

PARSE_MODE_ALIASES = {
    "html": "HTML",
    "markdown": "Markdown",
    "markdownv2": "MarkdownV2",
}


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
    files: bool
    max_image_bytes: int
    max_voice_bytes: int
    max_file_bytes: int
    image_prompt: str


@dataclass(frozen=True)
class TelegramBotShellConfig:
    enabled: bool
    timeout_ms: int
    max_output_chars: int


@dataclass(frozen=True)
class TelegramBotCommandScope:
    scope: dict[str, Any]
    language_code: str


@dataclass(frozen=True)
class TelegramBotCommandRegistration:
    enabled: bool
    scopes: list[TelegramBotCommandScope]


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
    shell: TelegramBotShellConfig
    command_registration: TelegramBotCommandRegistration
    state_file: Path
    app_server_command_env: str
    app_server_command: list[str]
    app_server_max_handles: Optional[int]
    app_server_idle_ttl_seconds: Optional[int]
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
        cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
        enabled = bool(cfg.get("enabled", False))
        mode = str(cfg.get("mode", "polling"))
        bot_token_env = str(cfg.get("bot_token_env", "CAR_TELEGRAM_BOT_TOKEN"))
        chat_id_env = str(cfg.get("chat_id_env", "CAR_TELEGRAM_CHAT_ID"))
        parse_mode_raw = (
            cfg.get("parse_mode") if "parse_mode" in cfg else DEFAULT_PARSE_MODE
        )
        parse_mode = _normalize_parse_mode(parse_mode_raw)
        debug_raw_value = cfg.get("debug")
        debug_raw: dict[str, Any] = (
            debug_raw_value if isinstance(debug_raw_value, dict) else {}
        )
        debug_prefix_context = bool(debug_raw.get("prefix_context", False))
        bot_token = env.get(bot_token_env)

        allowed_chat_ids = set(_parse_int_list(cfg.get("allowed_chat_ids")))
        allowed_chat_ids.update(_parse_int_list(env.get(chat_id_env)))
        allowed_user_ids = set(_parse_int_list(cfg.get("allowed_user_ids")))

        require_topics = bool(cfg.get("require_topics", False))

        defaults_raw_value = cfg.get("defaults")
        defaults_raw: dict[str, Any] = (
            defaults_raw_value if isinstance(defaults_raw_value, dict) else {}
        )
        approval_mode = normalize_approval_mode(
            defaults_raw.get("approval_mode"), default=APPROVAL_MODE_YOLO
        )
        approval_policy = defaults_raw.get(
            "approval_policy", DEFAULT_SAFE_APPROVAL_POLICY
        )
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
            approval_policy=(
                str(approval_policy) if approval_policy is not None else None
            ),
            sandbox_policy=sandbox_policy,
            yolo_approval_policy=yolo_approval_policy,
            yolo_sandbox_policy=yolo_sandbox_policy,
        )

        concurrency_raw_value = cfg.get("concurrency")
        concurrency_raw: dict[str, Any] = (
            concurrency_raw_value if isinstance(concurrency_raw_value, dict) else {}
        )
        max_parallel_turns = int(concurrency_raw.get("max_parallel_turns", 4))
        if max_parallel_turns <= 0:
            max_parallel_turns = 1
        per_topic_queue = bool(concurrency_raw.get("per_topic_queue", True))
        concurrency = TelegramBotConcurrency(
            max_parallel_turns=max_parallel_turns,
            per_topic_queue=per_topic_queue,
        )

        media_raw_value = cfg.get("media")
        media_raw: dict[str, Any] = (
            media_raw_value if isinstance(media_raw_value, dict) else {}
        )
        media_enabled = bool(media_raw.get("enabled", True))
        media_images = bool(media_raw.get("images", True))
        media_voice = bool(media_raw.get("voice", True))
        media_files = bool(media_raw.get("files", True))
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
        max_file_bytes = int(
            media_raw.get("max_file_bytes", DEFAULT_MEDIA_MAX_FILE_BYTES)
        )
        if max_file_bytes <= 0:
            max_file_bytes = DEFAULT_MEDIA_MAX_FILE_BYTES
        image_prompt = str(
            media_raw.get("image_prompt", DEFAULT_MEDIA_IMAGE_PROMPT)
        ).strip()
        if not image_prompt:
            image_prompt = DEFAULT_MEDIA_IMAGE_PROMPT
        media = TelegramBotMediaConfig(
            enabled=media_enabled,
            images=media_images,
            voice=media_voice,
            files=media_files,
            max_image_bytes=max_image_bytes,
            max_voice_bytes=max_voice_bytes,
            max_file_bytes=max_file_bytes,
            image_prompt=image_prompt,
        )

        shell_raw_value = cfg.get("shell")
        shell_raw: dict[str, Any] = (
            shell_raw_value if isinstance(shell_raw_value, dict) else {}
        )
        shell_enabled = bool(shell_raw.get("enabled", False))
        shell_timeout_ms = int(shell_raw.get("timeout_ms", DEFAULT_SHELL_TIMEOUT_MS))
        if shell_timeout_ms <= 0:
            shell_timeout_ms = DEFAULT_SHELL_TIMEOUT_MS
        shell_max_output_chars = int(
            shell_raw.get("max_output_chars", DEFAULT_SHELL_MAX_OUTPUT_CHARS)
        )
        if shell_max_output_chars <= 0:
            shell_max_output_chars = DEFAULT_SHELL_MAX_OUTPUT_CHARS
        shell = TelegramBotShellConfig(
            enabled=shell_enabled,
            timeout_ms=shell_timeout_ms,
            max_output_chars=shell_max_output_chars,
        )

        command_reg_raw_value = cfg.get("command_registration")
        command_reg_raw: dict[str, Any] = (
            command_reg_raw_value if isinstance(command_reg_raw_value, dict) else {}
        )
        command_reg_enabled = bool(command_reg_raw.get("enabled", True))
        scopes = _parse_command_scopes(command_reg_raw.get("scopes"))
        command_registration = TelegramBotCommandRegistration(
            enabled=command_reg_enabled, scopes=scopes
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

        app_server_raw_value = cfg.get("app_server")
        app_server_raw: dict[str, Any] = (
            app_server_raw_value if isinstance(app_server_raw_value, dict) else {}
        )
        app_server_max_handles = int(
            app_server_raw.get("max_handles", DEFAULT_APP_SERVER_MAX_HANDLES)
        )
        if app_server_max_handles <= 0:
            app_server_max_handles = None
        app_server_idle_ttl_seconds = int(
            app_server_raw.get("idle_ttl_seconds", DEFAULT_APP_SERVER_IDLE_TTL_SECONDS)
        )
        if app_server_idle_ttl_seconds <= 0:
            app_server_idle_ttl_seconds = None

        polling_raw_value = cfg.get("polling")
        polling_raw: dict[str, Any] = (
            polling_raw_value if isinstance(polling_raw_value, dict) else {}
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
            shell=shell,
            command_registration=command_registration,
            state_file=state_file,
            app_server_command_env=app_server_command_env,
            app_server_command=app_server_command,
            app_server_max_handles=app_server_max_handles,
            app_server_idle_ttl_seconds=app_server_idle_ttl_seconds,
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


def _parse_command(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [part for part in shlex.split(raw) if part]
    return []


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


def _normalize_parse_mode(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    return PARSE_MODE_ALIASES.get(cleaned.lower(), cleaned)


def _parse_command_scopes(raw: Any) -> list[TelegramBotCommandScope]:
    scopes: list[TelegramBotCommandScope] = []
    if raw is None:
        raw = [
            {"type": "default", "language_code": ""},
            {"type": "all_group_chats", "language_code": ""},
        ]
    if isinstance(raw, list):
        for item in raw:
            scope_payload: dict[str, Any] = {"type": "default"}
            language_code = ""
            if isinstance(item, str):
                scope_payload = {"type": item}
            elif isinstance(item, dict):
                if isinstance(item.get("scope"), dict):
                    scope_payload = dict(item.get("scope", {}))
                else:
                    scope_payload = {
                        "type": (
                            str(item.get("type", "default"))
                            if item.get("type") is not None
                            else "default"
                        )
                    }
                    for key, value in item.items():
                        if key in ("scope", "type", "language_code"):
                            continue
                        scope_payload[key] = value
                language_code_raw = item.get("language_code", "")
                if language_code_raw is not None:
                    language_code = str(language_code_raw)
            if "type" not in scope_payload:
                scope_payload["type"] = "default"
            scopes.append(
                TelegramBotCommandScope(
                    scope=scope_payload, language_code=language_code
                )
            )
    if not scopes:
        scopes.append(
            TelegramBotCommandScope(scope={"type": "default"}, language_code="")
        )
    return scopes
