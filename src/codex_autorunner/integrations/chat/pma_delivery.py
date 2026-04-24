"""Adapter contract for PMA chat delivery intents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ...core.pma_chat_delivery import PmaChatDeliveryAttempt, PmaChatDeliveryIntent


@dataclass(frozen=True)
class PmaChatDeliveryRecord:
    delivery_mode: str
    surface_kind: str
    surface_key: str
    delivery_record_id: str
    workspace_root: str | None = None


@dataclass(frozen=True)
class PmaChatDeliveryAdapterResult:
    route: str
    targets: int
    published: int
    delivery_records: tuple[PmaChatDeliveryRecord, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "targets": self.targets,
            "published": self.published,
        }


@runtime_checkable
class PmaChatDeliveryAdapter(Protocol):
    @property
    def surface_kind(self) -> str:
        """Stable transport key handled by this adapter."""

    async def deliver_pma_attempt(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaChatDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
        """Translate one PMA control-plane attempt into transport IO."""


__all__ = [
    "PmaChatDeliveryAdapter",
    "PmaChatDeliveryAdapterResult",
    "PmaChatDeliveryRecord",
]
