from __future__ import annotations

from typing import Any, Mapping, Optional

from .text_utils import _parse_iso_timestamp

PMA_FILE_NEXT_ACTION_PROCESS = "process_uploaded_file"
PMA_FILE_NEXT_ACTION_REVIEW_STALE = "review_stale_uploaded_file"


def _extract_entry_freshness(entry: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    freshness = entry.get("freshness")
    if isinstance(freshness, Mapping):
        return freshness
    canonical = entry.get("canonical_state_v1")
    if isinstance(canonical, Mapping):
        nested = canonical.get("freshness")
        if isinstance(nested, Mapping):
            return nested
    return None


def classify_pma_file_inbox_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    freshness = _extract_entry_freshness(entry)
    is_stale = bool(
        isinstance(freshness, Mapping) and freshness.get("is_stale") is True
    )
    if is_stale:
        return {
            "next_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
            "attention_summary": (
                "Likely stale leftover upload. Verify whether it was already handled "
                "before treating it as new work."
            ),
            "why_selected": (
                "Stale file remains in the PMA inbox and is more likely leftover "
                "state than urgent work"
            ),
            "recommended_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
            "recommended_detail": (
                "Check recent PMA activity before routing. If the file was already "
                "handled, move it out of the active inbox with `car pma file "
                "consume` or `car pma file dismiss`."
            ),
            "urgency": "low",
            "likely_false_positive": True,
        }
    return {
        "next_action": PMA_FILE_NEXT_ACTION_PROCESS,
        "attention_summary": "Fresh upload is waiting in the PMA inbox.",
        "why_selected": "Fresh upload is waiting in the PMA inbox",
        "recommended_action": PMA_FILE_NEXT_ACTION_PROCESS,
        "recommended_detail": (
            "Inspect `.codex-autorunner/filebox/inbox/` and route the upload"
        ),
        "urgency": "normal",
        "likely_false_positive": False,
    }


def enrich_pma_file_inbox_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(entry)
    enriched.update(classify_pma_file_inbox_entry(enriched))
    return enriched


def _timestamp_sort_value(value: Any) -> float:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()
