"""Canonical command ingress parsing for interaction-style command payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandIngress:
    """Normalized command payload shape shared by chat adapters/services."""

    command_path: tuple[str, ...]
    options: dict[str, Any]

    @property
    def command(self) -> str:
        return ":".join(self.command_path)


def canonicalize_command_ingress(
    *,
    command: object | None = None,
    command_path: Sequence[object] | None = None,
    options: object | None = None,
) -> CommandIngress | None:
    """Normalize command ingress from tuple-path or colon-delimited string."""

    if command_path is not None:
        normalized_path = _normalize_command_path(command_path)
    else:
        normalized_path = _normalize_command_string(command)

    if not normalized_path:
        return None

    normalized_options: dict[str, Any] = {}
    if isinstance(options, Mapping):
        for key, value in options.items():
            if isinstance(key, str) and key:
                normalized_options[key] = value

    return CommandIngress(command_path=normalized_path, options=normalized_options)


def _normalize_command_string(command: object | None) -> tuple[str, ...]:
    if not isinstance(command, str):
        return ()
    parts = command.split(":")
    if not parts:
        return ()
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        return ()
    return _normalize_command_path(parts)


def _normalize_command_path(path: Sequence[object]) -> tuple[str, ...]:
    normalized: list[str] = []
    for part in path:
        if not isinstance(part, str):
            return ()
        token = part.strip()
        if not token:
            return ()
        normalized.append(token)
    return tuple(normalized)
