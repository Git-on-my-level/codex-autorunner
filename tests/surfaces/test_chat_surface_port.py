from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pytest

from codex_autorunner.core.domain.refs import SurfaceRef
from codex_autorunner.core.ports.surface_port import EngineCommand, SurfaceHealthStatus
from codex_autorunner.integrations.chat.adapter import (
    SendAttachmentRequest,
    SendTextRequest,
)
from codex_autorunner.integrations.chat.capabilities import ChatCapabilities
from codex_autorunner.integrations.chat.models import (
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.chat.renderer import RenderedText, TextRenderer
from codex_autorunner.surfaces._chat_surface_port import (
    engine_command_from_chat_event,
    inbound_event_from_chat_event,
)
from codex_autorunner.surfaces.discord import build_discord_surface_port
from codex_autorunner.surfaces.telegram import build_telegram_surface_port


class _Renderer(TextRenderer):
    def render_text(self, text: str, *, parse_mode: str | None = None) -> RenderedText:
        return RenderedText(text=text, parse_mode=parse_mode)

    def split_text(
        self,
        rendered: RenderedText,
        *,
        max_length: int | None = None,
    ) -> tuple[RenderedText, ...]:
        return (rendered,)


@dataclass
class _FakeAdapter:
    platform: str = "telegram"
    events: tuple[ChatMessageEvent, ...] = ()

    def __post_init__(self) -> None:
        self.sent: list[SendTextRequest] = []

    @property
    def capabilities(self) -> ChatCapabilities:
        return ChatCapabilities()

    @property
    def renderer(self) -> TextRenderer:
        return _Renderer()

    async def poll_events(
        self, *, timeout_seconds: float = 30.0
    ) -> Sequence[ChatMessageEvent]:
        events = self.events
        self.events = ()
        return events

    async def send_text(self, request: SendTextRequest) -> ChatMessageRef:
        self.sent.append(request)
        return ChatMessageRef(thread=request.thread, message_id="sent-1")

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[object] = (),
    ) -> None:
        return None

    async def delete_message(self, message: ChatMessageRef) -> None:
        return None

    async def send_attachment(self, request: SendAttachmentRequest) -> ChatMessageRef:
        return ChatMessageRef(thread=request.thread, message_id="sent-file-1")

    async def ack_interaction(
        self, interaction: object, *, text: str | None = None
    ) -> None:
        return None


def _message_event(platform: str = "telegram") -> ChatMessageEvent:
    thread = ChatThreadRef(platform=platform, chat_id="100", thread_id="200")
    return ChatMessageEvent(
        update_id="u-1",
        thread=thread,
        message=ChatMessageRef(thread=thread, message_id="m-1"),
        from_user_id="42",
        text="hello PMA",
    )


def test_chat_event_maps_to_canonical_engine_message_command() -> None:
    command = engine_command_from_chat_event(_message_event())

    assert command.command_type == "surface.message.received"
    assert command.target == SurfaceRef(kind="telegram", key="100:200")
    assert command.payload["surface_kind"] == "telegram"
    assert command.payload["surface_key"] == "100:200"
    assert command.payload["prompt_text"] == "hello PMA"
    assert command.payload["from_user_id"] == "42"


def test_inbound_event_carries_engine_command_for_ingress() -> None:
    inbound = inbound_event_from_chat_event(_message_event("discord"))

    assert inbound.surface == SurfaceRef(kind="discord", key="100:200")
    assert inbound.event_type == "surface.message.received"
    assert isinstance(inbound.payload["engine_command"], EngineCommand)
    assert inbound.payload["engine_command_payload"]["prompt_text"] == "hello PMA"


@pytest.mark.anyio
async def test_telegram_surface_port_reports_capabilities_and_degraded_without_adapter() -> (
    None
):
    port = build_telegram_surface_port()
    ref = SurfaceRef(kind="telegram", key="100:200")

    capabilities = await port.capabilities(ref)
    health = await port.health(ref)

    assert capabilities.surface == ref
    assert capabilities.supports_threads is True
    assert capabilities.supports_files is True
    assert "pma_threads" in capabilities.features
    assert health.status is SurfaceHealthStatus.DEGRADED


@pytest.mark.anyio
async def test_discord_surface_port_delegates_delivery_to_chat_adapter() -> None:
    adapter = _FakeAdapter(platform="discord")
    port = build_discord_surface_port(adapter=adapter)
    target = SurfaceRef(kind="discord", key="channel-1")

    result = await port.send(
        EngineCommand(
            command_type="surface.reply",
            target=target,
            payload={"text": "done"},
        )
    )

    assert result.status == "delivered"
    assert result.delivery_id == "sent-1"
    assert adapter.sent[0].thread == ChatThreadRef(
        platform="discord",
        chat_id="channel-1",
        thread_id=None,
    )
    assert adapter.sent[0].text == "done"


@pytest.mark.anyio
async def test_surface_port_receive_yields_canonical_inbound_event() -> None:
    event = _message_event("telegram")
    adapter = _FakeAdapter(platform="telegram", events=(event,))
    port = build_telegram_surface_port(adapter=adapter, poll_timeout_seconds=0.01)

    received = await port.receive(
        SurfaceRef(kind="telegram", key="100:200")
    ).__anext__()

    assert received.event_type == "surface.message.received"
    assert received.payload["engine_command"].payload["prompt_text"] == "hello PMA"
