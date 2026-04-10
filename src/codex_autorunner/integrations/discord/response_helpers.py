from __future__ import annotations

import logging
from typing import Any, Optional

from .interaction_session import (
    DiscordInteractionSession,
    DiscordInteractionSessionManager,
    InteractionSessionError,
    InteractionSessionKind,
)
from .rest import DiscordRestClient


class DiscordResponder:
    def __init__(
        self,
        rest: DiscordRestClient,
        config: Any,
        logger: logging.Logger,
    ) -> None:
        self._sessions = DiscordInteractionSessionManager(
            rest=rest,
            config=config,
            logger=logger,
        )

    def start_session(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
    ) -> DiscordInteractionSession:
        return self._sessions.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )

    def session(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind = InteractionSessionKind.UNKNOWN,
    ) -> DiscordInteractionSession:
        existing = self.get_session(interaction_token)
        if existing is not None:
            return existing.refresh(interaction_id=interaction_id, kind=kind)
        return self.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )

    def get_session(
        self,
        interaction_token: str,
    ) -> Optional[DiscordInteractionSession]:
        return self._sessions.get_session(interaction_token)

    def prepared_interaction_policy(
        self,
        interaction_token: str,
    ) -> Optional[str]:
        session = self.get_session(interaction_token)
        if session is None:
            return None
        return session.prepared_policy()

    async def respond(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        await session.send_message(text, ephemeral=ephemeral)

    async def defer(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        ephemeral: bool = False,
    ) -> bool:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        if ephemeral:
            return await session.defer_ephemeral()
        return await session.defer_public()

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        return await session.defer_component_update()

    async def send_followup(
        self,
        *,
        interaction_id: Optional[str] = None,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ) -> bool:
        session = self.get_session(interaction_token)
        if session is None:
            if interaction_id is None:
                return False
            session = self.session(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        try:
            return await session.send_followup(
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
        except InteractionSessionError:
            return False

    async def edit_original_component_message(
        self,
        *,
        interaction_id: Optional[str] = None,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self.get_session(interaction_token)
        if session is None:
            if interaction_id is None:
                return False
            session = self.session(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        try:
            return await session.edit_original_message(
                text=text,
                components=components,
            )
        except InteractionSessionError:
            return False

    async def send_or_respond(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        if deferred and session.has_initial_response():
            sent = await self.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=text,
                ephemeral=ephemeral,
            )
            if sent:
                return
        await session.send_message(text, ephemeral=ephemeral)

    async def send_or_respond_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        if deferred and session.has_initial_response():
            sent = await self.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=text,
                components=components,
                ephemeral=ephemeral,
            )
            if sent:
                return
        await session.send_message(text, ephemeral=ephemeral, components=components)

    async def respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
        *,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        await session.send_message(text, ephemeral=ephemeral, components=components)

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        await session.update_component_message(text=text, components=components)

    async def respond_autocomplete(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        choices: list[dict[str, str]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.AUTOCOMPLETE,
        )
        await session.respond_autocomplete(choices=choices)

    async def respond_modal(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )
        await session.respond_modal(
            custom_id=custom_id,
            title=title,
            components=components,
        )
