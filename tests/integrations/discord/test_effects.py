from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.integrations.discord.effects import (
    DiscordAutocompleteEffect,
    DiscordComponentResponseEffect,
    DiscordComponentUpdateEffect,
    DiscordDeferEffect,
    DiscordEffectSink,
    DiscordFollowupEffect,
    DiscordModalEffect,
    DiscordOriginalMessageEditEffect,
    DiscordResponseEffect,
)
from codex_autorunner.integrations.discord.interaction_session import (
    InteractionInitialResponse,
    InteractionSessionKind,
)
from codex_autorunner.integrations.discord.response_helpers import DiscordResponder


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": f"followup-{len(self.followup_messages)}"}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": "@original"}


def _build_sink() -> tuple[_FakeRest, DiscordResponder, DiscordEffectSink]:
    rest = _FakeRest()
    config = SimpleNamespace(application_id="app-1", max_message_length=2000)
    responder = DiscordResponder(
        rest=rest,
        config=config,
        logger=logging.getLogger("test.discord.effects"),
    )
    return rest, responder, DiscordEffectSink(responder)


@pytest.mark.anyio
async def test_ephemeral_response_effect_uses_initial_response() -> None:
    rest, _responder, sink = _build_sink()

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordResponseEffect(text="hello", ephemeral=True),
    )

    assert rest.interaction_responses == [
        {
            "interaction_id": "inter-1",
            "interaction_token": "token-1",
            "payload": {"type": 4, "data": {"content": "hello", "flags": 64}},
        }
    ]
    assert rest.followup_messages == []


@pytest.mark.anyio
async def test_preferred_followup_response_effect_uses_followup_after_defer() -> None:
    rest, responder, sink = _build_sink()
    session = responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )
    session.restore_initial_response(InteractionInitialResponse.DEFER_EPHEMERAL)

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordResponseEffect(
            text="queued",
            ephemeral=True,
            prefer_followup=True,
        ),
    )

    assert rest.interaction_responses == []
    assert rest.followup_messages == [
        {
            "application_id": "app-1",
            "interaction_token": "token-1",
            "payload": {"content": "queued", "flags": 64},
        }
    ]


@pytest.mark.anyio
async def test_component_update_effect_uses_component_callback() -> None:
    rest, responder, sink = _build_sink()
    responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.COMPONENT,
    )

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordComponentUpdateEffect(
            text="updated",
            components=[{"type": 1, "components": []}],
        ),
    )

    assert rest.interaction_responses == [
        {
            "interaction_id": "inter-1",
            "interaction_token": "token-1",
            "payload": {
                "type": 7,
                "data": {
                    "content": "updated",
                    "components": [{"type": 1, "components": []}],
                },
            },
        }
    ]


@pytest.mark.anyio
async def test_deferred_component_response_effect_edits_original_message() -> None:
    rest, responder, sink = _build_sink()
    session = responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.COMPONENT,
    )
    session.restore_initial_response(InteractionInitialResponse.DEFER_COMPONENT_UPDATE)

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordComponentResponseEffect(
            text="after defer",
            deferred=True,
            components=[],
        ),
    )

    assert rest.interaction_responses == []
    assert rest.edited_original_interaction_responses == [
        {
            "application_id": "app-1",
            "interaction_token": "token-1",
            "payload": {"content": "after defer", "components": []},
        }
    ]


@pytest.mark.anyio
async def test_deferred_component_response_effect_clears_components_by_default() -> (
    None
):
    rest, responder, sink = _build_sink()
    session = responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.COMPONENT,
    )
    session.restore_initial_response(InteractionInitialResponse.DEFER_COMPONENT_UPDATE)

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordComponentResponseEffect(
            text="after defer",
            deferred=True,
        ),
    )

    assert rest.interaction_responses == []
    assert rest.edited_original_interaction_responses == [
        {
            "application_id": "app-1",
            "interaction_token": "token-1",
            "payload": {"content": "after defer", "components": []},
        }
    ]


@pytest.mark.anyio
async def test_original_message_edit_effect_translates_to_original_edit() -> None:
    rest, responder, sink = _build_sink()
    session = responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.COMPONENT,
    )
    session.restore_initial_response(InteractionInitialResponse.DEFER_COMPONENT_UPDATE)

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordOriginalMessageEditEffect(text="edit only", components=[]),
    )

    assert rest.edited_original_interaction_responses == [
        {
            "application_id": "app-1",
            "interaction_token": "token-1",
            "payload": {"content": "edit only", "components": []},
        }
    ]


@pytest.mark.anyio
async def test_modal_effect_translates_to_modal_callback() -> None:
    rest, responder, sink = _build_sink()
    responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.COMPONENT,
    )

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordModalEffect(
            kind=InteractionSessionKind.COMPONENT,
            custom_id="modal-1",
            title="Edit ticket",
            components=[{"type": 18, "label": "Body", "component": {"type": 4}}],
        ),
    )

    assert rest.interaction_responses == [
        {
            "interaction_id": "inter-1",
            "interaction_token": "token-1",
            "payload": {
                "type": 9,
                "data": {
                    "custom_id": "modal-1",
                    "title": "Edit ticket",
                    "components": [
                        {"type": 18, "label": "Body", "component": {"type": 4}}
                    ],
                },
            },
        }
    ]


@pytest.mark.anyio
async def test_autocomplete_effect_translates_to_autocomplete_callback() -> None:
    rest, responder, sink = _build_sink()
    responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.AUTOCOMPLETE,
    )

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordAutocompleteEffect(
            choices=[{"name": "workspace", "value": "workspace"}]
        ),
    )

    assert rest.interaction_responses == [
        {
            "interaction_id": "inter-1",
            "interaction_token": "token-1",
            "payload": {
                "type": 8,
                "data": {"choices": [{"name": "workspace", "value": "workspace"}]},
            },
        }
    ]


@pytest.mark.anyio
async def test_followup_effect_translates_to_followup_message() -> None:
    rest, responder, sink = _build_sink()
    session = responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )
    session.restore_initial_response(InteractionInitialResponse.DEFER_PUBLIC)

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordFollowupEffect(
            content="later",
            ephemeral=False,
            components=[{"type": 1, "components": []}],
        ),
    )

    assert rest.followup_messages == [
        {
            "application_id": "app-1",
            "interaction_token": "token-1",
            "payload": {
                "content": "later",
                "components": [{"type": 1, "components": []}],
            },
        }
    ]


@pytest.mark.anyio
async def test_defer_effect_translates_to_deferred_callback() -> None:
    rest, responder, sink = _build_sink()
    responder.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )

    await sink.apply(
        interaction_id="inter-1",
        interaction_token="token-1",
        effect=DiscordDeferEffect(mode="ephemeral"),
    )

    assert rest.interaction_responses == [
        {
            "interaction_id": "inter-1",
            "interaction_token": "token-1",
            "payload": {"type": 5, "data": {"flags": 64}},
        }
    ]
