from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .....core.config import PMA_DEFAULT_MAX_TEXT_CHARS


def normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def pma_config_from_raw(raw: Any) -> dict[str, Any]:
    pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
    if not isinstance(pma_config, dict):
        pma_config = {}
    return {
        "enabled": bool(pma_config.get("enabled", True)),
        "default_agent": normalize_optional_text(pma_config.get("default_agent")),
        "profile": normalize_optional_text(pma_config.get("profile")),
        "model": normalize_optional_text(pma_config.get("model")),
        "reasoning": normalize_optional_text(pma_config.get("reasoning")),
        "managed_thread_terminal_followup_default": bool(
            pma_config.get("managed_thread_terminal_followup_default", True)
        ),
        "active_context_max_lines": int(
            pma_config.get("active_context_max_lines", 200)
        ),
        "max_text_chars": int(
            pma_config.get("max_text_chars", PMA_DEFAULT_MAX_TEXT_CHARS)
        ),
    }


def build_idempotency_key(
    *,
    lane_id: str,
    agent: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    client_turn_id: Optional[str],
    message: str,
    profile: Optional[str] = None,
) -> str:
    payload = {
        "lane_id": lane_id,
        "agent": agent,
        "profile": profile,
        "model": model,
        "reasoning": reasoning,
        "client_turn_id": client_turn_id,
        "message": message,
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"pma:{digest}"
