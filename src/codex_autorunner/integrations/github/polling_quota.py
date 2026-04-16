from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from ...core.text_utils import _mapping, _normalize_text


def _normalize_positive_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _normalize_non_negative_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _timestamp_from_epoch(epoch_seconds: Optional[int]) -> Optional[str]:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass(frozen=True)
class GitHubQuotaState:
    resource: str
    remaining: int
    limit: int
    reset_epoch: Optional[int]
    near_limit: bool

    @property
    def reset_at(self) -> Optional[str]:
        return _timestamp_from_epoch(self.reset_epoch)


@dataclass(frozen=True)
class CachedQuotaState:
    value: Optional[GitHubQuotaState]
    expires_at: datetime


def cached_quota_state_from_mapping(value: Any) -> Optional[CachedQuotaState]:
    from .polling import _parse_iso

    payload = _mapping(value)
    expires_at_raw = _normalize_text(payload.get("expires_at"))
    if expires_at_raw is None:
        return None
    try:
        expires_at = _parse_iso(expires_at_raw)
    except ValueError:
        return None
    quota_state_payload = _mapping(payload.get("value"))
    resource = _normalize_text(quota_state_payload.get("resource"))
    remaining = _normalize_non_negative_int(quota_state_payload.get("remaining"))
    limit = _normalize_positive_int(quota_state_payload.get("limit"))
    reset_epoch = _normalize_positive_int(quota_state_payload.get("reset_epoch"))
    near_limit_raw = quota_state_payload.get("near_limit")
    if not quota_state_payload:
        return CachedQuotaState(value=None, expires_at=expires_at)
    if (
        resource is None
        or remaining is None
        or limit is None
        or not isinstance(near_limit_raw, bool)
    ):
        return None
    return CachedQuotaState(
        value=GitHubQuotaState(
            resource=resource,
            remaining=remaining,
            limit=limit,
            reset_epoch=reset_epoch,
            near_limit=near_limit_raw,
        ),
        expires_at=expires_at,
    )


def cached_quota_state_to_mapping(
    value: Optional[CachedQuotaState],
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    quota_state = value.value
    payload: dict[str, Any] = {
        "expires_at": value.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value": None,
    }
    if quota_state is not None:
        payload["value"] = {
            "resource": quota_state.resource,
            "remaining": quota_state.remaining,
            "limit": quota_state.limit,
            "reset_epoch": quota_state.reset_epoch,
            "near_limit": quota_state.near_limit,
        }
    return payload


def quota_state_from_payload(
    payload: Mapping[str, Any],
) -> Optional[GitHubQuotaState]:
    resources = _mapping(payload.get("resources"))
    _RATE_LIMIT_MIN_REMAINING = 100
    _RATE_LIMIT_RATIO_FLOOR = 0.02
    _RATE_LIMIT_RESOURCES = ("graphql", "core")
    selected: Optional[tuple[float, int, GitHubQuotaState]] = None
    for resource_name in _RATE_LIMIT_RESOURCES:
        entry = _mapping(resources.get(resource_name))
        limit = _normalize_positive_int(entry.get("limit"))
        remaining = _normalize_non_negative_int(entry.get("remaining"))
        reset_epoch = _normalize_positive_int(entry.get("reset"))
        if limit is None or remaining is None:
            continue
        remaining_ratio = remaining / float(limit)
        near_limit = (
            remaining <= min(_RATE_LIMIT_MIN_REMAINING, limit)
            or remaining_ratio <= _RATE_LIMIT_RATIO_FLOOR
        )
        candidate = GitHubQuotaState(
            resource=resource_name,
            remaining=remaining,
            limit=limit,
            reset_epoch=reset_epoch,
            near_limit=near_limit,
        )
        ranking = (remaining_ratio, remaining, candidate)
        if selected is None or ranking[:2] < selected[:2]:
            selected = ranking
    return selected[2] if selected is not None else None


def rate_limit_backoff_until(
    quota_state: Optional[GitHubQuotaState],
    *,
    now: datetime,
    parse_iso_fn: Callable[[str], datetime],
    iso_after_seconds_fn: Callable[[int], str],
    backoff_seconds: int = 15 * 60,
) -> str:
    reset_at = quota_state.reset_at if quota_state is not None else None
    if reset_at is not None:
        reset_dt = parse_iso_fn(reset_at)
        if reset_dt > now:
            return reset_at
    return iso_after_seconds_fn(backoff_seconds)


def quota_state_cache_expiry(
    quota_state: Optional[GitHubQuotaState],
    *,
    now: datetime,
    parse_iso_fn: Callable[[str], datetime],
) -> datetime:
    _RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS = 10 * 60
    _RATE_LIMIT_QUOTA_ERROR_CACHE_TTL_SECONDS = 30
    _RATE_LIMIT_QUOTA_NEAR_LIMIT_FALLBACK_TTL_SECONDS = 60
    if quota_state is None:
        return now + timedelta(seconds=_RATE_LIMIT_QUOTA_ERROR_CACHE_TTL_SECONDS)
    if not quota_state.near_limit:
        return now + timedelta(seconds=_RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS)
    reset_at = quota_state.reset_at
    if reset_at is not None:
        reset_timestamp = parse_iso_fn(reset_at)
        if reset_timestamp > now:
            return min(
                reset_timestamp,
                now + timedelta(seconds=_RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS),
            )
    return now + timedelta(seconds=_RATE_LIMIT_QUOTA_NEAR_LIMIT_FALLBACK_TTL_SECONDS)


__all__ = [
    "CachedQuotaState",
    "GitHubQuotaState",
    "cached_quota_state_from_mapping",
    "cached_quota_state_to_mapping",
    "quota_state_cache_expiry",
    "quota_state_from_payload",
    "rate_limit_backoff_until",
]
