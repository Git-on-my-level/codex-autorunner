from __future__ import annotations

from typing import AsyncGenerator

import pytest

from codex_autorunner.core.domain.refs import SurfaceRef
from codex_autorunner.core.ports.surface_port import (
    EngineCommand,
    InboundEvent,
    OutboundDelivery,
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
)
from codex_autorunner.core.ports.surface_port_registry import (
    SurfacePortNotFoundError,
    SurfacePortRegistrationError,
    SurfacePortRegistry,
    build_default_surface_registry,
)
from codex_autorunner.core.time_utils import now_iso


class _StubSurfacePort:
    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            surface=surface,
            supports_threads=True,
            supports_reactions=False,
            features=["stub"],
        )

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        status = (
            SurfaceHealthStatus.HEALTHY
            if self._healthy
            else SurfaceHealthStatus.UNAVAILABLE
        )
        return SurfaceHealth(
            surface=surface,
            status=status,
            message="ok" if self._healthy else "unhealthy",
            checked_at=now_iso(),
        )

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        return OutboundDelivery(
            delivery_id="stub-1",
            surface=command.target or SurfaceRef(kind="stub", key="test"),
            status="delivered",
        )

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        return
        yield  # noqa: F841


class _BrokenSurfacePort:
    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        raise RuntimeError("capabilities broken")

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        raise RuntimeError("health broken")

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        raise RuntimeError("send broken")

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        raise RuntimeError("receive broken")
        yield  # noqa: F841


def test_register_and_get() -> None:
    registry = SurfacePortRegistry()
    port = _StubSurfacePort()
    registry.register("stub", port)
    assert registry.get("stub") is port


def test_register_duplicate_raises() -> None:
    registry = SurfacePortRegistry()
    registry.register("stub", _StubSurfacePort())
    with pytest.raises(SurfacePortRegistrationError, match="already registered"):
        registry.register("stub", _StubSurfacePort())


def test_register_empty_kind_raises() -> None:
    registry = SurfacePortRegistry()
    with pytest.raises(SurfacePortRegistrationError, match="non-empty string"):
        registry.register("", _StubSurfacePort())


def test_get_missing_raises_not_found() -> None:
    registry = SurfacePortRegistry()
    with pytest.raises(SurfacePortNotFoundError, match="No SurfacePort registered"):
        registry.get("nonexistent")


def test_has() -> None:
    registry = SurfacePortRegistry()
    assert not registry.has("web")
    registry.register("web", _StubSurfacePort())
    assert registry.has("web")


def test_registered_kinds() -> None:
    registry = SurfacePortRegistry()
    registry.register("beta", _StubSurfacePort())
    registry.register("alpha", _StubSurfacePort())
    assert registry.registered_kinds() == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_health_delegates() -> None:
    registry = SurfacePortRegistry()
    registry.register("stub", _StubSurfacePort(healthy=True))
    ref = SurfaceRef(kind="stub", key="test")
    result = await registry.health(ref)
    assert result.status == SurfaceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_health_catches_exception() -> None:
    registry = SurfacePortRegistry()
    registry.register("broken", _BrokenSurfacePort())
    ref = SurfaceRef(kind="broken", key="test")
    result = await registry.health(ref)
    assert result.status == SurfaceHealthStatus.UNAVAILABLE
    assert "health broken" in result.message


@pytest.mark.asyncio
async def test_health_missing_raises() -> None:
    registry = SurfacePortRegistry()
    ref = SurfaceRef(kind="missing", key="test")
    with pytest.raises(SurfacePortNotFoundError):
        await registry.health(ref)


@pytest.mark.asyncio
async def test_capabilities_delegates() -> None:
    registry = SurfacePortRegistry()
    registry.register("stub", _StubSurfacePort())
    ref = SurfaceRef(kind="stub", key="test")
    caps = await registry.capabilities(ref)
    assert caps.supports_threads is True
    assert "stub" in caps.features


@pytest.mark.asyncio
async def test_all_health() -> None:
    registry = SurfacePortRegistry()
    registry.register("healthy", _StubSurfacePort(healthy=True))
    registry.register("unhealthy", _StubSurfacePort(healthy=False))
    results = await registry.all_health()
    assert len(results) == 2
    statuses = {h.surface.kind: h.status for h in results}
    assert statuses["healthy"] == SurfaceHealthStatus.HEALTHY
    assert statuses["unhealthy"] == SurfaceHealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_all_health_catches_exceptions() -> None:
    registry = SurfacePortRegistry()
    registry.register("broken", _BrokenSurfacePort())
    results = await registry.all_health()
    assert len(results) == 1
    assert results[0].status == SurfaceHealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_all_capabilities() -> None:
    registry = SurfacePortRegistry()
    registry.register("stub", _StubSurfacePort())
    caps = await registry.all_capabilities()
    assert len(caps) == 1
    assert "stub" in caps[0].features


@pytest.mark.asyncio
async def test_all_capabilities_strict_raises() -> None:
    registry = SurfacePortRegistry(strict=True)
    registry.register("broken", _BrokenSurfacePort())
    with pytest.raises(RuntimeError, match="capabilities broken"):
        await registry.all_capabilities()


@pytest.mark.asyncio
async def test_all_capabilities_non_strict_skips() -> None:
    registry = SurfacePortRegistry(strict=False)
    registry.register("broken", _BrokenSurfacePort())
    caps = await registry.all_capabilities()
    assert caps == []


def test_build_default_surface_registry_empty() -> None:
    registry = build_default_surface_registry()
    assert registry.registered_kinds() == []


def test_build_default_surface_registry_with_extras() -> None:
    port = _StubSurfacePort()
    registry = build_default_surface_registry(extra_ports=[("web", port)])
    assert registry.has("web")
    assert registry.get("web") is port
