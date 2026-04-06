import asyncio
import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....core.config import (
    ConfigError,
    collect_env_overrides,
    load_hub_config,
)
from ....core.logging_utils import log_event, setup_rotating_logger
from ....core.redaction import redact_text
from ....integrations.telegram.adapter import TelegramAPIError, TelegramBotClient
from ....integrations.telegram.service import (
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramBotService,
)
from ....integrations.telegram.state import (
    TelegramStateStore,
    parse_topic_key,
    topic_key,
)
from ....voice import VoiceConfig

_LOG_PREFIX_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\[(?P<level>[A-Z]+)\] "
    r"(?P<message>.*)$"
)
_CONVERSATION_IN_TEXT_PATTERN = re.compile(
    r"\bconversation\s+(?P<conversation_id>-?\d+:(?:\d+|root)(?::[^\s\)]+)?)",
    re.IGNORECASE,
)
_TOPIC_KEY_IN_TEXT_PATTERN = re.compile(
    r"(?P<conversation_id>-?\d+:(?:\d+|root)(?::[^\s\)\"',]+)?)",
    re.IGNORECASE,
)
_ERROR_EVENT_HINTS = (
    ".failed",
    ".error",
    ".timeout",
    ".disconnected",
    ".exception",
    "turn_error",
)
_SEARCH_LOG_GLOBS = (
    "codex-autorunner.log*",
    "codex-server.log*",
    "codex-autorunner-hub.log*",
    "codex-autorunner-telegram.log*",
    "logs/*.log*",
)
_TELEGRAM_BOT_URL_PATTERN = re.compile(
    r"(https?://api\.telegram\.org/(?:file/)?bot)[^/\s]+",
    re.IGNORECASE,
)
_TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


@dataclass(frozen=True)
class _ConversationTarget:
    conversation_id: str
    chat_id: int
    thread_id: Optional[int]


@dataclass(frozen=True)
class _LogTraceMatch:
    path: Path
    line_no: int
    sequence: int
    timestamp: Optional[str]
    level: Optional[str]
    event: Optional[str]
    payload: Optional[dict[str, Any]]
    raw_line: str
    is_error_candidate: bool
    context: tuple[str, ...]


def _extract_conversation_id(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("conversation query is empty")
    match = _CONVERSATION_IN_TEXT_PATTERN.search(cleaned)
    if match:
        return match.group("conversation_id").strip()
    lowered = cleaned.lower()
    if lowered.startswith("conversation "):
        cleaned = cleaned[len("conversation ") :].strip()
    cleaned = cleaned.strip("()[]{}.,;\"' ")
    return cleaned


def _parse_conversation_target(query: str) -> _ConversationTarget:
    conversation_id = _extract_conversation_id(query)
    chat_id: int
    thread_id: Optional[int]
    try:
        chat_id, thread_id, _scope = parse_topic_key(conversation_id)
    except ValueError as exc:
        raise ValueError(
            "Could not parse conversation id. "
            "Use '<chat_id>:<thread_id|root>' or include '(conversation <id>)'."
        ) from exc
    return _ConversationTarget(
        conversation_id=topic_key(chat_id, thread_id),
        chat_id=chat_id,
        thread_id=thread_id,
    )


def _split_log_line(raw_line: str) -> tuple[Optional[str], Optional[str], str]:
    match = _LOG_PREFIX_PATTERN.match(raw_line)
    if not match:
        return None, None, raw_line
    return (
        match.group("timestamp"),
        match.group("level"),
        match.group("message"),
    )


def _parse_log_payload(message: str) -> Optional[dict[str, Any]]:
    stripped = message.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _payload_matches_conversation(
    payload: dict[str, Any],
    *,
    conversation_id: str,
    chat_id: int,
    thread_id: Optional[int],
) -> bool:
    payload_conversation = payload.get("conversation_id")
    if (
        isinstance(payload_conversation, str)
        and payload_conversation == conversation_id
    ):
        return True

    payload_chat = _coerce_optional_int(payload.get("chat_id"))
    if payload_chat != chat_id:
        return False

    if "thread_id" in payload:
        is_valid_thread, normalized_payload_thread = _coerce_optional_thread_id(
            payload.get("thread_id")
        )
        if not is_valid_thread:
            return False
        return normalized_payload_thread == thread_id

    payload_topic_key = payload.get("topic_key")
    if isinstance(payload_topic_key, str):
        try:
            topic_chat, topic_thread, _scope = parse_topic_key(payload_topic_key)
        except ValueError:
            return False
        return topic_chat == chat_id and topic_thread == thread_id

    return False


def _line_matches_conversation(
    raw_line: str, payload: Optional[dict[str, Any]], target: _ConversationTarget
) -> bool:
    if _raw_line_mentions_conversation(raw_line, target):
        return True
    if not isinstance(payload, dict):
        return False
    return _payload_matches_conversation(
        payload,
        conversation_id=target.conversation_id,
        chat_id=target.chat_id,
        thread_id=target.thread_id,
    )


def _coerce_optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if re.fullmatch(r"-?\d+", normalized):
            return int(normalized)
    return None


def _coerce_optional_thread_id(value: Any) -> tuple[bool, Optional[int]]:
    if value is None:
        return True, None
    if isinstance(value, str) and value.strip().lower() == "root":
        return True, None
    coerced = _coerce_optional_int(value)
    if coerced is None:
        return False, None
    return True, coerced


def _raw_line_mentions_conversation(raw_line: str, target: _ConversationTarget) -> bool:
    for match in _TOPIC_KEY_IN_TEXT_PATTERN.finditer(raw_line):
        candidate = match.group("conversation_id")
        try:
            topic_chat, topic_thread, _scope = parse_topic_key(candidate)
        except ValueError:
            continue
        if topic_chat == target.chat_id and topic_thread == target.thread_id:
            return True
    return False


def _match_chronological_key(match: _LogTraceMatch) -> tuple[int, str, int]:
    # Use parsed timestamps first; preserve original sequence for stable ties.
    if isinstance(match.timestamp, str) and match.timestamp:
        return (0, match.timestamp, match.sequence)
    return (1, "", match.sequence)


def _is_error_candidate(
    *,
    level: Optional[str],
    event: Optional[str],
    payload: Optional[dict[str, Any]],
    message: str,
) -> bool:
    normalized_level = (level or "").upper()
    if normalized_level in {"ERROR", "CRITICAL"}:
        return True
    normalized_event = (event or "").lower()
    if normalized_event and any(
        hint in normalized_event for hint in _ERROR_EVENT_HINTS
    ):
        return True
    if isinstance(payload, dict) and (
        isinstance(payload.get("error"), str)
        or isinstance(payload.get("error_type"), str)
    ):
        return True
    lowered_message = message.lower()
    return "traceback" in lowered_message or "exception" in lowered_message


def _collect_log_paths(
    roots: list[Path], log_path: Path, backup_count: int
) -> list[Path]:
    candidates: set[Path] = set()
    if log_path.exists():
        candidates.add(log_path)

    if backup_count > 0:
        for idx in range(1, backup_count + 1):
            rotated = log_path.with_name(f"{log_path.name}.{idx}")
            if rotated.exists():
                candidates.add(rotated)

    for root in roots:
        state_root = root / ".codex-autorunner"
        for pattern in _SEARCH_LOG_GLOBS:
            for path in state_root.glob(pattern):
                if path.is_file():
                    candidates.add(path)

    return sorted(candidates, key=lambda item: str(item))


def _read_log_lines(path: Path, scan_lines: int) -> list[tuple[int, str]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if scan_lines <= 0:
            return [
                (idx, line.rstrip("\n")) for idx, line in enumerate(handle, start=1)
            ]
        tail: deque[tuple[int, str]] = deque(maxlen=scan_lines)
        for idx, line in enumerate(handle, start=1):
            tail.append((idx, line.rstrip("\n")))
    return list(tail)


def _format_match_line(match: _LogTraceMatch) -> str:
    timestamp = match.timestamp or "unknown-time"
    level = (match.level or "INFO").upper()
    event = f" event={match.event}" if match.event else ""
    return f"{timestamp} {level} {match.path}:{match.line_no}{event}"


def _sanitize_trace_text(text: str) -> str:
    redacted = redact_text(text)
    redacted = _TELEGRAM_BOT_URL_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _TELEGRAM_BOT_TOKEN_PATTERN.sub(
        "[TELEGRAM_BOT_TOKEN_REDACTED]", redacted
    )
    return redacted


def _sanitize_payload_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_trace_text(value)
    if isinstance(value, list):
        return [_sanitize_payload_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_payload_value(inner_value)
            for key, inner_value in value.items()
        }
    return value


def register_telegram_commands(
    telegram_app: typer.Typer,
    *,
    raise_exit: Callable,
    require_optional_feature: Callable,
) -> None:
    @telegram_app.command("start")
    def telegram_start(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ):
        """Start the Telegram bot polling service."""
        require_optional_feature(
            feature="telegram",
            deps=[("httpx", "httpx")],
            extra="telegram",
        )
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
            collaboration_raw=(
                config.raw.get("collaboration_policy")
                if isinstance(config.raw, dict)
                else None
            ),
            opencode_raw=(
                config.raw.get("opencode") if isinstance(config.raw, dict) else None
            ),
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
        try:
            telegram_cfg.validate()
        except TelegramBotConfigError as exc:
            raise_exit(str(exc), cause=exc)
        logger = setup_rotating_logger("codex-autorunner-telegram", config.log)
        env_overrides = collect_env_overrides(env=os.environ, include_telegram=True)
        if env_overrides:
            logger.info("Environment overrides active: %s", ", ".join(env_overrides))
        log_event(
            logger,
            logging.INFO,
            "telegram.bot.starting",
            root=str(config.root),
            mode="hub",
        )
        voice_raw = config.repo_defaults.get("voice") if config.repo_defaults else None
        voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
        update_repo_url = config.update_repo_url
        update_repo_ref = config.update_repo_ref
        update_backend = config.update_backend
        update_linux_service_names = config.update_linux_service_names

        async def _run() -> None:
            service = TelegramBotService(
                telegram_cfg,
                logger=logger,
                hub_root=config.root,
                manifest_path=config.manifest_path,
                voice_config=voice_config,
                housekeeping_config=config.housekeeping,
                update_repo_url=update_repo_url,
                update_repo_ref=update_repo_ref,
                update_skip_checks=config.update_skip_checks,
                update_backend=update_backend,
                update_linux_service_names=update_linux_service_names,
                app_server_auto_restart=config.app_server.auto_restart,
            )
            await service.run_polling()

        try:
            asyncio.run(_run())
        except TelegramBotLockError as exc:
            raise_exit(str(exc), cause=exc)

    @telegram_app.command("health")
    def telegram_health(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
        timeout: float = typer.Option(5.0, "--timeout", help="Timeout (seconds)"),
    ):
        """Run a Telegram API health check (`getMe`) using current bot config."""
        require_optional_feature(
            feature="telegram",
            deps=[("httpx", "httpx")],
            extra="telegram",
        )
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
            collaboration_raw=(
                config.raw.get("collaboration_policy")
                if isinstance(config.raw, dict)
                else None
            ),
            opencode_raw=(
                config.raw.get("opencode") if isinstance(config.raw, dict) else None
            ),
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
        bot_token = telegram_cfg.bot_token
        if not bot_token:
            raise_exit(f"missing bot token env '{telegram_cfg.bot_token_env}'")
        assert bot_token is not None
        timeout_seconds = max(float(timeout), 0.1)

        async def _run() -> None:
            async with TelegramBotClient(bot_token) as client:
                await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)

        try:
            asyncio.run(_run())
        except TelegramAPIError as exc:
            raise_exit(f"Telegram health check failed: {exc}", cause=exc)

    @telegram_app.command("state-check")
    def telegram_state_check(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ):
        """Validate Telegram state store connectivity and schema access."""
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
            collaboration_raw=(
                config.raw.get("collaboration_policy")
                if isinstance(config.raw, dict)
                else None
            ),
            opencode_raw=(
                config.raw.get("opencode") if isinstance(config.raw, dict) else None
            ),
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")

        try:
            store = TelegramStateStore(
                telegram_cfg.state_file,
                default_approval_mode=telegram_cfg.defaults.approval_mode,
            )
            store._connection_sync()
        except Exception as exc:  # intentional: state diagnostic error barrier
            raise_exit(f"Telegram state check failed: {exc}", cause=exc)

    @telegram_app.command("trace")
    def telegram_trace(
        conversation_query: Optional[str] = typer.Argument(
            None,
            help=(
                "Conversation id ('<chat_id>:<thread_id|root>') "
                "or a message containing '(conversation <id>)'."
            ),
        ),
        conversation: Optional[str] = typer.Option(
            None,
            "--conversation",
            help=(
                "Conversation id or text containing '(conversation <id>)'. "
                "Use this when the id starts with '-'."
            ),
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
        context_lines: int = typer.Option(
            2, "--context-lines", min=0, help="Context lines before/after each match"
        ),
        limit: int = typer.Option(
            50, "--limit", min=1, help="Max matches shown per section"
        ),
        scan_lines: int = typer.Option(
            0,
            "--scan-lines",
            help="Lines to scan per log file from the end (0 scans whole file)",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Trace conversation-scoped Telegram log events and likely error lines."""
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        query_value = (
            conversation if isinstance(conversation, str) else conversation_query
        )
        if not isinstance(query_value, str) or not query_value.strip():
            raise_exit("Provide CONVERSATION_QUERY or --conversation.")
        assert isinstance(query_value, str)

        try:
            target = _parse_conversation_target(query_value)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        requested_root = (path or Path.cwd()).resolve()
        search_roots = sorted(
            {
                config.root.resolve(),
                requested_root,
            },
            key=lambda item: str(item),
        )
        log_paths = _collect_log_paths(
            search_roots,
            config.log.path,
            backup_count=max(int(config.log.backup_count), 0),
        )
        if not log_paths:
            searched = ", ".join(
                str(root / ".codex-autorunner") for root in search_roots
            )
            raise_exit(f"No log files found under: {searched}")

        matches: list[_LogTraceMatch] = []
        match_sequence = 0
        total_scanned_lines = 0
        read_errors: list[str] = []
        for log_path in log_paths:
            try:
                indexed_lines = _read_log_lines(log_path, scan_lines)
            except OSError as exc:
                read_errors.append(f"{log_path}: {exc}")
                continue
            total_scanned_lines += len(indexed_lines)
            for index, (line_no, raw_line) in enumerate(indexed_lines):
                timestamp, level, message = _split_log_line(raw_line)
                payload = _parse_log_payload(message)
                if not _line_matches_conversation(raw_line, payload, target):
                    continue
                safe_payload = (
                    _sanitize_payload_value(payload)
                    if isinstance(payload, dict)
                    else None
                )
                event_value = (
                    payload.get("event") if isinstance(payload, dict) else None
                )
                event = event_value if isinstance(event_value, str) else None
                start = max(0, index - context_lines)
                end = min(len(indexed_lines), index + context_lines + 1)
                context = tuple(
                    f"{context_line_no}: {_sanitize_trace_text(context_line)}"
                    for context_line_no, context_line in indexed_lines[start:end]
                )
                matches.append(
                    _LogTraceMatch(
                        path=log_path,
                        line_no=line_no,
                        sequence=match_sequence,
                        timestamp=timestamp,
                        level=level,
                        event=event,
                        payload=(
                            safe_payload if isinstance(safe_payload, dict) else None
                        ),
                        raw_line=_sanitize_trace_text(raw_line),
                        is_error_candidate=_is_error_candidate(
                            level=level,
                            event=event,
                            payload=payload,
                            message=message,
                        ),
                        context=context,
                    )
                )
                match_sequence += 1

        matches = sorted(matches, key=_match_chronological_key)
        error_matches = [match for match in matches if match.is_error_candidate]
        recent_matches = matches[-limit:]
        recent_error_matches = error_matches[-limit:]

        if not matches:
            raise_exit(
                "No matches for conversation_id "
                f"{target.conversation_id}. Searched {len(log_paths)} log file(s)."
            )

        if json_output:

            def _serialize_match(match: _LogTraceMatch) -> dict[str, Any]:
                return {
                    "path": str(match.path),
                    "line_no": match.line_no,
                    "timestamp": match.timestamp,
                    "level": match.level,
                    "event": match.event,
                    "payload": match.payload,
                    "raw_line": match.raw_line,
                    "is_error_candidate": match.is_error_candidate,
                    "context": list(match.context),
                }

            typer.echo(
                json.dumps(
                    {
                        "conversation_id": target.conversation_id,
                        "chat_id": target.chat_id,
                        "thread_id": target.thread_id,
                        "log_path": str(config.log.path),
                        "searched_paths": [str(path) for path in log_paths],
                        "total_scanned_lines": total_scanned_lines,
                        "matches": [
                            _serialize_match(match) for match in recent_matches
                        ],
                        "errors": [
                            _serialize_match(match) for match in recent_error_matches
                        ],
                        "read_errors": read_errors,
                    },
                    indent=2,
                    sort_keys=False,
                )
            )
            return

        typer.echo(
            f"Conversation: {target.conversation_id} "
            f"(chat_id={target.chat_id}, thread_id={target.thread_id or 'root'})"
        )
        typer.echo(f"Configured log path: {config.log.path}")
        typer.echo(
            f"Searched files: {len(log_paths)} | Scanned lines: {total_scanned_lines}"
        )
        typer.echo(
            f"Matched lines: {len(matches)} | Error candidates: {len(error_matches)}"
        )

        if read_errors:
            typer.echo("Read errors:")
            for item in read_errors:
                typer.echo(f"- {item}")

        typer.echo("Searched paths:")
        for log_path in log_paths:
            typer.echo(f"- {log_path}")

        if recent_error_matches:
            typer.echo("Error candidates:")
            for match in recent_error_matches:
                typer.echo(f"- {_format_match_line(match)}")
                if match.payload and isinstance(match.payload.get("reason"), str):
                    typer.echo(f"  reason={match.payload['reason']}")
                if match.payload and isinstance(match.payload.get("error"), str):
                    typer.echo(f"  error={match.payload['error']}")
                if match.payload and isinstance(match.payload.get("error_type"), str):
                    typer.echo(f"  error_type={match.payload['error_type']}")
                for context_line in match.context:
                    typer.echo(f"  {context_line}")

        typer.echo("Recent matched lines:")
        for match in recent_matches:
            typer.echo(f"- {_format_match_line(match)}")
