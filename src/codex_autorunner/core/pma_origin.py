from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .text_utils import _normalize_optional_text

PMA_ORIGIN_METADATA_KEY = "pma_origin"


@dataclass(frozen=True)
class PmaOriginContext:
    thread_id: Optional[str] = None
    lane_id: Optional[str] = None
    agent: Optional[str] = None
    profile: Optional[str] = None

    def is_empty(self) -> bool:
        return not any((self.thread_id, self.lane_id, self.agent, self.profile))

    def to_metadata(self) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if self.thread_id:
            metadata["thread_id"] = self.thread_id
        if self.lane_id:
            metadata["lane_id"] = self.lane_id
        if self.agent:
            metadata["agent"] = self.agent
        if self.profile:
            metadata["profile"] = self.profile
        return metadata


def normalize_pma_origin_context(value: Any) -> Optional[PmaOriginContext]:
    if not isinstance(value, dict):
        return None
    origin = PmaOriginContext(
        thread_id=_normalize_optional_text(value.get("thread_id")),
        lane_id=_normalize_optional_text(value.get("lane_id")),
        agent=_normalize_optional_text(value.get("agent")),
        profile=_normalize_optional_text(value.get("profile")),
    )
    return None if origin.is_empty() else origin


def extract_pma_origin_metadata(metadata: Any) -> Optional[PmaOriginContext]:
    if not isinstance(metadata, dict):
        return None
    return normalize_pma_origin_context(metadata.get(PMA_ORIGIN_METADATA_KEY))


def resolve_runtime_pma_origin(runtime_state: Any) -> Optional[PmaOriginContext]:
    current = getattr(runtime_state, "pma_current", None)
    return normalize_pma_origin_context(current)


def merge_pma_origin_metadata(
    metadata: Optional[dict[str, Any]],
    *,
    origin: Optional[PmaOriginContext] = None,
    origin_thread_id: Optional[str] = None,
    origin_lane_id: Optional[str] = None,
) -> dict[str, Any]:
    resolved = dict(metadata or {})
    existing_origin = extract_pma_origin_metadata(resolved)
    merged_origin = PmaOriginContext(
        thread_id=(
            _normalize_optional_text(origin_thread_id)
            or (origin.thread_id if origin else None)
            or (existing_origin.thread_id if existing_origin else None)
        ),
        lane_id=(
            _normalize_optional_text(origin_lane_id)
            or (origin.lane_id if origin else None)
            or (existing_origin.lane_id if existing_origin else None)
        ),
        agent=(
            (origin.agent if origin else None)
            or (existing_origin.agent if existing_origin else None)
        ),
        profile=(
            (origin.profile if origin else None)
            or (existing_origin.profile if existing_origin else None)
        ),
    )
    if merged_origin.is_empty():
        resolved.pop(PMA_ORIGIN_METADATA_KEY, None)
        return resolved
    resolved[PMA_ORIGIN_METADATA_KEY] = merged_origin.to_metadata()
    return resolved
