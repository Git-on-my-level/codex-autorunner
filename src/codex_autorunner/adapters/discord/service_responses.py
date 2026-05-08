from __future__ import annotations

from typing import Any, Optional

from .effects import (
    DiscordAutocompleteEffect,
    DiscordComponentResponseEffect,
    DiscordComponentUpdateEffect,
    DiscordDeferEffect,
    DiscordEffect,
    DiscordFollowupEffect,
    DiscordModalEffect,
    DiscordOriginalMessageEditEffect,
    DiscordResponseEffect,
)
from .ingress import InteractionKind
from .interaction_session import DiscordInteractionSession, InteractionSessionKind
from .rendering import truncate_for_discord
from .response_helpers import DiscordResponder


class DiscordInteractionResponseMixin:
    _config: Any
    _responder: DiscordResponder

    async def _apply_discord_effect(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        effect: DiscordEffect,
    ) -> None:
        raise NotImplementedError

    def _interaction_session_kind(
        self,
        kind: InteractionKind | InteractionSessionKind | str | None,
    ) -> InteractionSessionKind:
        if isinstance(kind, InteractionSessionKind):
            return kind
        if isinstance(kind, InteractionKind):
            return InteractionSessionKind(kind.value)
        if isinstance(kind, str):
            try:
                return InteractionSessionKind(kind)
            except ValueError:
                return InteractionSessionKind.UNKNOWN
        return InteractionSessionKind.UNKNOWN

    def _ensure_interaction_session(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: InteractionKind | InteractionSessionKind | str | None = None,
    ) -> DiscordInteractionSession:
        return self._responder.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=self._interaction_session_kind(kind),
        )

    def _get_interaction_session(
        self,
        interaction_token: str,
    ) -> Optional[DiscordInteractionSession]:
        return self._responder.get_session(interaction_token)

    def interaction_has_initial_response(
        self,
        interaction_token: str,
    ) -> bool:
        session = self._get_interaction_session(interaction_token)
        return bool(session and session.has_initial_response())

    def interaction_is_deferred(
        self,
        interaction_token: str,
    ) -> bool:
        session = self._get_interaction_session(interaction_token)
        return bool(session and session.is_deferred())

    def _interaction_has_initial_response(
        self,
        interaction_token: str,
    ) -> bool:
        return self.interaction_has_initial_response(interaction_token)

    def _interaction_is_deferred(
        self,
        interaction_token: str,
    ) -> bool:
        return self.interaction_is_deferred(interaction_token)

    async def _apply_interaction_effect(
        self, interaction_id: str, interaction_token: str, effect: DiscordEffect
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=effect,
        )

    async def _apply_response_effect(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool,
        prefer_followup: bool,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        await self._apply_interaction_effect(
            interaction_id,
            interaction_token,
            DiscordResponseEffect(
                text=text,
                ephemeral=ephemeral,
                components=components,
                prefer_followup=prefer_followup,
            ),
        )

    async def respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=True,
            prefer_followup=False,
        )

    async def defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            await self._apply_interaction_effect(
                interaction_id,
                interaction_token,
                DiscordDeferEffect(mode="ephemeral"),
            )
        except Exception:
            return False
        return True

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        session = self._ensure_interaction_session(
            interaction_id,
            interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        if session.has_initial_response():
            return True
        try:
            await self._apply_interaction_effect(
                interaction_id,
                interaction_token,
                DiscordDeferEffect(mode="component_update"),
            )
        except Exception:
            return False
        return True

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=True,
            prefer_followup=True,
        )

    async def send_or_respond_ephemeral_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        _ = deferred
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=True,
            components=components,
            prefer_followup=True,
        )

    async def send_or_update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        await self._apply_interaction_effect(
            interaction_id,
            interaction_token,
            DiscordComponentResponseEffect(
                text=truncate_for_discord(
                    text,
                    max_len=max(int(self._config.max_message_length), 32),
                ),
                deferred=deferred,
                components=components,
            ),
        )

    async def respond_ephemeral_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=True,
            components=components,
            prefer_followup=False,
        )

    async def respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        await self._apply_interaction_effect(
            interaction_id,
            interaction_token,
            DiscordAutocompleteEffect(choices=choices),
        )

    async def respond_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: InteractionSessionKind,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_interaction_effect(
            interaction_id,
            interaction_token,
            DiscordModalEffect(
                kind=kind,
                custom_id=custom_id,
                title=title,
                components=components,
            ),
        )

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_interaction_effect(
            interaction_id,
            interaction_token,
            DiscordComponentUpdateEffect(text=text, components=components),
        )

    async def edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_interaction_effect(
            session.interaction_id if session is not None else "",
            interaction_token,
            DiscordOriginalMessageEditEffect(text=text, components=components),
        )
        return True

    async def send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_interaction_effect(
            session.interaction_id if session is not None else "",
            interaction_token,
            DiscordFollowupEffect(
                content=content,
                ephemeral=True,
                components=components,
            ),
        )
        return True

    async def respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=False,
            prefer_followup=False,
        )

    async def defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            await self._apply_interaction_effect(
                interaction_id,
                interaction_token,
                DiscordDeferEffect(mode="public"),
            )
        except Exception:
            return False
        return True

    async def send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=False,
            prefer_followup=True,
        )

    async def send_or_respond_public_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        _ = deferred
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=False,
            components=components,
            prefer_followup=True,
        )

    async def respond_public_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_response_effect(
            interaction_id,
            interaction_token,
            text,
            ephemeral=False,
            components=components,
            prefer_followup=False,
        )

    async def send_followup_public(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_interaction_effect(
            session.interaction_id if session is not None else "",
            interaction_token,
            DiscordFollowupEffect(
                content=content,
                ephemeral=False,
                components=components,
            ),
        )
        return True

    _respond_ephemeral = respond_ephemeral
    _defer_ephemeral = defer_ephemeral
    _defer_component_update = defer_component_update
    _send_or_respond_ephemeral = send_or_respond_ephemeral
    _send_or_update_component_message = send_or_update_component_message
    _respond_with_components = respond_ephemeral_with_components
    _respond_autocomplete = respond_autocomplete
    _respond_modal = respond_modal
    _update_component_message = update_component_message
    _edit_original_component_message = edit_original_component_message
    _send_followup_ephemeral = send_followup_ephemeral
    _respond_public = respond_public
    _defer_public = defer_public
    _send_or_respond_public = send_or_respond_public
    _respond_with_components_public = respond_public_with_components
    _send_or_respond_with_components_ephemeral = (
        send_or_respond_ephemeral_with_components
    )
    _send_or_respond_with_components_public = send_or_respond_public_with_components
    _send_followup_public = send_followup_public
