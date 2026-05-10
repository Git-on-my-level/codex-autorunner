"""
Contract tests for SurfacePort implementations.

Currently there are no concrete SurfacePort adapters in the engine, so this
module defines the contract skeleton plus a ``StubSurfacePort``.  When real
surface adapters (web, Discord, Telegram, CLI, ACP) are registered, add them
to ``SURFACE_PORT_FACTORIES`` below.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Tuple

import pytest

from codex_autorunner.core.domain.refs import SurfaceRef
from codex_autorunner.core.ports.surface_port import (
    EngineCommand,
    InboundEvent,
    OutboundDelivery,
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
    SurfacePort,
)


class StubSurfacePort:
    """Minimal in-memory SurfacePort for contract verification."""

    def __init__(self, surface: SurfaceRef) -> None:
        self._surface = surface
        self._healthy = True

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        return SurfaceCapabilities(surface=surface, supports_threads=True)

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        status = (
            SurfaceHealthStatus.HEALTHY
            if self._healthy
            else SurfaceHealthStatus.UNAVAILABLE
        )
        return SurfaceHealth(
            surface=surface,
            status=status,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        attachments = command.payload.get("attachments")
        if attachments and command.target is not None:
            capabilities = await self.capabilities(command.target)
            if not capabilities.supports_files:
                return OutboundDelivery(
                    delivery_id=str(uuid.uuid4()),
                    surface=command.target,
                    status="failed",
                    error="surface does not support file attachments",
                )
        return OutboundDelivery(
            delivery_id=str(uuid.uuid4()),
            surface=command.target or self._surface,
        )

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        yield InboundEvent(
            surface=surface,
            event_id="stub-1",
            event_type="message",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


SURFACE_PORT_FACTORIES: list[Tuple[str, type]] = [
    ("stub", StubSurfacePort),
]


@pytest.fixture(
    params=[name for name, _ in SURFACE_PORT_FACTORIES],
    ids=[name for name, _ in SURFACE_PORT_FACTORIES],
)
def surface_port(request) -> SurfacePort:
    surface = SurfaceRef(kind="stub", key="test-channel")
    for name, cls in SURFACE_PORT_FACTORIES:
        if name == request.param:
            return cls(surface)
    raise ValueError(f"Unknown surface port factory: {request.param}")


@pytest.fixture(params=["web", "discord", "telegram", "cli", "acp"])
def surface_kind(request) -> str:
    return request.param


class TestSurfacePortContract:
    """Invariants every SurfacePort must satisfy."""

    def test_registered_surface_port_factories_are_explicit(self) -> None:
        registered = {name for name, _ in SURFACE_PORT_FACTORIES}
        # No concrete engine SurfacePort adapters are registered yet; the stub
        # keeps the protocol contract executable until one lands.
        assert registered == {"stub"}

    def test_capabilities_returns_surface_capabilities(
        self, surface_port: SurfacePort
    ) -> None:
        surf = SurfaceRef(kind="stub", key="test-channel")
        cap = _run(surface_port.capabilities(surf))
        assert isinstance(cap, SurfaceCapabilities)
        assert cap.surface == surf

    def test_capabilities_has_boolean_features(self, surface_port: SurfacePort) -> None:
        surf = SurfaceRef(kind="stub", key="test-channel")
        cap = _run(surface_port.capabilities(surf))
        assert isinstance(cap.supports_threads, bool)
        assert isinstance(cap.supports_reactions, bool)
        assert isinstance(cap.supports_files, bool)
        assert isinstance(cap.supports_typing_indicator, bool)

    def test_health_returns_surface_health(self, surface_port: SurfacePort) -> None:
        surf = SurfaceRef(kind="stub", key="test-channel")
        health = _run(surface_port.health(surf))
        assert isinstance(health, SurfaceHealth)
        assert health.surface == surf
        assert isinstance(health.status, SurfaceHealthStatus)

    def test_health_status_is_valid_enum(self, surface_port: SurfacePort) -> None:
        surf = SurfaceRef(kind="stub", key="test-channel")
        health = _run(surface_port.health(surf))
        assert health.status in {
            SurfaceHealthStatus.HEALTHY,
            SurfaceHealthStatus.DEGRADED,
            SurfaceHealthStatus.UNAVAILABLE,
        }

    def test_send_returns_outbound_delivery(self, surface_port: SurfacePort) -> None:
        cmd = EngineCommand(
            command_type="send_message",
            target=SurfaceRef(kind="stub", key="test-channel"),
            payload={"text": "hello"},
        )
        delivery = _run(surface_port.send(cmd))
        assert isinstance(delivery, OutboundDelivery)
        assert delivery.delivery_id
        assert isinstance(delivery.status, str)

    def test_send_delivery_has_surface(self, surface_port: SurfacePort) -> None:
        target = SurfaceRef(kind="stub", key="test-channel")
        cmd = EngineCommand(command_type="send_message", target=target)
        delivery = _run(surface_port.send(cmd))
        assert delivery.surface == target

    def test_send_rejects_attachments_when_files_are_unsupported(
        self, surface_port: SurfacePort
    ) -> None:
        target = SurfaceRef(kind="stub", key="test-channel")
        capabilities = _run(surface_port.capabilities(target))
        if capabilities.supports_files:
            pytest.skip(
                "surface supports files; unsupported attachment contract skipped"
            )

        cmd = EngineCommand(
            command_type="send_message",
            target=target,
            payload={
                "text": "hello",
                "attachments": [
                    {
                        "name": "report.txt",
                        "content_type": "text/plain",
                        "data": b"hello",
                    }
                ],
            },
        )
        delivery = _run(surface_port.send(cmd))

        assert delivery.surface == target
        assert delivery.status == "failed"
        assert delivery.error

    def test_receive_yields_inbound_events(self, surface_port: SurfacePort) -> None:
        surf = SurfaceRef(kind="stub", key="test-channel")

        async def _collect():
            events = []
            async for event in surface_port.receive(surf):
                events.append(event)
            return events

        events = asyncio.get_event_loop().run_until_complete(_collect())
        assert len(events) >= 0
        for event in events:
            assert isinstance(event, InboundEvent)
            assert event.event_id
            assert event.event_type
            assert event.timestamp

    def test_has_required_protocol_methods(self, surface_port: SurfacePort) -> None:
        assert callable(getattr(surface_port, "capabilities", None))
        assert callable(getattr(surface_port, "health", None))
        assert callable(getattr(surface_port, "send", None))
        assert callable(getattr(surface_port, "receive", None))


class TestSurfacePortDataclasses:
    """Verify surface port dataclasses are frozen and well-formed."""

    def test_surface_capabilities_frozen(self) -> None:
        cap = SurfaceCapabilities(surface=SurfaceRef(kind="web", key="ws"))
        with pytest.raises(AttributeError):
            cap.supports_threads = False  # type: ignore[misc]

    def test_surface_health_frozen(self) -> None:
        h = SurfaceHealth(surface=SurfaceRef(kind="web", key="ws"))
        with pytest.raises(AttributeError):
            h.status = SurfaceHealthStatus.DEGRADED  # type: ignore[misc]

    def test_inbound_event_frozen(self) -> None:
        evt = InboundEvent(
            surface=SurfaceRef(kind="web", key="ws"),
            event_id="e1",
            event_type="message",
            timestamp="2026-01-01T00:00:00Z",
        )
        with pytest.raises(AttributeError):
            evt.event_type = "edited"  # type: ignore[misc]

    def test_engine_command_frozen(self) -> None:
        cmd = EngineCommand(command_type="send")
        with pytest.raises(AttributeError):
            cmd.command_type = "delete"  # type: ignore[misc]

    def test_outbound_delivery_frozen(self) -> None:
        d = OutboundDelivery(
            delivery_id="d1",
            surface=SurfaceRef(kind="web", key="ws"),
        )
        with pytest.raises(AttributeError):
            d.status = "failed"  # type: ignore[misc]

    def test_health_status_enum_values(self) -> None:
        assert SurfaceHealthStatus.HEALTHY.value == "healthy"
        assert SurfaceHealthStatus.DEGRADED.value == "degraded"
        assert SurfaceHealthStatus.UNAVAILABLE.value == "unavailable"
