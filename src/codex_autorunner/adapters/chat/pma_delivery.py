"""Adapter contract for chat delivery intents.

Canonical delivery types live in ``pma_domain.models``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ...core.chat_delivery import ChatDeliveryIntent
from ...core.pma_domain.models import PmaDeliveryAttempt


@dataclass(frozen=True)
class ChatDeliveryRecord:
    delivery_mode: str
    surface_kind: str
    surface_key: str
    delivery_record_id: str
    workspace_root: str | None = None


@dataclass(frozen=True)
class ChatDeliveryAdapterResult:
    route: str
    targets: int
    published: int
    delivery_records: tuple[ChatDeliveryRecord, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "targets": self.targets,
            "published": self.published,
        }


@runtime_checkable
class ChatDeliveryAdapter(Protocol):
    @property
    def surface_kind(self) -> str:
        """Stable transport key handled by this adapter."""

    async def deliver_pma_attempt(
        self,
        intent: ChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> ChatDeliveryAdapterResult:
        """Translate one PMA control-plane attempt into transport IO."""


__all__ = [
    "ChatDeliveryAdapter",
    "ChatDeliveryAdapterResult",
    "ChatDeliveryRecord",
]
