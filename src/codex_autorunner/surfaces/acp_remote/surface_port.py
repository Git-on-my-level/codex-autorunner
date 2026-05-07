from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional

from ...core.domain.refs import SurfaceRef
from ...core.ports.surface_port import (
    EngineCommand,
    InboundEvent,
    OutboundDelivery,
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
)
from ...core.time_utils import now_iso

_ACP_REMOTE_SURFACE_KIND = "acp_remote"

_logger = logging.getLogger(__name__)


class AcpRemoteSurfacePort:
    SKELETON_CAPABILITIES = (
        "acp_protocol",
        "remote_agent_handoff",
    )

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._logger = logger or _logger

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        ref = self._coerce_ref(surface)
        return SurfaceCapabilities(
            surface=ref,
            supports_threads=False,
            supports_reactions=False,
            supports_files=False,
            supports_typing_indicator=False,
            max_message_length=None,
            features=list(self.SKELETON_CAPABILITIES),
        )

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        ref = self._coerce_ref(surface)
        return SurfaceHealth(
            surface=ref,
            status=SurfaceHealthStatus.HEALTHY,
            message="acp_remote skeleton: not yet connected",
            checked_at=now_iso(),
        )

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        raise NotImplementedError(
            "acp_remote surface port does not support send; runtime not implemented"
        )

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        raise NotImplementedError(
            "acp_remote surface port does not support receive; runtime not implemented"
        )
        yield  # noqa: F841

    def _coerce_ref(self, surface: SurfaceRef) -> SurfaceRef:
        if surface.kind == _ACP_REMOTE_SURFACE_KIND:
            return surface
        return SurfaceRef(kind=_ACP_REMOTE_SURFACE_KIND, key=surface.key)


def build_acp_remote_surface_port(
    *,
    logger: Optional[logging.Logger] = None,
) -> AcpRemoteSurfacePort:
    return AcpRemoteSurfacePort(logger=logger)


__all__ = ["AcpRemoteSurfacePort", "build_acp_remote_surface_port"]
