from __future__ import annotations

import pytest

from codex_autorunner.core.domain.refs import SurfaceRef
from codex_autorunner.core.ports.surface_port import (
    SurfaceCapabilities,
    SurfaceHealthStatus,
)
from codex_autorunner.surfaces.acp_remote import (
    AcpRemoteSurfacePort,
    build_acp_remote_surface_port,
)


def test_build_returns_acp_remote_surface_port():
    port = build_acp_remote_surface_port()
    assert isinstance(port, AcpRemoteSurfacePort)


@pytest.mark.asyncio
async def test_capabilities_returns_skeleton():
    port = build_acp_remote_surface_port()
    ref = SurfaceRef(kind="acp_remote", key="test")
    caps = await port.capabilities(ref)
    assert isinstance(caps, SurfaceCapabilities)
    assert caps.surface.kind == "acp_remote"
    assert "acp_protocol" in caps.features
    assert "remote_agent_handoff" in caps.features
    assert caps.supports_threads is False
    assert caps.supports_reactions is False
    assert caps.supports_files is False


@pytest.mark.asyncio
async def test_health_returns_healthy():
    port = build_acp_remote_surface_port()
    ref = SurfaceRef(kind="acp_remote", key="test")
    health = await port.health(ref)
    assert health.status == SurfaceHealthStatus.HEALTHY
    assert "skeleton" in health.message


@pytest.mark.asyncio
async def test_send_raises_not_implemented():
    port = build_acp_remote_surface_port()
    from codex_autorunner.core.ports.surface_port import EngineCommand

    command = EngineCommand(
        command_type="test",
        target=SurfaceRef(kind="acp_remote", key="test"),
    )
    with pytest.raises(NotImplementedError, match="runtime not implemented"):
        await port.send(command)


@pytest.mark.asyncio
async def test_receive_raises_not_implemented():
    port = build_acp_remote_surface_port()
    ref = SurfaceRef(kind="acp_remote", key="test")
    with pytest.raises(NotImplementedError, match="runtime not implemented"):
        async for _ in port.receive(ref):
            pass


@pytest.mark.asyncio
async def test_coerces_non_acp_remote_ref():
    port = build_acp_remote_surface_port()
    ref = SurfaceRef(kind="other", key="x")
    caps = await port.capabilities(ref)
    assert caps.surface.kind == "acp_remote"


def test_registry_registration():
    from codex_autorunner.core.ports.surface_port_registry import SurfacePortRegistry

    registry = SurfacePortRegistry()
    port = build_acp_remote_surface_port()
    registry.register("acp_remote", port)
    assert registry.has("acp_remote")
    assert registry.get("acp_remote") is port
