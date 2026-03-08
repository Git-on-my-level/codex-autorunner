from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

DEFAULT_STALE_THRESHOLD_SECONDS = 30 * 60


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_iso_datetime(value: Any) -> Optional[str]:
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def resolve_stale_threshold_seconds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STALE_THRESHOLD_SECONDS
    if parsed <= 0:
        return DEFAULT_STALE_THRESHOLD_SECONDS
    return parsed


def build_freshness_payload(
    *,
    generated_at: Optional[str],
    stale_threshold_seconds: Any,
    candidates: Sequence[tuple[str, Any]],
    fallback_basis: str = "snapshot_generated_at",
) -> dict[str, Any]:
    generated_at_text = normalize_iso_datetime(generated_at) or iso_now()
    generated_dt = parse_iso_datetime(generated_at_text) or datetime.now(timezone.utc)
    threshold_seconds = resolve_stale_threshold_seconds(stale_threshold_seconds)

    recency_basis = fallback_basis
    basis_at = generated_at_text
    fallback_used = True
    for label, value in candidates:
        candidate_at = normalize_iso_datetime(value)
        if candidate_at is None:
            continue
        recency_basis = str(label or fallback_basis).strip() or fallback_basis
        basis_at = candidate_at
        fallback_used = False
        break

    basis_dt = parse_iso_datetime(basis_at)
    age_seconds: Optional[int] = None
    if basis_dt is not None:
        delta = generated_dt - basis_dt
        age_seconds = max(0, int(delta.total_seconds()))

    is_stale = bool(age_seconds is not None and age_seconds > threshold_seconds)
    status = "unknown" if age_seconds is None else ("stale" if is_stale else "fresh")
    return {
        "schema_version": 1,
        "generated_at": generated_at_text,
        "stale_threshold_seconds": threshold_seconds,
        "recency_basis": recency_basis,
        "basis_at": basis_at,
        "age_seconds": age_seconds,
        "is_stale": is_stale,
        "status": status,
        "fallback_used": fallback_used,
    }


def summarize_section_freshness(
    items: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[str],
    stale_threshold_seconds: Any,
    extractor: Optional[
        Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]
    ] = None,
) -> dict[str, Any]:
    generated_at_text = normalize_iso_datetime(generated_at) or iso_now()
    threshold_seconds = resolve_stale_threshold_seconds(stale_threshold_seconds)

    stale_count = 0
    fresh_count = 0
    unknown_count = 0
    basis_timestamps: list[str] = []

    for item in items:
        freshness = (
            extractor(item)
            if extractor is not None
            else item.get("freshness") if isinstance(item, Mapping) else None
        )
        if not isinstance(freshness, Mapping):
            unknown_count += 1
            continue
        basis_at = normalize_iso_datetime(freshness.get("basis_at"))
        if basis_at:
            basis_timestamps.append(basis_at)
        if freshness.get("status") == "stale" or freshness.get("is_stale") is True:
            stale_count += 1
        elif freshness.get("status") == "fresh":
            fresh_count += 1
        else:
            unknown_count += 1

    oldest_basis_at = min(basis_timestamps) if basis_timestamps else None
    newest_basis_at = max(basis_timestamps) if basis_timestamps else None
    entity_count = len(items)
    return {
        "generated_at": generated_at_text,
        "stale_threshold_seconds": threshold_seconds,
        "entity_count": entity_count,
        "stale_count": stale_count,
        "fresh_count": fresh_count,
        "unknown_count": unknown_count,
        "is_stale": stale_count > 0,
        "partially_stale": stale_count > 0 and fresh_count > 0,
        "oldest_basis_at": oldest_basis_at,
        "newest_basis_at": newest_basis_at,
    }
