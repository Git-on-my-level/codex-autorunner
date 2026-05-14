from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.orchestration.sqlite import open_orchestration_sqlite
from ...core.sqlite_utils import table_columns
from ..chat.surface_resolver import (
    DiscordSurfaceResolver,
    SurfaceInfo,
    build_surface_resolvers,
    close_surface_resolvers,
    resolve_surface_key,
)

_UNAVAILABLE = "(unavailable)"
_DM_GUILD_SORT = "\uffff"


@dataclass(frozen=True)
class DiscordChannelBinding:
    channel_id: str
    binding_id: Optional[str]
    thread_target_id: Optional[str]
    agent_id: Optional[str]
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    workspace_root: Optional[str]
    display_name: Optional[str]
    lifecycle_status: Optional[str]
    runtime_status: Optional[str]
    disabled_at: Optional[str]
    updated_at: Optional[str]


@dataclass(frozen=True)
class DiscordChannelRow:
    channel_id: str
    name: str
    type: str
    guild_id: Optional[str]
    guild: str
    bound_thread: str
    available: bool
    error: Optional[str]
    bindings: tuple[DiscordChannelBinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "type": self.type,
            "guild_id": self.guild_id,
            "guild": None if self.guild == "-" else self.guild,
            "bound_thread": self.bound_thread or None,
            "available": self.available,
            "error": self.error,
            "bindings": [
                {
                    "binding_id": binding.binding_id,
                    "thread_target_id": binding.thread_target_id,
                    "agent_id": binding.agent_id,
                    "repo_id": binding.repo_id,
                    "resource_kind": binding.resource_kind,
                    "resource_id": binding.resource_id,
                    "workspace_root": binding.workspace_root,
                    "display_name": binding.display_name,
                    "lifecycle_status": binding.lifecycle_status,
                    "runtime_status": binding.runtime_status,
                    "disabled_at": binding.disabled_at,
                    "updated_at": binding.updated_at,
                }
                for binding in self.bindings
            ],
        }


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _select_expr(columns: set[str], alias: str, column: str) -> str:
    if column in columns:
        return f"{alias}.{column}"
    return f"NULL AS {column}"


def list_discord_channel_bindings(hub_root: Path) -> list[DiscordChannelBinding]:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=False) as conn:
        binding_columns = table_columns(conn, "orch_bindings")
        thread_columns = table_columns(conn, "orch_thread_targets")
        if not binding_columns:
            return []
        rows = conn.execute(
            f"""
            SELECT
                b.binding_id,
                b.surface_key AS channel_id,
                b.target_id AS thread_target_id,
                COALESCE(b.agent_id, t.agent_id) AS agent_id,
                COALESCE(b.repo_id, t.repo_id) AS repo_id,
                {_select_expr(binding_columns, 'b', 'resource_kind')},
                {_select_expr(binding_columns, 'b', 'resource_id')},
                {_select_expr(thread_columns, 't', 'workspace_root')},
                {_select_expr(thread_columns, 't', 'display_name')},
                {_select_expr(thread_columns, 't', 'lifecycle_status')},
                {_select_expr(thread_columns, 't', 'runtime_status')},
                b.disabled_at,
                b.updated_at
              FROM orch_bindings AS b
         LEFT JOIN orch_thread_targets AS t
                ON t.thread_target_id = b.target_id
             WHERE b.surface_kind = 'discord'
          ORDER BY b.updated_at DESC, b.created_at DESC
            """
        ).fetchall()
    bindings: list[DiscordChannelBinding] = []
    for row in rows:
        channel_id = _normalize_text(row["channel_id"])
        if channel_id is None:
            continue
        bindings.append(
            DiscordChannelBinding(
                channel_id=channel_id,
                binding_id=_normalize_text(row["binding_id"]),
                thread_target_id=_normalize_text(row["thread_target_id"]),
                agent_id=_normalize_text(row["agent_id"]),
                repo_id=_normalize_text(row["repo_id"]),
                resource_kind=_normalize_text(row["resource_kind"]),
                resource_id=_normalize_text(row["resource_id"]),
                workspace_root=_normalize_text(row["workspace_root"]),
                display_name=_normalize_text(row["display_name"]),
                lifecycle_status=_normalize_text(row["lifecycle_status"]),
                runtime_status=_normalize_text(row["runtime_status"]),
                disabled_at=_normalize_text(row["disabled_at"]),
                updated_at=_normalize_text(row["updated_at"]),
            )
        )
    return bindings


def _binding_owner(binding: DiscordChannelBinding) -> str:
    if binding.repo_id:
        return binding.repo_id
    if binding.resource_kind and binding.resource_id:
        return binding.resource_id
    if binding.workspace_root:
        return Path(binding.workspace_root).name or binding.workspace_root
    return "hub"


def _is_active_binding(binding: DiscordChannelBinding) -> bool:
    return binding.disabled_at is None and binding.lifecycle_status != "archived"


def _bound_thread_label(bindings: tuple[DiscordChannelBinding, ...]) -> str:
    active = [binding for binding in bindings if _is_active_binding(binding)]
    if not active:
        return ""
    binding = active[0]
    thread_id = binding.thread_target_id or ""
    agent = binding.agent_id or "unknown"
    return f"{thread_id[:8]} ({agent}/{_binding_owner(binding)})"


def _group_bindings(
    bindings: list[DiscordChannelBinding],
) -> dict[str, tuple[DiscordChannelBinding, ...]]:
    grouped: dict[str, list[DiscordChannelBinding]] = {}
    for binding in bindings:
        grouped.setdefault(binding.channel_id, []).append(binding)
    return {
        channel_id: tuple(channel_bindings)
        for channel_id, channel_bindings in grouped.items()
    }


async def build_discord_channel_rows(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    bound_only: bool = False,
    guild_id: Optional[str] = None,
) -> list[DiscordChannelRow]:
    try:
        bindings = list_discord_channel_bindings(hub_root)
    except sqlite3.Error:
        bindings = []

    grouped = _group_bindings(bindings)
    channel_ids = sorted(grouped)
    resolvers = build_surface_resolvers(raw_config)
    resolver = resolvers.get("discord")
    try:
        resolved = await _resolve_discord_infos(
            channel_ids=channel_ids,
            resolver=resolver if isinstance(resolver, DiscordSurfaceResolver) else None,
        )
    finally:
        await close_surface_resolvers(resolvers)

    rows: list[DiscordChannelRow] = []
    for channel_id in channel_ids:
        channel_bindings = grouped[channel_id]
        if bound_only and not any(
            _is_active_binding(binding) for binding in channel_bindings
        ):
            continue
        info = resolved.get(channel_id)
        payload_guild_id = info.parent_id if info is not None else None
        if guild_id and payload_guild_id != guild_id:
            continue
        rows.append(
            DiscordChannelRow(
                channel_id=channel_id,
                name=info.name if info is not None else _UNAVAILABLE,
                type=info.surface_type if info is not None else "-",
                guild_id=payload_guild_id,
                guild=info.parent_name or "-" if info is not None else _UNAVAILABLE,
                bound_thread=_bound_thread_label(channel_bindings),
                available=info is not None,
                error=None if info is not None else "not resolved",
                bindings=channel_bindings,
            )
        )

    return sorted(rows, key=_channel_sort_key)


async def _resolve_discord_infos(
    *,
    channel_ids: list[str],
    resolver: Optional[DiscordSurfaceResolver],
) -> dict[str, Optional[SurfaceInfo]]:
    if resolver is None:
        return {channel_id: None for channel_id in channel_ids}

    async def resolve_one(channel_id: str) -> None:
        resolved[channel_id] = await resolve_surface_key(
            {"discord": resolver},
            surface_kind="discord",
            surface_key=channel_id,
        )

    resolved: dict[str, Optional[SurfaceInfo]] = {}
    await asyncio.gather(*(resolve_one(channel_id) for channel_id in channel_ids))
    return resolved


def _channel_sort_key(row: DiscordChannelRow) -> tuple[str, str, str]:
    guild = _DM_GUILD_SORT if row.type == "dm" else row.guild.lower()
    return (guild, row.name.lower(), row.channel_id)


def rows_to_json(rows: list[DiscordChannelRow]) -> str:
    return json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=False)


def rows_to_table(rows: list[DiscordChannelRow]) -> str:
    if not rows:
        return "No Discord channels found"
    headers = ("CHANNEL ID", "NAME", "TYPE", "GUILD", "BOUND THREAD")
    body = [
        (
            row.channel_id,
            row.name,
            row.type,
            row.guild,
            row.bound_thread or "-",
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(item[index]) for item in body))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(item))
        for item in body
    )
    return "\n".join(lines)


__all__ = [
    "DiscordChannelBinding",
    "DiscordChannelRow",
    "build_discord_channel_rows",
    "list_discord_channel_bindings",
    "rows_to_json",
    "rows_to_table",
]
