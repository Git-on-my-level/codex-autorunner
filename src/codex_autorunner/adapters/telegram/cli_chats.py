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
    SurfaceInfo,
    build_surface_resolvers,
    close_surface_resolvers,
    resolve_surface_key,
)

_UNAVAILABLE = "(unavailable)"


@dataclass(frozen=True)
class TelegramChatBinding:
    chat_key: str
    binding_id: Optional[str]
    thread_target_id: Optional[str]
    agent_id: Optional[str]
    repo_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    workspace_root: Optional[str]
    lifecycle_status: Optional[str]
    disabled_at: Optional[str]


@dataclass(frozen=True)
class TelegramChatRow:
    chat_key: str
    name: str
    type: str
    bound_thread: str
    available: bool
    error: Optional[str]
    bindings: tuple[TelegramChatBinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_key": self.chat_key,
            "name": self.name,
            "type": self.type,
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
                    "lifecycle_status": binding.lifecycle_status,
                    "disabled_at": binding.disabled_at,
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


def list_telegram_chat_bindings(hub_root: Path) -> list[TelegramChatBinding]:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=False) as conn:
        binding_columns = table_columns(conn, "orch_bindings")
        thread_columns = table_columns(conn, "orch_thread_targets")
        if not binding_columns:
            return []
        rows = conn.execute(
            f"""
            SELECT
                b.binding_id,
                b.surface_key AS chat_key,
                b.target_id AS thread_target_id,
                COALESCE(b.agent_id, t.agent_id) AS agent_id,
                COALESCE(b.repo_id, t.repo_id) AS repo_id,
                {_select_expr(binding_columns, 'b', 'resource_kind')},
                {_select_expr(binding_columns, 'b', 'resource_id')},
                {_select_expr(thread_columns, 't', 'workspace_root')},
                {_select_expr(thread_columns, 't', 'lifecycle_status')},
                b.disabled_at
              FROM orch_bindings AS b
         LEFT JOIN orch_thread_targets AS t
                ON t.thread_target_id = b.target_id
             WHERE b.surface_kind = 'telegram'
          ORDER BY b.updated_at DESC, b.created_at DESC
            """
        ).fetchall()
    bindings: list[TelegramChatBinding] = []
    for row in rows:
        chat_key = _normalize_text(row["chat_key"])
        if chat_key is None:
            continue
        bindings.append(
            TelegramChatBinding(
                chat_key=chat_key,
                binding_id=_normalize_text(row["binding_id"]),
                thread_target_id=_normalize_text(row["thread_target_id"]),
                agent_id=_normalize_text(row["agent_id"]),
                repo_id=_normalize_text(row["repo_id"]),
                resource_kind=_normalize_text(row["resource_kind"]),
                resource_id=_normalize_text(row["resource_id"]),
                workspace_root=_normalize_text(row["workspace_root"]),
                lifecycle_status=_normalize_text(row["lifecycle_status"]),
                disabled_at=_normalize_text(row["disabled_at"]),
            )
        )
    return bindings


async def build_telegram_chat_rows(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    bound_only: bool = False,
) -> list[TelegramChatRow]:
    try:
        bindings = list_telegram_chat_bindings(hub_root)
    except sqlite3.Error:
        bindings = []
    grouped = _group_bindings(bindings)
    chat_keys = sorted(grouped)
    resolvers = build_surface_resolvers(raw_config)
    try:
        resolved = await _resolve_telegram_infos(
            chat_keys=chat_keys, resolvers=resolvers
        )
    finally:
        await close_surface_resolvers(resolvers)
    rows: list[TelegramChatRow] = []
    for chat_key in chat_keys:
        chat_bindings = grouped[chat_key]
        if bound_only and not any(
            _is_active_binding(binding) for binding in chat_bindings
        ):
            continue
        info = resolved.get(chat_key)
        rows.append(
            TelegramChatRow(
                chat_key=chat_key,
                name=info.name if info is not None else _UNAVAILABLE,
                type=info.surface_type if info is not None else "-",
                bound_thread=_bound_thread_label(chat_bindings),
                available=info is not None,
                error=None if info is not None else "not resolved",
                bindings=chat_bindings,
            )
        )
    return sorted(rows, key=lambda row: (row.name.lower(), row.chat_key))


async def _resolve_telegram_infos(
    *,
    chat_keys: list[str],
    resolvers: Mapping[str, Any],
) -> dict[str, Optional[SurfaceInfo]]:
    async def resolve_one(chat_key: str) -> None:
        resolved[chat_key] = await resolve_surface_key(
            resolvers,
            surface_kind="telegram",
            surface_key=chat_key,
        )

    resolved: dict[str, Optional[SurfaceInfo]] = {}
    await asyncio.gather(*(resolve_one(chat_key) for chat_key in chat_keys))
    return resolved


def _group_bindings(
    bindings: list[TelegramChatBinding],
) -> dict[str, tuple[TelegramChatBinding, ...]]:
    grouped: dict[str, list[TelegramChatBinding]] = {}
    for binding in bindings:
        grouped.setdefault(binding.chat_key, []).append(binding)
    return {chat_key: tuple(items) for chat_key, items in grouped.items()}


def _binding_owner(binding: TelegramChatBinding) -> str:
    if binding.repo_id:
        return binding.repo_id
    if binding.resource_kind and binding.resource_id:
        return binding.resource_id
    if binding.workspace_root:
        return Path(binding.workspace_root).name or binding.workspace_root
    return "hub"


def _is_active_binding(binding: TelegramChatBinding) -> bool:
    return binding.disabled_at is None and binding.lifecycle_status != "archived"


def _bound_thread_label(bindings: tuple[TelegramChatBinding, ...]) -> str:
    active = [binding for binding in bindings if _is_active_binding(binding)]
    if not active:
        return ""
    binding = active[0]
    thread_id = binding.thread_target_id or ""
    agent = binding.agent_id or "unknown"
    return f"{thread_id[:8]} ({agent}/{_binding_owner(binding)})"


def rows_to_json(rows: list[TelegramChatRow]) -> str:
    return json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=False)


def rows_to_table(rows: list[TelegramChatRow]) -> str:
    if not rows:
        return "No Telegram chats found"
    headers = ("CHAT KEY", "NAME", "TYPE", "BOUND THREAD")
    body = [
        (
            row.chat_key,
            row.name,
            row.type,
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
    "TelegramChatBinding",
    "TelegramChatRow",
    "build_telegram_chat_rows",
    "list_telegram_chat_bindings",
    "rows_to_json",
    "rows_to_table",
]
