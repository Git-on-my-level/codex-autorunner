from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional


def _normalize_optional_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class RuntimeThreadBinding:
    backend_thread_id: Optional[str]
    backend_runtime_instance_id: Optional[str] = None


_BINDINGS_LOCK = Lock()
_RUNTIME_THREAD_BINDINGS: dict[tuple[str, str], RuntimeThreadBinding] = {}


def _binding_key(hub_root: Path, thread_target_id: str) -> Optional[tuple[str, str]]:
    normalized_thread_target_id = _normalize_optional_text(thread_target_id)
    if normalized_thread_target_id is None:
        return None
    return str(hub_root.resolve()), normalized_thread_target_id


def get_runtime_thread_binding(
    hub_root: Path, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    key = _binding_key(hub_root, thread_target_id)
    if key is None:
        return None
    with _BINDINGS_LOCK:
        return _RUNTIME_THREAD_BINDINGS.get(key)


def set_runtime_thread_binding(
    hub_root: Path,
    thread_target_id: str,
    *,
    backend_thread_id: Optional[str],
    backend_runtime_instance_id: Optional[str] = None,
) -> None:
    key = _binding_key(hub_root, thread_target_id)
    if key is None:
        return
    normalized_backend_thread_id = _normalize_optional_text(backend_thread_id)
    normalized_runtime_instance_id = _normalize_optional_text(
        backend_runtime_instance_id
    )
    with _BINDINGS_LOCK:
        if normalized_backend_thread_id is None:
            _RUNTIME_THREAD_BINDINGS.pop(key, None)
            return
        _RUNTIME_THREAD_BINDINGS[key] = RuntimeThreadBinding(
            backend_thread_id=normalized_backend_thread_id,
            backend_runtime_instance_id=normalized_runtime_instance_id,
        )


def clear_runtime_thread_binding(hub_root: Path, thread_target_id: str) -> None:
    set_runtime_thread_binding(
        hub_root,
        thread_target_id,
        backend_thread_id=None,
        backend_runtime_instance_id=None,
    )


def clear_runtime_thread_bindings_for_hub_root(hub_root: Path) -> None:
    normalized_hub_root = str(hub_root.resolve())
    with _BINDINGS_LOCK:
        keys = [
            key for key in _RUNTIME_THREAD_BINDINGS if key[0] == normalized_hub_root
        ]
        for key in keys:
            _RUNTIME_THREAD_BINDINGS.pop(key, None)


__all__ = [
    "RuntimeThreadBinding",
    "clear_runtime_thread_binding",
    "clear_runtime_thread_bindings_for_hub_root",
    "get_runtime_thread_binding",
    "set_runtime_thread_binding",
]
