from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..domain.refs import SurfaceRef
from .surface_port import (
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
    SurfacePort,
)

_logger = logging.getLogger(__name__)


class SurfacePortNotFoundError(KeyError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"No SurfacePort registered for kind={kind!r}")


class SurfacePortRegistrationError(ValueError):
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        super().__init__(f"Cannot register SurfacePort for kind={kind!r}: {detail}")


@dataclass
class SurfacePortRegistry:
    _ports: Dict[str, SurfacePort] = field(default_factory=dict)
    strict: bool = True

    def register(self, kind: str, port: SurfacePort) -> None:
        if not kind or not kind.strip():
            raise SurfacePortRegistrationError(kind, "kind must be a non-empty string")
        if kind in self._ports:
            raise SurfacePortRegistrationError(
                kind, f"a port is already registered for kind={kind!r}"
            )
        self._ports[kind] = port
        _logger.debug("SurfacePort registered: kind=%s", kind)

    def get(self, kind: str) -> SurfacePort:
        port = self._ports.get(kind)
        if port is None:
            raise SurfacePortNotFoundError(kind)
        return port

    def has(self, kind: str) -> bool:
        return kind in self._ports

    def registered_kinds(self) -> List[str]:
        return sorted(self._ports.keys())

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        port = self._get_or_raise(surface.kind)
        try:
            return await port.health(surface)
        except Exception as exc:
            return SurfaceHealth(
                surface=surface,
                status=SurfaceHealthStatus.UNAVAILABLE,
                message=str(exc),
            )

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        port = self._get_or_raise(surface.kind)
        return await port.capabilities(surface)

    async def all_health(self) -> List[SurfaceHealth]:
        results: List[SurfaceHealth] = []
        for kind, port in self._ports.items():
            ref = SurfaceRef(kind=kind, key="__registry_probe__")
            try:
                h = await port.health(ref)
            except Exception as exc:
                h = SurfaceHealth(
                    surface=ref,
                    status=(
                        SurfacePort.UNAVAILABLE
                        if hasattr(SurfacePort, "UNAVAILABLE")
                        else SurfaceHealthStatus.UNAVAILABLE
                    ),
                    message=str(exc),
                )
            results.append(h)
        return results

    async def all_capabilities(self) -> List[SurfaceCapabilities]:
        results: List[SurfaceCapabilities] = []
        for kind, port in self._ports.items():
            ref = SurfaceRef(kind=kind, key="__registry_probe__")
            try:
                c = await port.capabilities(ref)
            except Exception as exc:
                if self.strict:
                    raise
                _logger.warning("Capability probe failed for kind=%s: %s", kind, exc)
                continue
            results.append(c)
        return results

    def _get_or_raise(self, kind: str) -> SurfacePort:
        port = self._ports.get(kind)
        if port is None:
            raise SurfacePortNotFoundError(kind)
        return port


def build_default_surface_registry(
    extra_ports: Optional[Sequence[tuple[str, SurfacePort]]] = None,
) -> SurfacePortRegistry:
    registry = SurfacePortRegistry()
    if extra_ports:
        for kind, port in extra_ports:
            registry.register(kind, port)
    return registry


__all__ = [
    "SurfacePortNotFoundError",
    "SurfacePortRegistrationError",
    "SurfacePortRegistry",
    "build_default_surface_registry",
]
