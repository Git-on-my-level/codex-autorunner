from __future__ import annotations

from typing import Optional

COMPACT_SEED_PREFIX = "Context from previous conversation:"
COMPACT_SEED_SUFFIX = "Continue from this context. Ask for missing info if needed."


def build_compact_seed_prompt(summary_text: str) -> str:
    summary = summary_text.strip() or "(no summary)"
    return f"{COMPACT_SEED_PREFIX}\n\n{summary}\n\n{COMPACT_SEED_SUFFIX}"


def match_pending_compact_seed(
    seed_text: Optional[str],
    *,
    pending_target_id: Optional[str],
    active_target_id: Optional[str],
    allow_global_target: bool = True,
) -> Optional[str]:
    if not isinstance(seed_text, str):
        return None
    normalized_seed = seed_text.strip()
    if not normalized_seed:
        return None
    if pending_target_id is None:
        return normalized_seed if allow_global_target else None
    if active_target_id and pending_target_id == active_target_id:
        return normalized_seed
    return None


__all__ = [
    "COMPACT_SEED_PREFIX",
    "COMPACT_SEED_SUFFIX",
    "build_compact_seed_prompt",
    "match_pending_compact_seed",
]
