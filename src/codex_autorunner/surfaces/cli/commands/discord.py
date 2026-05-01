from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import typer

from ....core.config import ACTIVE_HUB_ROOT_ENV, ConfigError, load_hub_config
from ....core.logging_utils import setup_rotating_logger
from ....core.redaction import redact_text
from ....integrations.discord.command_registry import sync_commands
from ....integrations.discord.commands import build_application_commands
from ....integrations.discord.config import DiscordBotConfig, DiscordBotConfigError
from ....integrations.discord.rest import DiscordRestClient
from ....integrations.discord.service import create_discord_bot_service
from ._log_trace_common import (
    LogTraceMatch,
    collect_log_paths,
    extract_conversation_id,
    format_match_line,
    is_error_candidate,
    parse_log_payload,
    read_log_lines,
    sanitize_payload_value,
    split_log_line,
)

_CONVERSATION_IN_TEXT_PATTERN = re.compile(
    r"\bconversation\s+(?P<conversation_id>[^\s\)]+)",
    re.IGNORECASE,
)
_SEARCH_LOG_GLOBS = (
    "codex-autorunner.log*",
    "codex-server.log*",
    "codex-autorunner-hub.log*",
    "codex-autorunner-discord.log*",
    "logs/*.log*",
)
_DISCORD_WEBHOOK_URL_PATTERN = re.compile(
    r"(https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/)[^/\s]+",
    re.IGNORECASE,
)
_DISCORD_BOT_AUTH_PATTERN = re.compile(r"\b(Bot\s+)[A-Za-z0-9._-]{20,}\b")
_DISCORD_ID_TOKEN_PATTERN = re.compile(r"^\d+$")


@dataclass(frozen=True)
class _DiscordConversationTarget:
    conversation_id: str
    channel_id: str
    thread_id: Optional[str]


def _normalize_id(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _normalize_thread_id(value: Any) -> Optional[str]:
    normalized = _normalize_id(value)
    if normalized in {"-", "root"}:
        return None
    return normalized


def _parse_conversation_target(query: str) -> _DiscordConversationTarget:
    conversation_id = extract_conversation_id(query, _CONVERSATION_IN_TEXT_PATTERN)
    raw = conversation_id.strip()

    channel_id: Optional[str] = None
    thread_id: Optional[str] = None

    if raw.lower().startswith("discord:"):
        parts = raw.split(":", 2)
        if len(parts) == 3:
            channel_id = _normalize_id(parts[1])
            thread_id = _normalize_thread_id(parts[2])
    elif ":" in raw:
        channel_raw, thread_raw = raw.split(":", 1)
        channel_id = _normalize_id(channel_raw)
        thread_id = _normalize_thread_id(thread_raw)
    else:
        channel_id = _normalize_id(raw)
        thread_id = None

    if (
        not isinstance(channel_id, str)
        or not channel_id
        or not _DISCORD_ID_TOKEN_PATTERN.match(channel_id)
        or (
            isinstance(thread_id, str)
            and thread_id
            and not _DISCORD_ID_TOKEN_PATTERN.match(thread_id)
        )
    ):
        raise ValueError(
            "Could not parse conversation id. Use "
            "'discord:<channel_id>:<guild_id|->', '<channel_id>[:<guild_id|->]', "
            "or include '(conversation <id>)'."
        )

    canonical = f"discord:{channel_id}:{thread_id or '-'}"
    return _DiscordConversationTarget(
        conversation_id=canonical,
        channel_id=channel_id,
        thread_id=thread_id,
    )


def _payload_matches_conversation(
    payload: dict[str, Any],
    *,
    conversation_id: str,
    channel_id: str,
    thread_id: Optional[str],
) -> bool:
    payload_conversation = payload.get("conversation_id")
    if (
        isinstance(payload_conversation, str)
        and payload_conversation == conversation_id
    ):
        return True

    payload_channel = _normalize_id(payload.get("channel_id"))
    if payload_channel is None:
        payload_channel = _normalize_id(payload.get("chat_id"))
    if not isinstance(payload_channel, str) or payload_channel != channel_id:
        return False

    has_thread_hint = "thread_id" in payload or "guild_id" in payload
    if has_thread_hint:
        payload_thread = payload.get("thread_id")
        if payload_thread is None and "guild_id" in payload:
            payload_thread = payload.get("guild_id")
        return _normalize_thread_id(payload_thread) == thread_id

    return thread_id is None


def _line_matches_conversation(
    raw_line: str,
    payload: Optional[dict[str, Any]],
    target: _DiscordConversationTarget,
) -> bool:
    if target.conversation_id in raw_line:
        return True
    if f"channel_id={target.channel_id}" in raw_line:
        if target.thread_id is None:
            return True
        if (
            f"guild_id={target.thread_id}" in raw_line
            or f"thread_id={target.thread_id}" in raw_line
        ):
            return True
    if not isinstance(payload, dict):
        return False
    return _payload_matches_conversation(
        payload,
        conversation_id=target.conversation_id,
        channel_id=target.channel_id,
        thread_id=target.thread_id,
    )


def _sanitize_trace_text(text: str) -> str:
    redacted = redact_text(text)
    redacted = _DISCORD_WEBHOOK_URL_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _DISCORD_BOT_AUTH_PATTERN.sub(
        r"\1[DISCORD_BOT_TOKEN_REDACTED]", redacted
    )
    return redacted


def _require_discord_feature(require_optional_feature: Callable) -> None:
    require_optional_feature(
        feature="discord",
        deps=[("websockets", "websockets")],
        extra="discord",
    )


def _resolve_pma_enabled(hub_config: Any) -> bool:
    pma_raw = getattr(hub_config, "raw", {}).get("pma", {})
    if isinstance(pma_raw, dict):
        return bool(pma_raw.get("enabled", True))
    return True


async def _sync_discord_application_commands(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
    rest_client_factory: Callable[..., Any] = DiscordRestClient,
    sync_func: Callable[..., Awaitable[None]] = sync_commands,
) -> None:
    if not config.bot_token:
        raise DiscordBotConfigError(f"missing bot token env '{config.bot_token_env}'")
    if not config.application_id:
        raise DiscordBotConfigError(f"missing application id env '{config.app_id_env}'")

    commands = build_application_commands(config.root)
    async with rest_client_factory(bot_token=config.bot_token) as rest:
        await sync_func(
            rest,
            application_id=config.application_id,
            commands=commands,
            scope=config.command_registration.scope,
            guild_ids=config.command_registration.guild_ids,
            logger=logger,
        )


def register_discord_commands(
    app: typer.Typer,
    *,
    raise_exit: Callable,
    require_optional_feature: Callable,
) -> None:
    @app.command("start")
    def discord_start(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        """Start the Discord bot service."""
        _require_discord_feature(require_optional_feature)
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        os.environ[ACTIVE_HUB_ROOT_ENV] = str(config.root)
        try:
            discord_raw = (
                config.raw.get("discord_bot") if isinstance(config.raw, dict) else {}
            )
            pma_enabled = _resolve_pma_enabled(config)
            discord_cfg = DiscordBotConfig.from_raw(
                root=config.root,
                raw=discord_raw if isinstance(discord_raw, dict) else {},
                pma_enabled=pma_enabled,
                collaboration_raw=(
                    config.raw.get("collaboration_policy")
                    if isinstance(config.raw, dict)
                    else None
                ),
            )
            if not discord_cfg.enabled:
                raise_exit("discord_bot is disabled; set discord_bot.enabled: true")
            logger = setup_rotating_logger("codex-autorunner-discord", config.log)
            update_repo_url = config.update_repo_url
            update_repo_ref = config.update_repo_ref
            update_backend = config.update_backend
            update_linux_service_names = config.update_linux_service_names
            service = create_discord_bot_service(
                discord_cfg,
                logger=logger,
                manifest_path=config.manifest_path,
                update_repo_url=update_repo_url,
                update_repo_ref=update_repo_ref,
                update_skip_checks=config.update_skip_checks,
                update_backend=update_backend,
                update_linux_service_names=update_linux_service_names,
            )
            asyncio.run(service.run_forever())
        except DiscordBotConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except KeyboardInterrupt:
            typer.echo("Discord bot stopped.")

    @app.command("health")
    def discord_health(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        """Run Discord health checks (placeholder; not implemented)."""
        _require_discord_feature(require_optional_feature)
        raise NotImplementedError("Discord health check is not implemented yet.")

    @app.command("trace")
    def discord_trace(
        conversation_query: Optional[str] = typer.Argument(
            None,
            help=(
                "Conversation id ('discord:<channel_id>:<guild_id|->' or "
                "'<channel_id>[:<guild_id|->]') or text containing "
                "'(conversation <id>)'."
            ),
        ),
        conversation: Optional[str] = typer.Option(
            None,
            "--conversation",
            help=(
                "Conversation id or text containing '(conversation <id>)'. "
                "Use this when the value might be parsed as an option."
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
    ) -> None:
        """Trace conversation-scoped Discord log events and likely error lines."""
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
        log_paths = collect_log_paths(
            search_roots,
            config.log.path,
            backup_count=max(int(config.log.backup_count), 0),
            search_globs=_SEARCH_LOG_GLOBS,
        )
        if not log_paths:
            searched = ", ".join(
                str(root / ".codex-autorunner") for root in search_roots
            )
            raise_exit(f"No log files found under: {searched}")

        matches: list[LogTraceMatch] = []
        total_scanned_lines = 0
        read_errors: list[str] = []
        for log_path in log_paths:
            try:
                indexed_lines = read_log_lines(log_path, scan_lines)
            except OSError as exc:
                read_errors.append(f"{log_path}: {exc}")
                continue
            total_scanned_lines += len(indexed_lines)
            for index, (line_no, raw_line) in enumerate(indexed_lines):
                timestamp, level, message = split_log_line(raw_line)
                payload = parse_log_payload(message)
                if not _line_matches_conversation(raw_line, payload, target):
                    continue
                safe_payload = (
                    sanitize_payload_value(payload, _sanitize_trace_text)
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
                    LogTraceMatch(
                        path=log_path,
                        line_no=line_no,
                        timestamp=timestamp,
                        level=level,
                        event=event,
                        payload=(
                            safe_payload if isinstance(safe_payload, dict) else None
                        ),
                        raw_line=_sanitize_trace_text(raw_line),
                        is_error_candidate=is_error_candidate(
                            level=level,
                            event=event,
                            payload=payload,
                            message=message,
                        ),
                        context=context,
                    )
                )

        error_matches = [match for match in matches if match.is_error_candidate]
        recent_matches = matches[-limit:]
        recent_error_matches = error_matches[-limit:]

        if not matches:
            raise_exit(
                "No matches for conversation_id "
                f"{target.conversation_id}. Searched {len(log_paths)} log file(s)."
            )

        if json_output:

            def _serialize_match(match: LogTraceMatch) -> dict[str, Any]:
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
                        "channel_id": target.channel_id,
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
            f"(channel_id={target.channel_id}, thread_id={target.thread_id or '-'})"
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
                typer.echo(f"- {format_match_line(match)}")
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
            typer.echo(f"- {format_match_line(match)}")

    @app.command("register-commands")
    def discord_register_commands(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        """Register/sync Discord application commands with Discord API."""
        _require_discord_feature(require_optional_feature)
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        try:
            discord_raw = (
                config.raw.get("discord_bot") if isinstance(config.raw, dict) else {}
            )
            pma_enabled = _resolve_pma_enabled(config)
            discord_cfg = DiscordBotConfig.from_raw(
                root=config.root,
                raw=discord_raw if isinstance(discord_raw, dict) else {},
                pma_enabled=pma_enabled,
                collaboration_raw=(
                    config.raw.get("collaboration_policy")
                    if isinstance(config.raw, dict)
                    else None
                ),
            )
            if not discord_cfg.enabled:
                raise_exit("discord_bot is disabled; set discord_bot.enabled: true")
            asyncio.run(
                _sync_discord_application_commands(
                    discord_cfg,
                    logger=logging.getLogger("codex_autorunner.discord.commands"),
                )
            )
        except (DiscordBotConfigError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)

        typer.echo("Discord application commands synchronized.")
