from __future__ import annotations

import pytest

from codex_autorunner.core.domain.refs import SurfaceRef
from codex_autorunner.core.ports.surface_port import (
    EngineCommand,
    SurfaceHealthStatus,
)
from codex_autorunner.surfaces.web.web_surface_port import (
    WebSurfaceConfig,
    WebSurfacePort,
    build_web_surface_port,
)


def _web_ref(key: str = "test") -> SurfaceRef:
    return SurfaceRef(kind="web", key=key)


def test_build_web_surface_port_defaults() -> None:
    port = build_web_surface_port()
    assert isinstance(port, WebSurfacePort)
    assert isinstance(port._config, WebSurfaceConfig)
    assert port._config.static_assets_ok is True


def test_build_web_surface_port_custom() -> None:
    port = build_web_surface_port(
        host="0.0.0.0",
        port=8080,
        base_path="/car",
        auth_enabled=True,
        static_assets_ok=False,
    )
    assert port._config.host == "0.0.0.0"
    assert port._config.port == 8080
    assert port._config.base_path == "/car"
    assert port._config.auth_enabled is True
    assert port._config.static_assets_ok is False


@pytest.mark.asyncio
async def test_capabilities_returns_web_features() -> None:
    port = build_web_surface_port()
    ref = _web_ref()
    caps = await port.capabilities(ref)
    assert caps.surface.kind == "web"
    assert caps.supports_threads is True
    assert caps.supports_reactions is True
    assert caps.supports_files is True
    assert caps.supports_typing_indicator is True
    assert "sse" in caps.features
    assert "http_api" in caps.features
    assert "pma_threads" in caps.features


@pytest.mark.asyncio
async def test_capabilities_normalizes_non_web_surface_ref() -> None:
    port = build_web_surface_port()
    ref = SurfaceRef(kind="other", key="x")
    caps = await port.capabilities(ref)
    assert caps.surface.kind == "web"


@pytest.mark.asyncio
async def test_health_healthy_when_assets_ok() -> None:
    port = build_web_surface_port(static_assets_ok=True)
    ref = _web_ref()
    health = await port.health(ref)
    assert health.status == SurfaceHealthStatus.HEALTHY
    assert health.message == "ok"
    assert health.checked_at


@pytest.mark.asyncio
async def test_health_degraded_when_assets_missing() -> None:
    port = build_web_surface_port(static_assets_ok=False)
    ref = _web_ref()
    health = await port.health(ref)
    assert health.status == SurfaceHealthStatus.DEGRADED
    assert "Static assets" in health.message


@pytest.mark.asyncio
async def test_health_normalizes_non_web_surface_ref() -> None:
    port = build_web_surface_port()
    ref = SurfaceRef(kind="other", key="x")
    health = await port.health(ref)
    assert health.surface.kind == "web"


@pytest.mark.asyncio
async def test_send_returns_delivered() -> None:
    port = build_web_surface_port()
    cmd = EngineCommand(
        command_type="message",
        target=_web_ref("user-1"),
        payload={"text": "hello"},
    )
    delivery = await port.send(cmd)
    assert delivery.status == "delivered"
    assert delivery.delivery_id.startswith("web-")
    assert delivery.surface.kind == "web"


@pytest.mark.asyncio
async def test_send_with_no_target_uses_default() -> None:
    port = build_web_surface_port()
    cmd = EngineCommand(command_type="ping")
    delivery = await port.send(cmd)
    assert delivery.surface.kind == "web"
    assert delivery.surface.key == "default"


@pytest.mark.asyncio
async def test_receive_returns_empty() -> None:
    port = build_web_surface_port()
    ref = _web_ref()
    events = []
    async for event in port.receive(ref):
        events.append(event)
    assert events == []


@pytest.mark.asyncio
async def test_web_surface_port_in_registry() -> None:
    from codex_autorunner.core.ports.surface_port_registry import SurfacePortRegistry

    port = build_web_surface_port()
    registry = SurfacePortRegistry()
    registry.register("web", port)
    assert registry.has("web")
    caps = await registry.capabilities(_web_ref())
    assert caps.surface.kind == "web"
    health = await registry.health(_web_ref())
    assert health.status == SurfaceHealthStatus.HEALTHY
