from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from ...core.state_roots import resolve_global_state_root


def _telegram_lock_path(token: str) -> Path:
    if not isinstance(token, str) or not token:
        raise ValueError("token is required")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return resolve_global_state_root() / "locks" / f"telegram_bot_{digest}.lock"


def _read_lock_payload(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _lock_payload_summary(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("pid", "started_at", "host", "cwd", "config_root"):
        if key in payload:
            summary[key] = payload.get(key)
    return summary
