from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..chat.commands import parse_chat_command
from .handlers.commands import CommandSpec


@dataclass(frozen=True)
class TelegramCommandDiff:
    added: list[str]
    removed: list[str]
    changed: list[str]
    order_changed: bool

    @property
    def needs_update(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.order_changed)


def build_command_payloads(
    command_specs: Mapping[str, CommandSpec],
) -> tuple[list[dict[str, str]], list[str]]:
    commands: list[dict[str, str]] = []
    invalid: list[str] = []
    for spec in command_specs.values():
        name = _validate_command_name(spec.name)
        if not name:
            invalid.append(spec.name)
            continue
        description = _normalize_description(spec.description)
        if not description:
            description = name
        commands.append({"command": name, "description": description})
    return commands, invalid


def diff_command_lists(
    desired: Iterable[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
) -> TelegramCommandDiff:
    desired_norm = _normalize_payloads(desired)
    current_norm = _normalize_payloads(current)

    desired_map = _payload_map(desired_norm)
    current_map = _payload_map(current_norm)

    desired_order = [name for name, _desc in desired_norm]
    current_order = [name for name, _desc in current_norm]

    added = [name for name in desired_order if name not in current_map]
    removed = [name for name in current_order if name not in desired_map]
    changed = [
        name
        for name in desired_order
        if name in current_map and desired_map.get(name) != current_map.get(name)
    ]

    order_changed = False
    if not (added or removed or changed):
        filtered_current_order = [name for name in current_order if name in desired_map]
        order_changed = desired_order != filtered_current_order

    return TelegramCommandDiff(
        added=added,
        removed=removed,
        changed=changed,
        order_changed=order_changed,
    )


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _validate_command_name(name: str) -> Optional[str]:
    normalized = _normalize_name(name)
    if not normalized:
        return None
    # Reuse shared slash-command grammar for name validation.
    parsed = parse_chat_command(f"/{normalized}")
    if parsed is None:
        return None
    return parsed.name


def _normalize_description(description: str) -> str:
    return description.strip()


def _normalize_payloads(
    commands: Iterable[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    for item in commands:
        command = item.get("command")
        description = item.get("description")
        if not isinstance(command, str) or not isinstance(description, str):
            continue
        name = _normalize_name(command)
        if not name:
            continue
        normalized.append((name, _normalize_description(description)))
    return normalized


def _payload_map(commands: Sequence[tuple[str, str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name, description in commands:
        if name not in mapping:
            mapping[name] = description
    return mapping
