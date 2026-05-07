from __future__ import annotations

import logging
from dataclasses import dataclass
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

_WEB_SURFACE_KIND = "web"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSurfaceConfig:
    host: str = "127.0.0.1"
    port: int = 0
    base_path: str = ""
    auth_enabled: bool = False
    static_assets_ok: bool = True


class WebSurfacePort:
    def __init__(
        self,
        config: Optional[WebSurfaceConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config or WebSurfaceConfig()
        self._logger = logger or _logger
        self._started = False

    def _web_ref(self, surface: SurfaceRef) -> SurfaceRef:
        if surface.kind == _WEB_SURFACE_KIND:
            return surface
        return SurfaceRef(kind=_WEB_SURFACE_KIND, key=surface.key)

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        ref = self._web_ref(surface)
        return SurfaceCapabilities(
            surface=ref,
            supports_threads=True,
            supports_reactions=True,
            supports_files=True,
            supports_typing_indicator=True,
            max_message_length=None,
            features=[
                "sse",
                "http_api",
                "static_assets",
                "pma_threads",
                "pma_chat",
                "pma_reactions",
            ],
        )

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        ref = self._web_ref(surface)
        if not self._config.static_assets_ok:
            return SurfaceHealth(
                surface=ref,
                status=SurfaceHealthStatus.DEGRADED,
                message="Static assets not available",
                checked_at=now_iso(),
            )
        return SurfaceHealth(
            surface=ref,
            status=SurfaceHealthStatus.HEALTHY,
            message="ok",
            checked_at=now_iso(),
        )

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        delivery_id = f"web-{now_iso()}"
        target = command.target or SurfaceRef(kind=_WEB_SURFACE_KIND, key="default")
        self._logger.debug(
            "Web surface send: command=%s target=%s", command.command_type, target
        )
        return OutboundDelivery(
            delivery_id=delivery_id,
            surface=target,
            status="delivered",
        )

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        return
        yield  # noqa: F841


def build_web_surface_port(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    base_path: str = "",
    auth_enabled: bool = False,
    static_assets_ok: bool = True,
    logger: Optional[logging.Logger] = None,
) -> WebSurfacePort:
    config = WebSurfaceConfig(
        host=host,
        port=port,
        base_path=base_path,
        auth_enabled=auth_enabled,
        static_assets_ok=static_assets_ok,
    )
    return WebSurfacePort(config=config, logger=logger)


__all__ = [
    "WebSurfaceConfig",
    "WebSurfacePort",
    "build_web_surface_port",
]
