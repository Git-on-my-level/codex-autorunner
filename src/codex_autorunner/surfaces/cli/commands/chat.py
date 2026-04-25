from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import typer

from ....core.config import ConfigError, load_hub_config
from ....core.orchestration.sqlite import resolve_orchestration_sqlite_path
from ....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from ....integrations.chat.dispatcher import conversation_id_for
from ....integrations.chat.queue_control import (
    ChatQueueControlStore,
    normalize_chat_thread_id,
)
from ....integrations.discord.config import (
    DEFAULT_BOT_TOKEN_ENV as DEFAULT_DISCORD_BOT_TOKEN_ENV,
)
from ....integrations.discord.rest import DiscordRestClient
from ....integrations.telegram.adapter import TelegramBotClient
from ....integrations.telegram.state_types import parse_topic_key
from ..hub_path_option import hub_root_path_option

_TELEGRAM_BOT_TOKEN_ENV = "CAR_TELEGRAM_BOT_TOKEN"
_DISCORD_CHANNEL_TYPES = {
    0: "text",
    1: "dm",
    2: "voice",
    3: "group_dm",
    4: "category",
    5: "announcement",
    10: "announcement_thread",
    11: "public_thread",
    12: "private_thread",
    13: "stage_voice",
    14: "directory",
    15: "forum",
    16: "media",
}


@dataclass(frozen=True)
class _ChatResolveTarget:
    platform: str
    chat_id: str
    thread_id: Optional[str]
    query: str

    @property
    def identifier(self) -> str:
        if self.platform == "telegram":
            return f"telegram:{self.chat_id}:{self.thread_id or 'root'}"
        return f"discord:{self.chat_id}"


def _raise_chat_exit(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _normalize_chat_lookup_token(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    head = cleaned.split()[0]
    return head.strip("()[]{}.,;\"'")


def _normalize_platform(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in {"discord", "telegram"}:
        raise typer.BadParameter("--platform must be one of: discord, telegram")
    return normalized


def _load_hub_raw_config(hub_root: Path) -> dict[str, Any]:
    try:
        config = load_hub_config(hub_root)
    except ConfigError:
        return {}
    raw = getattr(config, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _platform_env_name(raw_config: Mapping[str, Any], *, platform: str) -> str:
    if platform == "discord":
        section = raw_config.get("discord_bot")
        if isinstance(section, Mapping):
            value = str(
                section.get("bot_token_env", DEFAULT_DISCORD_BOT_TOKEN_ENV)
            ).strip()
            if value:
                return value
        return DEFAULT_DISCORD_BOT_TOKEN_ENV

    section = raw_config.get("telegram_bot")
    if isinstance(section, Mapping):
        value = str(section.get("bot_token_env", _TELEGRAM_BOT_TOKEN_ENV)).strip()
        if value:
            return value
    return _TELEGRAM_BOT_TOKEN_ENV


def _matching_directory_entry(
    entries: list[dict[str, Any]],
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
) -> Optional[dict[str, Any]]:
    for entry in entries:
        if entry.get("platform") != platform:
            continue
        if str(entry.get("chat_id") or "").strip() != chat_id:
            continue
        entry_thread = entry.get("thread_id")
        entry_thread_id = (
            str(entry_thread).strip()
            if entry_thread is not None and str(entry_thread).strip()
            else None
        )
        if entry_thread_id == thread_id:
            return entry
        if platform == "discord" and thread_id is None:
            return entry
    return None


def _preferred_platforms_for_bare_token(
    token: str,
    *,
    directory_entries: list[dict[str, Any]],
) -> tuple[str, ...]:
    if ":" in token:
        return ("telegram",)
    if token.startswith("-"):
        return ("telegram",)

    matches: list[str] = []
    for platform in ("discord", "telegram"):
        entry = _matching_directory_entry(
            directory_entries,
            platform=platform,
            chat_id=token,
            thread_id=None,
        )
        if entry is not None:
            matches.append(platform)
    if matches:
        return tuple(dict.fromkeys(matches))
    return ("discord", "telegram")


def _parse_explicit_target(
    *,
    platform: str,
    token: str,
    original_query: str,
) -> _ChatResolveTarget:
    if platform == "discord":
        body = token
        if body.startswith("discord:"):
            body = body[len("discord:") :]
        chat_id = body.split(":", 1)[0].strip()
        if not chat_id:
            raise typer.BadParameter(
                f"Could not parse Discord target from '{original_query}'."
            )
        return _ChatResolveTarget(
            platform="discord",
            chat_id=chat_id,
            thread_id=None,
            query=original_query,
        )

    body = token
    if body.startswith("telegram:"):
        body = body[len("telegram:") :]
    if not body:
        raise typer.BadParameter(
            f"Could not parse Telegram target from '{original_query}'."
        )
    if ":" in body:
        chat_id_int, thread_id_int, _scope = parse_topic_key(body)
        return _ChatResolveTarget(
            platform="telegram",
            chat_id=str(chat_id_int),
            thread_id=(str(thread_id_int) if thread_id_int is not None else None),
            query=original_query,
        )
    chat_id_value = int(body)
    return _ChatResolveTarget(
        platform="telegram",
        chat_id=str(chat_id_value),
        thread_id=None,
        query=original_query,
    )


def _expand_chat_resolve_targets(
    raw_targets: list[str],
    *,
    platform: Optional[str],
    directory_entries: list[dict[str, Any]],
) -> list[_ChatResolveTarget]:
    expanded: list[_ChatResolveTarget] = []
    for raw_target in raw_targets:
        token = _normalize_chat_lookup_token(raw_target)
        if not token:
            continue
        if platform is not None:
            expanded.append(
                _parse_explicit_target(
                    platform=platform,
                    token=token,
                    original_query=raw_target,
                )
            )
            continue
        if token.startswith("discord:"):
            expanded.append(
                _parse_explicit_target(
                    platform="discord",
                    token=token,
                    original_query=raw_target,
                )
            )
            continue
        if token.startswith("telegram:"):
            expanded.append(
                _parse_explicit_target(
                    platform="telegram",
                    token=token,
                    original_query=raw_target,
                )
            )
            continue
        for inferred_platform in _preferred_platforms_for_bare_token(
            token, directory_entries=directory_entries
        ):
            expanded.append(
                _parse_explicit_target(
                    platform=inferred_platform,
                    token=token,
                    original_query=raw_target,
                )
            )

    deduped: list[_ChatResolveTarget] = []
    seen: set[tuple[str, str, Optional[str]]] = set()
    for target in expanded:
        key = (target.platform, target.chat_id, target.thread_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _recent_notification_targets(hub_root: Path, *, limit: int) -> list[str]:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    if not db_path.exists():
        return []

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT surface_kind, surface_key
              FROM orch_notification_conversations
             WHERE surface_kind IN ('discord', 'telegram')
             ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    targets: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        surface_kind = str(row["surface_kind"] or "").strip().lower()
        surface_key = str(row["surface_key"] or "").strip()
        if surface_kind not in {"discord", "telegram"} or not surface_key:
            continue
        if surface_key.startswith("notification:"):
            continue
        key = (surface_kind, surface_key)
        if key in seen:
            continue
        seen.add(key)
        if surface_kind == "telegram":
            targets.append(f"telegram:{surface_key}")
        else:
            targets.append(f"discord:{surface_key}")
        if len(targets) >= limit:
            break
    return targets


def _discord_directory_metadata(entry: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    meta = entry.get("meta")
    return meta if isinstance(meta, dict) else {}


def _telegram_directory_metadata(entry: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    meta = entry.get("meta")
    return meta if isinstance(meta, dict) else {}


def _discord_type_name(payload: Mapping[str, Any]) -> Optional[str]:
    raw_type = payload.get("type")
    if not isinstance(raw_type, int):
        return None
    return _DISCORD_CHANNEL_TYPES.get(raw_type, str(raw_type))


def _discord_name_from_payload(payload: Mapping[str, Any]) -> Optional[str]:
    channel_type = payload.get("type")
    if channel_type == 1:
        recipients = payload.get("recipients")
        if isinstance(recipients, list):
            labels = []
            for item in recipients:
                if not isinstance(item, Mapping):
                    continue
                username = str(item.get("username") or "").strip()
                if username:
                    labels.append(f"@{username}")
            if labels:
                return "DM with " + ", ".join(labels)
        return "DM"
    name = str(payload.get("name") or "").strip()
    return name or None


def _discord_display_name(
    *,
    channel_payload: Mapping[str, Any],
    guild_payload: Optional[Mapping[str, Any]],
    directory_entry: Optional[Mapping[str, Any]],
) -> tuple[Optional[str], str]:
    channel_name = _discord_name_from_payload(channel_payload)
    guild_name = (
        str(guild_payload.get("name") or "").strip()
        if isinstance(guild_payload, Mapping)
        else ""
    )
    directory_display = (
        str(directory_entry.get("display") or "").strip()
        if isinstance(directory_entry, Mapping)
        else ""
    )
    channel_type = channel_payload.get("type")
    if channel_type == 1 and channel_name:
        return channel_name, channel_name
    if guild_name and channel_name:
        return channel_name, f"{guild_name} / #{channel_name}"
    if channel_name:
        return channel_name, f"#{channel_name}"
    if directory_display:
        return directory_display, directory_display
    fallback = f"discord:{str(channel_payload.get('id') or '').strip() or 'unknown'}"
    return None, fallback


def _telegram_display_name(
    *,
    chat_payload: Mapping[str, Any],
    target: _ChatResolveTarget,
    directory_entry: Optional[Mapping[str, Any]],
) -> tuple[Optional[str], str]:
    directory_meta = _telegram_directory_metadata(directory_entry)
    directory_display = (
        str(directory_entry.get("display") or "").strip()
        if isinstance(directory_entry, Mapping)
        else ""
    )
    if target.thread_id is not None:
        topic_title = str(directory_meta.get("topic_title") or "").strip()
        chat_title = str(chat_payload.get("title") or "").strip()
        if chat_title and topic_title:
            return topic_title, f"{chat_title} / {topic_title}"
        if directory_display:
            return topic_title or directory_display, directory_display

    title = str(chat_payload.get("title") or "").strip()
    username = str(chat_payload.get("username") or "").strip()
    first_name = str(chat_payload.get("first_name") or "").strip()
    last_name = str(chat_payload.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if title:
        return title, title
    if username:
        return username, f"DM with @{username}"
    if full_name:
        return full_name, f"DM with {full_name}"
    if directory_display:
        return directory_display, directory_display
    return None, target.identifier


def _fallback_resolution(
    *,
    target: _ChatResolveTarget,
    directory_entry: Optional[Mapping[str, Any]],
    error: Optional[str],
) -> dict[str, Any]:
    meta = (
        dict(directory_entry.get("meta") or {})
        if isinstance(directory_entry, Mapping)
        else {}
    )
    display = (
        str(directory_entry.get("display") or "").strip()
        if isinstance(directory_entry, Mapping)
        else ""
    )
    available = bool(display)
    result: dict[str, Any] = {
        "query": target.query,
        "identifier": target.identifier,
        "platform": target.platform,
        "id": target.chat_id,
        "thread_id": target.thread_id,
        "name": display or None,
        "display": display or target.identifier,
        "type": meta.get("chat_type"),
        "source": "directory" if available else "unavailable",
        "available": available,
        "error": None if available else error,
    }
    if target.platform == "discord":
        guild_id = meta.get("guild_id")
        if isinstance(guild_id, str) and guild_id.strip():
            result["guild_id"] = guild_id.strip()
    topic_title = meta.get("topic_title")
    if isinstance(topic_title, str) and topic_title.strip():
        result["topic_title"] = topic_title.strip()
    return result


async def _resolve_discord_target(
    *,
    target: _ChatResolveTarget,
    raw_config: Mapping[str, Any],
    directory_entry: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    token_env = _platform_env_name(raw_config, platform="discord")
    bot_token = os.environ.get(token_env)
    if not isinstance(bot_token, str) or not bot_token.strip():
        return _fallback_resolution(
            target=target,
            directory_entry=directory_entry,
            error=f"missing bot token env '{token_env}'",
        )

    try:
        async with DiscordRestClient(bot_token=bot_token.strip()) as client:
            channel_payload = await client.get_channel(channel_id=target.chat_id)
            guild_id = str(channel_payload.get("guild_id") or "").strip()
            guild_payload: Optional[dict[str, Any]] = None
            if guild_id:
                guild_payload = await client.get_guild(guild_id=guild_id)
    except Exception as exc:
        return _fallback_resolution(
            target=target,
            directory_entry=directory_entry,
            error=str(exc),
        )

    name, display = _discord_display_name(
        channel_payload=channel_payload,
        guild_payload=guild_payload,
        directory_entry=directory_entry,
    )
    result: dict[str, Any] = {
        "query": target.query,
        "identifier": target.identifier,
        "platform": "discord",
        "id": target.chat_id,
        "thread_id": None,
        "name": name,
        "display": display,
        "type": _discord_type_name(channel_payload),
        "source": "api+directory" if isinstance(directory_entry, Mapping) else "api",
        "available": True,
        "error": None,
        "parent_id": str(channel_payload.get("parent_id") or "").strip() or None,
        "topic": str(channel_payload.get("topic") or "").strip() or None,
    }
    guild_id = str(channel_payload.get("guild_id") or "").strip()
    if guild_id:
        result["guild_id"] = guild_id
    if isinstance(guild_payload, Mapping):
        guild_name = str(guild_payload.get("name") or "").strip()
        if guild_name:
            result["guild_name"] = guild_name
    return result


async def _resolve_telegram_target(
    *,
    target: _ChatResolveTarget,
    raw_config: Mapping[str, Any],
    directory_entry: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    token_env = _platform_env_name(raw_config, platform="telegram")
    bot_token = os.environ.get(token_env)
    if not isinstance(bot_token, str) or not bot_token.strip():
        return _fallback_resolution(
            target=target,
            directory_entry=directory_entry,
            error=f"missing bot token env '{token_env}'",
        )

    try:
        async with TelegramBotClient(bot_token.strip()) as client:
            chat_payload = await client.get_chat(chat_id=int(target.chat_id))
    except Exception as exc:
        return _fallback_resolution(
            target=target,
            directory_entry=directory_entry,
            error=str(exc),
        )

    name, display = _telegram_display_name(
        chat_payload=chat_payload,
        target=target,
        directory_entry=directory_entry,
    )
    directory_meta = _telegram_directory_metadata(directory_entry)
    result: dict[str, Any] = {
        "query": target.query,
        "identifier": target.identifier,
        "platform": "telegram",
        "id": target.chat_id,
        "thread_id": target.thread_id,
        "name": name,
        "display": display,
        "type": str(chat_payload.get("type") or "").strip() or None,
        "source": "api+directory" if isinstance(directory_entry, Mapping) else "api",
        "available": True,
        "error": None,
    }
    title = str(chat_payload.get("title") or "").strip()
    if title:
        result["title"] = title
    username = str(chat_payload.get("username") or "").strip()
    if username:
        result["username"] = username
    topic_title = str(directory_meta.get("topic_title") or "").strip()
    if topic_title:
        result["topic_title"] = topic_title
    return result


async def _resolve_chat_targets_async(
    *,
    targets: list[_ChatResolveTarget],
    raw_config: Mapping[str, Any],
    directory_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in targets:
        directory_entry = _matching_directory_entry(
            directory_entries,
            platform=target.platform,
            chat_id=target.chat_id,
            thread_id=target.thread_id,
        )
        if target.platform == "telegram":
            results.append(
                await _resolve_telegram_target(
                    target=target,
                    raw_config=raw_config,
                    directory_entry=directory_entry,
                )
            )
            continue
        results.append(
            await _resolve_discord_target(
                target=target,
                raw_config=raw_config,
                directory_entry=directory_entry,
            )
        )
    return results


def _format_resolve_text_line(result: Mapping[str, Any]) -> str:
    identifier = str(result.get("identifier") or "").strip()
    display = str(result.get("display") or "").strip()
    if not bool(result.get("available")):
        error = str(result.get("error") or "unavailable").strip()
        return f"{identifier}  (unavailable: {error})"

    details: list[str] = []
    type_name = str(result.get("type") or "").strip()
    if type_name:
        details.append(f"type:{type_name}")
    guild_name = str(result.get("guild_name") or "").strip()
    guild_id = str(result.get("guild_id") or "").strip()
    if guild_name:
        details.append(f"guild:{guild_name}")
    elif guild_id:
        details.append(f"guild_id:{guild_id}")
    topic_title = str(result.get("topic_title") or "").strip()
    if topic_title and topic_title not in display:
        details.append(f"topic:{topic_title}")
    source = str(result.get("source") or "").strip()
    if source:
        details.append(f"source:{source}")
    if details:
        return f"{identifier}  {display}  ({', '.join(details)})"
    return f"{identifier}  {display}"


def register_chat_commands(
    app: typer.Typer,
    *,
    resolve_hub_path: Callable[[Optional[Path]], Path],
) -> None:
    channels_app = typer.Typer(
        add_completion=False,
        help="Inspect cached chat channel/topic directory entries.",
    )
    queue_app = typer.Typer(
        add_completion=False,
        help="Inspect and remediate live per-conversation chat dispatch queues.",
    )
    app.add_typer(channels_app, name="channels")
    app.add_typer(queue_app, name="queue")

    @app.command("resolve")
    def chat_resolve(
        targets: list[str] = typer.Argument(
            None,
            help="Platform ids to resolve, e.g. discord:123 or telegram:-1001:77",
        ),
        platform: Optional[str] = typer.Option(
            None,
            "--platform",
            help="Force platform for bare ids (discord or telegram).",
        ),
        from_notifications: bool = typer.Option(
            False,
            "--from-notifications",
            help="Resolve recent notification destinations from orchestration state.",
        ),
        limit: int = typer.Option(
            20,
            "--limit",
            min=1,
            help="Maximum recent notification destinations when --from-notifications is used.",
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """Resolve chat ids to human-readable platform metadata."""
        normalized_platform = _normalize_platform(platform)
        normalized_format = str(output_format or "text").strip().lower()
        if normalized_format not in {"text", "json"}:
            raise typer.BadParameter("--format must be one of: text, json")

        hub_root = resolve_hub_path(path)
        raw_config = _load_hub_raw_config(hub_root)
        directory_entries = ChannelDirectoryStore(hub_root).list_entries(limit=None)

        raw_targets = list(targets or [])
        if from_notifications:
            raw_targets.extend(_recent_notification_targets(hub_root, limit=limit))
        if not raw_targets:
            _raise_chat_exit(
                "Provide one or more targets or pass --from-notifications."
            )

        try:
            expanded_targets = _expand_chat_resolve_targets(
                raw_targets,
                platform=normalized_platform,
                directory_entries=directory_entries,
            )
        except (ValueError, typer.BadParameter) as exc:
            _raise_chat_exit(str(exc))

        if not expanded_targets:
            _raise_chat_exit("No resolvable chat targets found.")

        results = asyncio.run(
            _resolve_chat_targets_async(
                targets=expanded_targets,
                raw_config=raw_config,
                directory_entries=directory_entries,
            )
        )

        payload = {"results": results}
        if normalized_format == "json":
            typer.echo(json.dumps(payload, indent=2))
            return

        for result in results:
            typer.echo(_format_resolve_text_line(result))

    @channels_app.command("list")
    def chat_channels_list(
        query: Optional[str] = typer.Option(
            None, "--query", help="Filter entries by substring"
        ),
        limit: int = typer.Option(100, "--limit", min=1, help="Maximum rows"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """List cached chat channels/topics discovered from inbound traffic."""
        hub_root = resolve_hub_path(path)
        store = ChannelDirectoryStore(hub_root)
        entries = store.list_entries(query=query, limit=limit)

        rows: list[dict[str, Any]] = []
        for entry in entries:
            key = channel_entry_key(entry)
            if not isinstance(key, str):
                continue
            rows.append(
                {
                    "key": key,
                    "display": entry.get("display"),
                    "seen_at": entry.get("seen_at"),
                    "meta": entry.get("meta"),
                    "entry": entry,
                }
            )

        if output_json:
            typer.echo(json.dumps({"entries": rows}, indent=2))
            return

        if not rows:
            typer.echo("No chat channel directory entries found.")
            return

        for row in rows:
            typer.echo(f"  {row['key']}")

    @queue_app.command("status")
    def chat_queue_status(
        channel: str = typer.Option(..., "--channel", help="Chat/channel id"),
        thread: Optional[str] = typer.Option(
            None, "--thread", help="Thread/topic id when applicable"
        ),
        platform: str = typer.Option(
            "discord", "--platform", help="Chat platform (default: discord)"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """Show persisted queue status for one channel/topic conversation."""
        hub_root = resolve_hub_path(path)
        conversation_id = conversation_id_for(
            str(platform or "").strip() or "discord",
            str(channel or "").strip(),
            normalize_chat_thread_id(thread),
        )
        store = ChatQueueControlStore(hub_root)
        snapshot = store.read_snapshot(conversation_id)

        payload = {
            "conversation_id": conversation_id,
            "platform": platform,
            "channel": channel,
            "thread": normalize_chat_thread_id(thread),
            "status": snapshot,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        if not isinstance(snapshot, dict):
            typer.echo(f"No queue state: {conversation_id}")
            return

        lines = [
            f"conversation={conversation_id}",
            f"pending={int(snapshot.get('pending_count') or 0)}",
            f"active={bool(snapshot.get('active'))}",
        ]
        active_update_id = snapshot.get("active_update_id")
        if isinstance(active_update_id, str) and active_update_id:
            lines.append(f"active_update_id={active_update_id}")
        active_started_at = snapshot.get("active_started_at")
        if isinstance(active_started_at, str) and active_started_at:
            lines.append(f"active_started_at={active_started_at}")
        updated_at = snapshot.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            lines.append(f"updated_at={updated_at}")
        typer.echo("\n".join(lines))

    @queue_app.command("reset")
    def chat_queue_reset(
        channel: str = typer.Option(..., "--channel", help="Chat/channel id"),
        thread: Optional[str] = typer.Option(
            None, "--thread", help="Thread/topic id when applicable"
        ),
        platform: str = typer.Option(
            "discord", "--platform", help="Chat platform (default: discord)"
        ),
        reason: Optional[str] = typer.Option(
            None, "--reason", help="Optional operator note for the reset request"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """Request a live queue reset for one channel/topic conversation."""
        hub_root = resolve_hub_path(path)
        normalized_platform = str(platform or "").strip() or "discord"
        normalized_channel = str(channel or "").strip()
        normalized_thread = normalize_chat_thread_id(thread)
        conversation_id = conversation_id_for(
            normalized_platform,
            normalized_channel,
            normalized_thread,
        )
        store = ChatQueueControlStore(hub_root)
        request = store.request_reset(
            conversation_id=conversation_id,
            platform=normalized_platform,
            chat_id=normalized_channel,
            thread_id=normalized_thread,
            reason=reason,
        )

        if output_json:
            typer.echo(json.dumps({"status": "ok", "reset_request": request}, indent=2))
            return

        typer.echo(f"Reset: {conversation_id}")
