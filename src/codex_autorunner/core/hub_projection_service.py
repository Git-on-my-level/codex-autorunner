from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .hub_projection_store import (
    ENTITY_STATE_PROJECTION_FAMILY,
    HubProjectionSnapshot,
    HubProjectionStore,
    HubUiEventAppendResult,
    HubUiEventBatch,
)


class HubProjectionService:
    """Service boundary for durable hub read-model projections and UI deltas."""

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._store = HubProjectionStore(hub_root, durable=durable)

    @property
    def store(self) -> HubProjectionStore:
        return self._store

    def validate_or_rebuild_stale_state(self) -> None:
        self._store.validate_or_reset_projection_state()

    def rebuild_all_projections(self) -> dict[str, Any]:
        self._store.prepare_schema()
        rebuilt = self._store.rebuild_entity_state_from_events()
        return {
            "families": [rebuilt],
        }

    def rebuild_family(self, family: str) -> dict[str, Any]:
        if family != ENTITY_STATE_PROJECTION_FAMILY:
            raise ValueError(f"unknown projection family: {family}")
        self._store.prepare_schema()
        return self._store.rebuild_entity_state_from_events()

    def apply_canonical_event(
        self,
        *,
        idempotency_key: str,
        entity_kind: str,
        entity_id: str,
        operation: str,
        payload: Optional[Mapping[str, Any]] = None,
        source_revision: Optional[str] = None,
    ) -> HubUiEventAppendResult:
        return self._store.append_event(
            idempotency_key=idempotency_key,
            entity_kind=entity_kind,
            entity_id=entity_id,
            operation=operation,
            payload=payload,
            source_revision=source_revision,
        )

    def read_snapshot_window(
        self,
        *,
        family: str = ENTITY_STATE_PROJECTION_FAMILY,
        after_key: Optional[str] = None,
        after_cursor: Optional[int] = None,
        after_revision: Optional[int] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._store.read_window(
            family=family,
            after_key=after_key,
            after_cursor=after_cursor,
            after_revision=after_revision,
            limit=limit,
        )

    def read_snapshot(
        self,
        *,
        family: str = ENTITY_STATE_PROJECTION_FAMILY,
        key: str,
    ) -> Optional[HubProjectionSnapshot]:
        return self._store.get_snapshot(family=family, key=key)

    def read_events_since(
        self,
        cursor: Optional[int | str],
        *,
        limit: int = 100,
    ) -> HubUiEventBatch:
        return self._store.read_events_since(cursor, limit=limit)


__all__ = ["HubProjectionService"]
