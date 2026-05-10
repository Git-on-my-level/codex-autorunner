"""Shared command-semantic lookup for chat surfaces.

This module is the adapter-layer command kernel: Telegram resolves command
families here, while Discord resolves canonical slash paths here. Surface
modules should keep transport-specific parsing and UX, but shared command
identity and workspace semantics belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from .command_contract import COMMAND_CONTRACT, CommandContractEntry, CommandStatus


@dataclass(frozen=True)
class TelegramCommandKernelEntry:
    name: str
    contract_ids: tuple[str, ...]
    canonical_paths: tuple[tuple[str, ...], ...]
    statuses: tuple[CommandStatus, ...]
    any_requires_bound_workspace: bool
    all_require_bound_workspace: bool


@dataclass(frozen=True)
class DiscordCommandKernelEntry:
    contract_id: str
    canonical_path: tuple[str, ...]
    discord_path: tuple[str, ...]
    status: CommandStatus
    requires_bound_workspace: bool


def _normalize_telegram_command_name(name: str) -> str:
    return str(name or "").strip().lower()


def _normalize_discord_command_path(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        str(part or "").strip().lower() for part in path if str(part or "").strip()
    )


@lru_cache(maxsize=1)
def _telegram_command_index() -> dict[str, TelegramCommandKernelEntry]:
    grouped: dict[str, list[CommandContractEntry]] = {}
    for entry in COMMAND_CONTRACT:
        for name in entry.telegram_commands:
            normalized = _normalize_telegram_command_name(name)
            if not normalized:
                continue
            grouped.setdefault(normalized, []).append(entry)
    index: dict[str, TelegramCommandKernelEntry] = {}
    for name, entries in grouped.items():
        index[name] = TelegramCommandKernelEntry(
            name=name,
            contract_ids=tuple(entry.id for entry in entries),
            canonical_paths=tuple(entry.path for entry in entries),
            statuses=tuple(entry.status for entry in entries),
            any_requires_bound_workspace=any(
                entry.requires_bound_workspace for entry in entries
            ),
            all_require_bound_workspace=all(
                entry.requires_bound_workspace for entry in entries
            ),
        )
    return index


@lru_cache(maxsize=1)
def _discord_command_index() -> dict[tuple[str, ...], DiscordCommandKernelEntry]:
    index: dict[tuple[str, ...], DiscordCommandKernelEntry] = {}
    for entry in COMMAND_CONTRACT:
        for path in entry.discord_paths:
            normalized = _normalize_discord_command_path(path)
            if not normalized:
                continue
            index[normalized] = DiscordCommandKernelEntry(
                contract_id=entry.id,
                canonical_path=entry.path,
                discord_path=normalized,
                status=entry.status,
                requires_bound_workspace=entry.requires_bound_workspace,
            )
    return index


def telegram_command_kernel_entry(
    name: str,
    *,
    contract: Sequence[CommandContractEntry] = COMMAND_CONTRACT,
) -> TelegramCommandKernelEntry | None:
    if contract is not COMMAND_CONTRACT:
        grouped: dict[str, list[CommandContractEntry]] = {}
        for entry in contract:
            for command_name in entry.telegram_commands:
                normalized = _normalize_telegram_command_name(command_name)
                if normalized:
                    grouped.setdefault(normalized, []).append(entry)
        normalized_name = _normalize_telegram_command_name(name)
        entries = grouped.get(normalized_name)
        if not entries:
            return None
        return TelegramCommandKernelEntry(
            name=normalized_name,
            contract_ids=tuple(entry.id for entry in entries),
            canonical_paths=tuple(entry.path for entry in entries),
            statuses=tuple(entry.status for entry in entries),
            any_requires_bound_workspace=any(
                entry.requires_bound_workspace for entry in entries
            ),
            all_require_bound_workspace=all(
                entry.requires_bound_workspace for entry in entries
            ),
        )
    return _telegram_command_index().get(_normalize_telegram_command_name(name))


def discord_command_kernel_entry(
    command_path: tuple[str, ...],
    *,
    contract: Sequence[CommandContractEntry] = COMMAND_CONTRACT,
) -> DiscordCommandKernelEntry | None:
    normalized_path = _normalize_discord_command_path(command_path)
    if contract is not COMMAND_CONTRACT:
        for entry in contract:
            for path in entry.discord_paths:
                if _normalize_discord_command_path(path) == normalized_path:
                    return DiscordCommandKernelEntry(
                        contract_id=entry.id,
                        canonical_path=entry.path,
                        discord_path=normalized_path,
                        status=entry.status,
                        requires_bound_workspace=entry.requires_bound_workspace,
                    )
        return None
    return _discord_command_index().get(normalized_path)


__all__ = [
    "DiscordCommandKernelEntry",
    "TelegramCommandKernelEntry",
    "discord_command_kernel_entry",
    "telegram_command_kernel_entry",
]
