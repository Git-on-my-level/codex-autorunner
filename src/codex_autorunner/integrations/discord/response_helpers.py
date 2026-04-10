from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

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
        *,
        hydrate_ack_mode: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        record_ack: Optional[
            Callable[[str, str, str, Optional[str]], Awaitable[None]]
        ] = None,
        record_delivery: Optional[
            Callable[[str, str, Optional[str], Optional[str]], Awaitable[None]]
        ] = None,
    ) -> None:
        self._sessions = DiscordInteractionSessionManager(
            rest=rest,
            config=config,
            logger=logger,
        )
        self._hydrate_ack_mode = hydrate_ack_mode
        self._record_ack = record_ack
        self._record_delivery = record_delivery

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

    async def _restore_ack_if_needed(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
    ) -> None:
        if session.has_initial_response() or self._hydrate_ack_mode is None:
            return
        ack_mode = await self._hydrate_ack_mode(interaction_id)
        if isinstance(ack_mode, str) and ack_mode.strip():
            session.restore_initial_response(ack_mode)

    async def _ensure_restored_session_state(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
    ) -> tuple[bool, bool]:
        had_initial_response = session.has_initial_response()
        if had_initial_response:
            return True, False
        await self._restore_ack_if_needed(
            session=session,
            interaction_id=interaction_id,
        )
        restored_initial_response = session.has_initial_response()
        return restored_initial_response, restored_initial_response

    async def _persist_session_state(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
        interaction_token: str,
        had_initial_response: bool,
    ) -> None:
        if (
            not had_initial_response
            and session.has_initial_response()
            and self._record_ack is not None
            and session.initial_response is not None
        ):
            await self._record_ack(
                interaction_id,
                interaction_token,
                session.initial_response.value,
                session.last_delivery_message_id,
            )
        if (
            self._record_delivery is not None
            and session.last_delivery_status is not None
        ):
            await self._record_delivery(
                interaction_id,
                session.last_delivery_status,
                session.last_delivery_error,
                session.last_delivery_message_id,
            )

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        await session.send_message(text, ephemeral=ephemeral)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response, restored_initial_response = (
            await self._ensure_restored_session_state(
                session=session,
                interaction_id=interaction_id,
            )
        )
        if restored_initial_response:
            return True
        if ephemeral:
            deferred = await session.defer_ephemeral()
        else:
            deferred = await session.defer_public()
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        return deferred

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
        had_initial_response, restored_initial_response = (
            await self._ensure_restored_session_state(
                session=session,
                interaction_id=interaction_id,
            )
        )
        if restored_initial_response:
            return True
        deferred = await session.defer_component_update()
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        return deferred

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
        )
        try:
            sent = await session.send_followup(
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
        except InteractionSessionError:
            return False
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        return sent

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
        )
        try:
            updated = await session.edit_original_message(
                text=text,
                components=components,
            )
        except InteractionSessionError:
            return False
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        return updated

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
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
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
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
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        await session.send_message(text, ephemeral=ephemeral, components=components)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        await session.update_component_message(text=text, components=components)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response = session.has_initial_response()
        await session.respond_autocomplete(choices=choices)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )

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
        had_initial_response = session.has_initial_response()
        await session.respond_modal(
            custom_id=custom_id,
            title=title,
            components=components,
        )
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
