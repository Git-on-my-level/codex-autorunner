from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .utils import atomic_write

HUB_INBOX_DISMISSALS_FILENAME = "hub_inbox_dismissals.json"
MESSAGE_PENDING_AUTO_DISMISS_STATE = "pending_auto_dismiss"
MESSAGE_RESOLVED_STATES = {"dismissed", "resolved"}


def hub_inbox_dismissals_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / HUB_INBOX_DISMISSALS_FILENAME


def _dismissal_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"


def _normalize_key_part(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def message_resolution_key(
    run_id: str,
    *,
    item_type: str,
    seq: Optional[int] = None,
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> str:
    normalized = (item_type or "").strip() or "run_dispatch"
    if normalized == "run_dispatch":
        if isinstance(seq, int) and seq > 0:
            return _dismissal_key(run_id, seq)
        return f"{run_id}:run_dispatch"
    if normalized == "capability_hint":
        normalized_hint_id = _normalize_key_part(hint_id)
        normalized_scope_key = _normalize_key_part(scope_key)
        if normalized_hint_id and normalized_scope_key:
            return f"{run_id}:{normalized}:{normalized_scope_key}:{normalized_hint_id}"
        if normalized_hint_id:
            return f"{run_id}:{normalized}:{normalized_hint_id}"
        return f"{run_id}:{normalized}"
    if isinstance(seq, int) and seq > 0:
        return f"{run_id}:{normalized}:{seq}"
    return f"{run_id}:{normalized}"


def message_resolution_state(item_type: str) -> str:
    if item_type == "run_dispatch":
        return "pending_dispatch"
    if item_type == "capability_hint":
        return "capability_hint"
    return "terminal_attention"


def message_resolvable_actions(item_type: str) -> list[str]:
    if item_type == "run_dispatch":
        return ["reply_resume", "dismiss"]
    if item_type == "capability_hint":
        return ["dismiss"]
    if item_type in {"run_failed", "run_stopped"}:
        return ["dismiss", "archive_run", "restart"]
    return ["dismiss", "reply_resume", "restart"]


def _message_resolution_lookup_keys(
    *,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> list[str]:
    keys = [
        message_resolution_key(
            run_id,
            item_type=item_type,
            seq=seq,
            hint_id=hint_id,
            scope_key=scope_key,
        )
    ]
    if item_type == "capability_hint" and scope_key:
        keys.append(
            message_resolution_key(
                run_id,
                item_type=item_type,
                seq=seq,
                hint_id=hint_id,
            )
        )
    if (
        item_type != "run_dispatch"
        and item_type != "capability_hint"
        and isinstance(seq, int)
        and seq > 0
    ):
        keys.append(message_resolution_key(run_id, item_type=item_type))
    return keys


def _is_resolved_entry(entry: dict[str, Any]) -> bool:
    state = str(entry.get("resolution_state") or "").strip().lower()
    return not state or state in MESSAGE_RESOLVED_STATES


def find_message_resolution_entry(
    dismissals: dict[str, dict[str, Any]],
    *,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    include_unresolved: bool = False,
) -> Optional[dict[str, Any]]:
    keys = _message_resolution_lookup_keys(
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        hint_id=hint_id,
        scope_key=scope_key,
    )
    for key in keys:
        entry = dismissals.get(key)
        if isinstance(entry, dict) and (
            include_unresolved or _is_resolved_entry(entry)
        ):
            return entry
    return None


def find_message_resolution(
    dismissals: dict[str, dict[str, Any]],
    *,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    return find_message_resolution_entry(
        dismissals,
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        hint_id=hint_id,
        scope_key=scope_key,
        include_unresolved=False,
    )


def load_hub_inbox_dismissals(repo_root: Path) -> dict[str, dict[str, Any]]:
    path = hub_inbox_dismissals_path(repo_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    items = payload.get("items")
    if not isinstance(items, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in items.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        out[key] = dict(value)
    return out


def save_hub_inbox_dismissals(
    repo_root: Path, items: dict[str, dict[str, Any]]
) -> None:
    path = hub_inbox_dismissals_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "items": items}
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def clear_message_resolution(
    *,
    repo_root: Path,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> bool:
    items = load_hub_inbox_dismissals(repo_root)
    removed = False
    for key in _message_resolution_lookup_keys(
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        hint_id=hint_id,
        scope_key=scope_key,
    ):
        if key in items:
            del items[key]
            removed = True
    if removed:
        save_hub_inbox_dismissals(repo_root, items)
    return removed


def record_message_resolution(
    *,
    repo_root: Path,
    repo_id: str,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    action: str,
    reason: Optional[str],
    actor: Optional[str],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> dict[str, Any]:
    items = load_hub_inbox_dismissals(repo_root)
    resolved_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "repo_id": repo_id,
        "run_id": run_id,
        "item_type": item_type,
        "seq": seq if isinstance(seq, int) and seq > 0 else None,
        "action": action,
        "reason": reason or None,
        "resolved_at": resolved_at,
        "resolution_state": "dismissed" if action == "dismiss" else "resolved",
    }
    normalized_hint_id = _normalize_key_part(hint_id)
    normalized_scope_key = _normalize_key_part(scope_key)
    if normalized_hint_id:
        payload["hint_id"] = normalized_hint_id
    if normalized_scope_key:
        payload["scope_key"] = normalized_scope_key
    if actor:
        payload["resolved_by"] = actor
    key = message_resolution_key(
        run_id,
        item_type=item_type,
        seq=seq,
        hint_id=normalized_hint_id,
        scope_key=normalized_scope_key,
    )
    items[key] = payload
    save_hub_inbox_dismissals(repo_root, items)
    return payload


def record_message_pending_auto_dismiss(
    *,
    repo_root: Path,
    repo_id: str,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    reason: Optional[str],
    grace_seconds: int,
    actor: Optional[str],
    hint_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    detected_at: Optional[str] = None,
) -> dict[str, Any]:
    items = load_hub_inbox_dismissals(repo_root)
    detected = detected_at or datetime.now(timezone.utc).isoformat()
    try:
        detected_dt = datetime.fromisoformat(detected.replace("Z", "+00:00"))
    except ValueError:
        detected_dt = datetime.now(timezone.utc)
        detected = detected_dt.isoformat()
    if detected_dt.tzinfo is None:
        detected_dt = detected_dt.replace(tzinfo=timezone.utc)
    expires_at = (
        detected_dt.astimezone(timezone.utc)
        + timedelta(seconds=max(0, int(grace_seconds)))
    ).isoformat()
    payload = {
        "repo_id": repo_id,
        "run_id": run_id,
        "item_type": item_type,
        "seq": seq if isinstance(seq, int) and seq > 0 else None,
        "action": "auto_dismiss_pending",
        "reason": reason or None,
        "detected_at": detected,
        "expires_at": expires_at,
        "grace_seconds": max(0, int(grace_seconds)),
        "resolution_state": MESSAGE_PENDING_AUTO_DISMISS_STATE,
    }
    normalized_hint_id = _normalize_key_part(hint_id)
    normalized_scope_key = _normalize_key_part(scope_key)
    if normalized_hint_id:
        payload["hint_id"] = normalized_hint_id
    if normalized_scope_key:
        payload["scope_key"] = normalized_scope_key
    if actor:
        payload["recorded_by"] = actor
    key = message_resolution_key(
        run_id,
        item_type=item_type,
        seq=seq,
        hint_id=normalized_hint_id,
        scope_key=normalized_scope_key,
    )
    items[key] = payload
    save_hub_inbox_dismissals(repo_root, items)
    return payload
