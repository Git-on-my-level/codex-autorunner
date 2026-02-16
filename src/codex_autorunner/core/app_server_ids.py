"""Compatibility shim for app-server ID helpers."""

from importlib import import_module
from typing import Any, Optional, cast


def _ids_module() -> Any:
    return import_module("codex_autorunner.integrations.app_server.ids")


def extract_turn_id(payload: Any) -> Optional[str]:
    return cast(Optional[str], _ids_module().extract_turn_id(payload))


def extract_thread_id_for_turn(payload: Any) -> Optional[str]:
    return cast(Optional[str], _ids_module().extract_thread_id_for_turn(payload))


def extract_thread_id(payload: Any) -> Optional[str]:
    return cast(Optional[str], _ids_module().extract_thread_id(payload))


__all__ = [
    "extract_turn_id",
    "extract_thread_id_for_turn",
    "extract_thread_id",
]
