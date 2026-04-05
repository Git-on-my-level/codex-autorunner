from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast

from .locks import file_lock
from .time_utils import now_iso
from .utils import atomic_write

_logger = logging.getLogger(__name__)

PMA_PROMPT_STATE_FILENAME = "prompt_state.json"
PMA_PROMPT_STATE_VERSION = 1
PMA_PROMPT_STATE_MAX_SESSIONS = 200
PMA_PROMPT_DIGEST_PREVIEW = 12

PMA_PROMPT_SECTION_ORDER = (
    "prompt",
    "discoverability",
    "fastpath",
    "agents",
    "active_context",
    "context_log_tail",
    "hub_snapshot",
)


def default_pma_prompt_state_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_PROMPT_STATE_FILENAME


def _default_pma_prompt_state() -> dict[str, Any]:
    return {
        "version": PMA_PROMPT_STATE_VERSION,
        "sessions": {},
        "updated_at": now_iso(),
    }


def _prompt_state_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _digest_text(value: Any) -> str:
    raw = value if isinstance(value, str) else str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest_preview(digest: Any) -> str:
    if not isinstance(digest, str):
        return ""
    return digest[:PMA_PROMPT_DIGEST_PREVIEW]


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def _build_prompt_bundle_digest(sections: Mapping[str, Mapping[str, Any]]) -> str:
    payload = {
        name: str((sections.get(name) or {}).get("digest") or "")
        for name in PMA_PROMPT_SECTION_ORDER
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return _digest_text(raw)


def _read_pma_prompt_state_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_pma_prompt_state()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        _logger.warning("Could not read PMA prompt state: %s", exc)
        return _default_pma_prompt_state()
    return data if isinstance(data, dict) else _default_pma_prompt_state()


def _write_pma_prompt_state_unlocked(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _validate_prompt_session_record(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    sections = record.get("sections")
    if not isinstance(sections, Mapping):
        return False
    for name in PMA_PROMPT_SECTION_ORDER:
        section = sections.get(name)
        if not isinstance(section, Mapping):
            return False
        if not _is_digest(section.get("digest")):
            return False
    bundle_digest = record.get("bundle_digest")
    if not _is_digest(bundle_digest):
        return False
    return bundle_digest == _build_prompt_bundle_digest(
        cast(Mapping[str, Mapping[str, Any]], sections)
    )


def _trim_prompt_sessions(sessions: Mapping[str, Any]) -> dict[str, Any]:
    items: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in sessions.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(value, Mapping):
            continue
        items.append((key, value))
    if len(items) <= PMA_PROMPT_STATE_MAX_SESSIONS:
        return {key: dict(value) for key, value in items}

    def _sort_key(item: tuple[str, Mapping[str, Any]]) -> tuple[str, str]:
        updated_at = str(item[1].get("updated_at") or "")
        return (updated_at, item[0])

    trimmed = sorted(items, key=_sort_key)[-PMA_PROMPT_STATE_MAX_SESSIONS:]
    return {key: dict(value) for key, value in trimmed}


def clear_pma_prompt_state_sessions(
    hub_root: Path,
    *,
    keys: Sequence[str] = (),
    prefixes: Sequence[str] = (),
    exclude_prefixes: Sequence[str] = (),
) -> list[str]:
    normalized_keys = {
        str(key).strip() for key in keys if isinstance(key, str) and key.strip()
    }
    normalized_prefixes = tuple(
        str(prefix).strip()
        for prefix in prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    normalized_excludes = tuple(
        str(prefix).strip()
        for prefix in exclude_prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    if not normalized_keys and not normalized_prefixes:
        return []

    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    cleared_keys: list[str] = []

    def _is_excluded(session_key: str) -> bool:
        return any(
            session_key == excluded.rstrip(".") or session_key.startswith(excluded)
            for excluded in normalized_excludes
        )

    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if not isinstance(sessions, Mapping):
            return []

        updated_sessions = dict(sessions)
        for session_key in tuple(updated_sessions.keys()):
            if not isinstance(session_key, str) or not session_key:
                continue
            key_match = session_key in normalized_keys
            prefix_match = bool(normalized_prefixes) and any(
                session_key.startswith(prefix) for prefix in normalized_prefixes
            )
            if not key_match and not prefix_match:
                continue
            if _is_excluded(session_key):
                continue
            updated_sessions.pop(session_key, None)
            cleared_keys.append(session_key)

        if cleared_keys:
            state["version"] = PMA_PROMPT_STATE_VERSION
            state["updated_at"] = now_iso()
            state["sessions"] = _trim_prompt_sessions(updated_sessions)
            _write_pma_prompt_state_unlocked(path, state)

    return sorted(cleared_keys)


def list_pma_prompt_state_session_keys(hub_root: Path) -> list[str]:
    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if not isinstance(sessions, Mapping):
            return []
        return sorted(
            key for key in sessions.keys() if isinstance(key, str) and key.strip()
        )


def _merge_prompt_session_state(
    hub_root: Path,
    *,
    prompt_state_key: str,
    sections: Mapping[str, Mapping[str, str]],
    force_full_context: bool,
) -> tuple[bool, str, Optional[Mapping[str, Any]], Optional[str]]:
    path = default_pma_prompt_state_path(hub_root)
    lock_path = _prompt_state_lock_path(path)
    use_delta = False
    delta_reason = "first_turn"
    prior_sections: Optional[Mapping[str, Any]] = None
    prior_updated_at: Optional[str] = None

    with file_lock(lock_path):
        state = _read_pma_prompt_state_unlocked(path)
        sessions = state.get("sessions")
        if isinstance(sessions, Mapping):
            prior_record = sessions.get(prompt_state_key)
            if _validate_prompt_session_record(prior_record):
                validated_record = cast(Mapping[str, Any], prior_record)
                prior_sections = cast(
                    Optional[Mapping[str, Any]], validated_record.get("sections")
                )
                prior_updated_at = cast(
                    Optional[str], validated_record.get("updated_at")
                )
                if force_full_context:
                    delta_reason = "explicit_refresh"
                else:
                    use_delta = True
                    delta_reason = "cached_context"
            elif prior_record is not None:
                delta_reason = "digest_mismatch"

        updated_sessions = dict(sessions) if isinstance(sessions, Mapping) else {}
        timestamp = now_iso()
        updated_sessions[prompt_state_key] = {
            "version": PMA_PROMPT_STATE_VERSION,
            "updated_at": timestamp,
            "bundle_digest": _build_prompt_bundle_digest(sections),
            "sections": {
                name: {"digest": str(payload.get("digest") or "")}
                for name, payload in sections.items()
            },
        }
        state["version"] = PMA_PROMPT_STATE_VERSION
        state["updated_at"] = timestamp
        state["sessions"] = _trim_prompt_sessions(updated_sessions)
        _write_pma_prompt_state_unlocked(path, state)

    return use_delta, delta_reason, prior_sections, prior_updated_at
