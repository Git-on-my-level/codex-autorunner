from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.integrations.discord.interaction_session import (
    DiscordInteractionSessionManager,
    InteractionInitialResponse,
    InteractionSessionError,
    InteractionSessionKind,
)


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
        return {"id": "followup-1"}

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


def _manager(rest: _FakeRest) -> DiscordInteractionSessionManager:
    return DiscordInteractionSessionManager(
        rest=rest,
        config=SimpleNamespace(application_id="app-1", max_message_length=2000),
        logger=logging.getLogger("test.interaction_session"),
    )


@pytest.mark.anyio
async def test_slash_command_defer_ephemeral_then_followup_message() -> None:
    rest = _FakeRest()
    manager = _manager(rest)
    session = manager.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )

    deferred = await session.defer_ephemeral()
    await session.send_message("done", ephemeral=True)

    assert deferred is True
    assert session.initial_response == InteractionInitialResponse.DEFER_EPHEMERAL
    assert rest.interaction_responses[0]["payload"] == {
        "type": 5,
        "data": {"flags": 64},
    }
    assert rest.followup_messages[0]["payload"]["content"] == "done"
    assert rest.followup_messages[0]["payload"]["flags"] == 64


@pytest.mark.anyio
async def test_double_ack_is_rejected() -> None:
    rest = _FakeRest()
    manager = _manager(rest)
    session = manager.start_session(
        interaction_id="inter-1",
        interaction_token="token-1",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )

    assert await session.defer_ephemeral() is True
    with pytest.raises(InteractionSessionError):
        await session.defer_public()


@pytest.mark.anyio
async def test_modal_open_rejected_after_acknowledgement() -> None:
    rest = _FakeRest()
    manager = _manager(rest)
    session = manager.start_session(
        interaction_id="inter-2",
        interaction_token="token-2",
        kind=InteractionSessionKind.SLASH_COMMAND,
    )

    assert await session.defer_ephemeral() is True
    with pytest.raises(InteractionSessionError):
        await session.respond_modal(
            custom_id="modal-1",
            title="Edit",
            components=[],
        )


@pytest.mark.anyio
async def test_component_update_rejected_for_wrong_ack_path() -> None:
    rest = _FakeRest()
    manager = _manager(rest)
    session = manager.start_session(
        interaction_id="inter-3",
        interaction_token="token-3",
        kind=InteractionSessionKind.COMPONENT,
    )

    assert await session.defer_ephemeral() is True
    with pytest.raises(InteractionSessionError):
        await session.update_component_message(text="nope", components=[])


@pytest.mark.anyio
async def test_component_defer_update_allows_original_edit() -> None:
    rest = _FakeRest()
    manager = _manager(rest)
    session = manager.start_session(
        interaction_id="inter-4",
        interaction_token="token-4",
        kind=InteractionSessionKind.COMPONENT,
    )

    assert await session.defer_component_update() is True
    await session.update_component_message(text="updated", components=[])

    assert session.initial_response == InteractionInitialResponse.DEFER_COMPONENT_UPDATE
    assert rest.interaction_responses[0]["payload"] == {"type": 6}
    assert (
        rest.edited_original_interaction_responses[0]["payload"]["content"] == "updated"
    )


@pytest.mark.anyio
async def test_autocomplete_and_modal_submit_transitions() -> None:
    rest = _FakeRest()
    manager = _manager(rest)

    autocomplete_session = manager.start_session(
        interaction_id="inter-5",
        interaction_token="token-5",
        kind=InteractionSessionKind.AUTOCOMPLETE,
    )
    await autocomplete_session.respond_autocomplete(
        choices=[{"name": "Workspace", "value": "repo-1"}]
    )
    with pytest.raises(InteractionSessionError):
        await autocomplete_session.send_message("late")

    modal_submit_session = manager.start_session(
        interaction_id="inter-6",
        interaction_token="token-6",
        kind=InteractionSessionKind.MODAL_SUBMIT,
    )
    await modal_submit_session.send_message("saved", ephemeral=True)
    with pytest.raises(InteractionSessionError):
        await modal_submit_session.respond_modal(
            custom_id="modal-2",
            title="Invalid",
            components=[],
        )

    assert rest.interaction_responses[0]["payload"] == {
        "type": 8,
        "data": {"choices": [{"name": "Workspace", "value": "repo-1"}]},
    }
    assert rest.interaction_responses[1]["payload"] == {
        "type": 4,
        "data": {"content": "saved", "flags": 64},
    }
