from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence


@dataclass(frozen=True)
class PmaDeliveryOutcome:
    status: Literal[
        "invalid",
        "no_content",
        "no_targets",
        "duplicate_only",
        "success",
        "partial_success",
        "failed",
    ]
    configured_targets: int
    delivered_targets: int
    failed_targets: int
    skipped_duplicates: int = 0
    delivery_targets: list[str] = field(default_factory=list)
    delivered_target_keys: list[str] = field(default_factory=list)
    skipped_duplicate_keys: list[str] = field(default_factory=list)
    chunk_count_by_target: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    dispatch_count: int = 0

    @property
    def delivered_any(self) -> bool:
        return self.delivered_targets > 0

    @property
    def ok(self) -> bool:
        return self.status in {"success", "partial_success"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "configured_targets": self.configured_targets,
            "delivered_targets": self.delivered_targets,
            "failed_targets": self.failed_targets,
            "skipped_duplicates": self.skipped_duplicates,
            "delivery_targets": list(self.delivery_targets),
            "delivered_target_keys": list(self.delivered_target_keys),
            "skipped_duplicate_keys": list(self.skipped_duplicate_keys),
            "chunk_count_by_target": dict(self.chunk_count_by_target),
            "errors": list(self.errors),
            "dispatch_count": self.dispatch_count,
            "ok": self.ok,
            "delivered_any": self.delivered_any,
        }


def _normalize_dispatch_record(payload: Any) -> Optional[dict[str, Any]]:
    if isinstance(payload, Mapping):
        dispatch_id = payload.get("dispatch_id")
    else:
        dispatch_id = getattr(payload, "dispatch_id", None)

    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        return None
    return {"dispatch_id": dispatch_id.strip()}


async def deliver_pma_dispatches_to_delivery_targets(
    *,
    hub_root: Path,
    turn_id: str,
    dispatches: Sequence[Any],
    telegram_state_path: Path,
    discord_state_path: Optional[Path] = None,
) -> PmaDeliveryOutcome:
    _ = hub_root, telegram_state_path, discord_state_path
    if not isinstance(turn_id, str) or not turn_id:
        return PmaDeliveryOutcome(
            status="invalid",
            configured_targets=0,
            delivered_targets=0,
            failed_targets=0,
        )
    normalized_dispatches = [
        item
        for item in (_normalize_dispatch_record(dispatch) for dispatch in dispatches)
        if isinstance(item, dict)
    ]
    if not normalized_dispatches:
        return PmaDeliveryOutcome(
            status="invalid",
            configured_targets=0,
            delivered_targets=0,
            failed_targets=0,
        )
    return PmaDeliveryOutcome(
        status="no_targets",
        configured_targets=0,
        delivered_targets=0,
        failed_targets=0,
        dispatch_count=len(normalized_dispatches),
    )


async def deliver_pma_output_to_active_sink(
    *,
    hub_root: Path,
    assistant_text: str,
    turn_id: str,
    lifecycle_event: Optional[dict[str, Any]],
    telegram_state_path: Path,
    discord_state_path: Optional[Path] = None,
) -> PmaDeliveryOutcome:
    _ = hub_root, lifecycle_event, telegram_state_path, discord_state_path
    if not assistant_text or not assistant_text.strip():
        return PmaDeliveryOutcome(
            status="no_content",
            configured_targets=0,
            delivered_targets=0,
            failed_targets=0,
        )
    if not isinstance(turn_id, str) or not turn_id:
        return PmaDeliveryOutcome(
            status="invalid",
            configured_targets=0,
            delivered_targets=0,
            failed_targets=0,
        )

    return PmaDeliveryOutcome(
        status="no_targets",
        configured_targets=0,
        delivered_targets=0,
        failed_targets=0,
    )


__all__ = [
    "PmaDeliveryOutcome",
    "deliver_pma_output_to_active_sink",
    "deliver_pma_dispatches_to_delivery_targets",
]
