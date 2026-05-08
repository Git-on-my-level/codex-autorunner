from __future__ import annotations

from typing import Any

from .interaction_registry import (
    BOOLEAN,
    INTEGER,
    STRING,
    SUB_COMMAND,
    SUB_COMMAND_GROUP,
)
from .interaction_registry import (
    build_application_commands as build_registry_application_commands,
)


def build_application_commands(context: Any = None) -> list[dict[str, Any]]:
    return build_registry_application_commands(context)


__all__ = [
    "BOOLEAN",
    "INTEGER",
    "STRING",
    "SUB_COMMAND",
    "SUB_COMMAND_GROUP",
    "build_application_commands",
]
