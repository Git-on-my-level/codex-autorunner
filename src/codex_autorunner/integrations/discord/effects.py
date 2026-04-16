from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Union

from .interaction_session import InteractionSessionKind

if TYPE_CHECKING:
    from .service import DiscordBotService


@dataclass(frozen=True)
class DiscordResponseEffect:
    text: str
    ephemeral: bool
    components: Optional[list[dict[str, Any]]] = None
    prefer_followup: bool = False


@dataclass(frozen=True)
class DiscordOriginalMessageEditEffect:
    text: str
    components: Optional[list[dict[str, Any]]] = None


@dataclass(frozen=True)
class DiscordComponentUpdateEffect:
    text: str
    components: list[dict[str, Any]]


@dataclass(frozen=True)
class DiscordComponentResponseEffect:
    text: str
    deferred: bool
    components: Optional[list[dict[str, Any]]] = None


@dataclass(frozen=True)
class DiscordModalEffect:
    kind: InteractionSessionKind
    custom_id: str
    title: str
    components: list[dict[str, Any]]


@dataclass(frozen=True)
class DiscordAutocompleteEffect:
    choices: list[dict[str, str]]


@dataclass(frozen=True)
class DiscordFollowupEffect:
    content: str
    ephemeral: bool
    components: Optional[list[dict[str, Any]]] = None


@dataclass(frozen=True)
class DiscordDeferEffect:
    mode: str


DiscordEffect = Union[
    DiscordResponseEffect,
    DiscordOriginalMessageEditEffect,
    DiscordComponentUpdateEffect,
    DiscordComponentResponseEffect,
    DiscordModalEffect,
    DiscordAutocompleteEffect,
    DiscordFollowupEffect,
    DiscordDeferEffect,
]


@dataclass
class DiscordHandlerResult:
    effects: list[DiscordEffect] = field(default_factory=list)

    def add(self, effect: DiscordEffect) -> None:
        self.effects.append(effect)

    def extend(self, effects: list[DiscordEffect]) -> None:
        self.effects.extend(effects)


class DiscordEffectDeliveryError(RuntimeError):
    def __init__(
        self,
        *,
        effect: DiscordEffect,
        interaction_id: str,
        interaction_token: str,
        delivery_status: Optional[str],
        delivery_error: Optional[str] = None,
    ) -> None:
        effect_type = type(effect).__name__
        status = delivery_status or "unknown_delivery_failure"
        message = f"{effect_type} failed via {status}"
        if delivery_error:
            message = f"{message}: {delivery_error}"
        super().__init__(message)
        self.effect = effect
        self.interaction_id = interaction_id
        self.interaction_token = interaction_token
        self.delivery_status = delivery_status
        self.delivery_error = delivery_error


class DiscordEffectSink:
    def __init__(self, service: "DiscordBotService") -> None:
        self._service = service

    async def apply_result(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        result: DiscordHandlerResult,
    ) -> None:
        for effect in result.effects:
            await self.apply(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=effect,
            )

    async def apply(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        effect: DiscordEffect,
    ) -> None:
        if isinstance(effect, DiscordResponseEffect):
            if effect.prefer_followup:
                if effect.components:
                    await self._service._responder.send_or_respond_with_components(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=True,
                        text=effect.text,
                        components=effect.components,
                        ephemeral=effect.ephemeral,
                    )
                else:
                    await self._service._responder.send_or_respond(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=True,
                        text=effect.text,
                        ephemeral=effect.ephemeral,
                    )
            else:
                if effect.components:
                    await self._service._responder.respond_with_components(
                        interaction_id,
                        interaction_token,
                        effect.text,
                        effect.components,
                        ephemeral=effect.ephemeral,
                    )
                else:
                    await self._service._responder.respond(
                        interaction_id,
                        interaction_token,
                        effect.text,
                        ephemeral=effect.ephemeral,
                    )
            self._raise_if_delivery_failed(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=effect,
            )
            return

        if isinstance(effect, DiscordOriginalMessageEditEffect):
            updated = await self._service._responder.edit_original_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=effect.text,
                components=effect.components,
            )
            if not updated:
                self._raise_if_delivery_failed(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    effect=effect,
                )
            return

        if isinstance(effect, DiscordComponentUpdateEffect):
            await self._service._responder.update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=effect.text,
                components=effect.components,
            )
            self._raise_if_delivery_failed(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=effect,
            )
            return

        if isinstance(effect, DiscordComponentResponseEffect):
            if effect.deferred:
                updated = (
                    await self._service._responder.edit_original_component_message(
                        interaction_token=interaction_token,
                        text=effect.text,
                        components=effect.components,
                    )
                )
                if updated:
                    return
                sent = await self._service._responder.send_followup(
                    interaction_token=interaction_token,
                    content=effect.text,
                    components=effect.components,
                    ephemeral=True,
                )
                if not sent:
                    self._raise_if_delivery_failed(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        effect=effect,
                    )
                return
            await self.apply(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=DiscordComponentUpdateEffect(
                    text=effect.text,
                    components=effect.components or [],
                ),
            )
            return

        if isinstance(effect, DiscordModalEffect):
            await self._service._responder.respond_modal(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                kind=effect.kind,
                custom_id=effect.custom_id,
                title=effect.title,
                components=effect.components,
            )
            self._raise_if_delivery_failed(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=effect,
            )
            return

        if isinstance(effect, DiscordAutocompleteEffect):
            await self._service._responder.respond_autocomplete(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                choices=effect.choices,
            )
            self._raise_if_delivery_failed(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=effect,
            )
            return

        if isinstance(effect, DiscordFollowupEffect):
            sent = await self._service._responder.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=effect.content,
                components=effect.components,
                ephemeral=effect.ephemeral,
            )
            if not sent:
                self._raise_if_delivery_failed(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    effect=effect,
                )
            return

        if isinstance(effect, DiscordDeferEffect):
            if effect.mode == "ephemeral":
                deferred = await self._service._responder.defer(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    ephemeral=True,
                )
            elif effect.mode == "public":
                deferred = await self._service._responder.defer(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    ephemeral=False,
                )
            elif effect.mode == "component_update":
                deferred = await self._service._responder.defer_component_update(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                )
            else:
                raise ValueError(f"unknown Discord defer effect mode: {effect.mode}")
            if not deferred:
                self._raise_if_delivery_failed(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    effect=effect,
                )
            return

        raise TypeError(f"unsupported Discord effect: {type(effect)!r}")

    def _raise_if_delivery_failed(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        effect: DiscordEffect,
    ) -> None:
        session = self._service._responder.get_session(interaction_token)
        delivery_status = session.last_delivery_status if session is not None else None
        delivery_error = session.last_delivery_error if session is not None else None
        if delivery_status and (
            delivery_status.endswith("_failed")
            or delivery_status.endswith("_unavailable")
        ):
            raise DiscordEffectDeliveryError(
                effect=effect,
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                delivery_status=delivery_status,
                delivery_error=delivery_error,
            )
        if delivery_status is None:
            raise DiscordEffectDeliveryError(
                effect=effect,
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                delivery_status=None,
                delivery_error=delivery_error,
            )


class DiscordEffectServiceProxy:
    def __init__(
        self,
        service: "DiscordBotService",
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        self._service = service
        self._interaction_id = interaction_id
        self._interaction_token = interaction_token
        self._result = DiscordHandlerResult()
        self._local_initial_response = False

    @property
    def result(self) -> DiscordHandlerResult:
        return self._result

    _PRIVATE_ALIAS_MAP: dict[str, str] = {
        "_defer_ephemeral": "defer_ephemeral",
        "_defer_public": "defer_public",
        "_defer_component_update": "defer_component_update",
        "_respond_ephemeral": "respond_ephemeral",
        "_respond_public": "respond_public",
        "_send_or_respond_ephemeral": "send_or_respond_ephemeral",
        "_send_or_respond_public": "send_or_respond_public",
        "_respond_with_components": "respond_ephemeral_with_components",
        "_respond_with_components_public": "respond_public_with_components",
        "_send_or_respond_with_components_ephemeral": "send_or_respond_ephemeral_with_components",
        "_send_or_respond_with_components_public": "send_or_respond_public_with_components",
        "_send_or_update_component_message": "send_or_update_component_message",
        "_update_component_message": "update_component_message",
        "_edit_original_component_message": "edit_original_component_message",
        "_send_followup_ephemeral": "send_followup_ephemeral",
        "_send_followup_public": "send_followup_public",
        "_respond_modal": "respond_modal",
        "_respond_autocomplete": "respond_autocomplete",
        "_interaction_has_initial_response": "interaction_has_initial_response",
        "_interaction_is_deferred": "interaction_is_deferred",
    }

    def __getattr__(self, name: str) -> Any:
        public_name = self._PRIVATE_ALIAS_MAP.get(name)
        if public_name is not None:
            return getattr(self, public_name)
        return getattr(self._service, name)

    def _mark_initial_response(self) -> None:
        self._local_initial_response = True

    def _buffer_effect(self, effect: DiscordEffect) -> None:
        self._result.add(effect)

    async def _apply_immediate(self, effect: DiscordEffect) -> None:
        await self._service._apply_discord_effect(
            interaction_id=self._interaction_id,
            interaction_token=self._interaction_token,
            effect=effect,
        )
        self._mark_initial_response()

    def interaction_has_initial_response(self, interaction_token: str) -> bool:
        if (
            interaction_token == self._interaction_token
            and self._local_initial_response
        ):
            return True
        return self._service._interaction_has_initial_response(interaction_token)

    def interaction_is_deferred(self, interaction_token: str) -> bool:
        if (
            interaction_token == self._interaction_token
            and self._local_initial_response
        ):
            session = self._service._responder.get_session(interaction_token)
            return bool(session and session.is_deferred())
        return self._service._interaction_is_deferred(interaction_token)

    async def defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        if self.interaction_has_initial_response(interaction_token):
            return True
        try:
            await self._apply_immediate(DiscordDeferEffect(mode="ephemeral"))
        except DiscordEffectDeliveryError:
            return False
        return True

    async def defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        if self.interaction_has_initial_response(interaction_token):
            return True
        try:
            await self._apply_immediate(DiscordDeferEffect(mode="public"))
        except DiscordEffectDeliveryError:
            return False
        return True

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        if self.interaction_has_initial_response(interaction_token):
            return True
        try:
            await self._apply_immediate(DiscordDeferEffect(mode="component_update"))
        except DiscordEffectDeliveryError:
            return False
        return True

    async def respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=True,
                prefer_followup=False,
            )
        )
        self._mark_initial_response()

    async def respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=False,
                prefer_followup=False,
            )
        )
        self._mark_initial_response()

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=True,
                prefer_followup=True,
            )
        )
        self._mark_initial_response()

    async def send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=False,
                prefer_followup=True,
            )
        )
        self._mark_initial_response()

    async def respond_ephemeral_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=True,
                components=components,
                prefer_followup=False,
            )
        )
        self._mark_initial_response()

    async def respond_public_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=False,
                components=components,
                prefer_followup=False,
            )
        )
        self._mark_initial_response()

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
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=True,
                components=components,
                prefer_followup=True,
            )
        )
        self._mark_initial_response()

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
        self._buffer_effect(
            DiscordResponseEffect(
                text=text,
                ephemeral=False,
                components=components,
                prefer_followup=True,
            )
        )
        self._mark_initial_response()

    async def send_or_update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._buffer_effect(
            DiscordComponentResponseEffect(
                text=text,
                deferred=deferred,
                components=components,
            )
        )
        self._mark_initial_response()

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        self._buffer_effect(
            DiscordComponentUpdateEffect(text=text, components=components)
        )
        self._mark_initial_response()

    async def edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        self._buffer_effect(
            DiscordOriginalMessageEditEffect(text=text, components=components)
        )
        self._mark_initial_response()
        return True

    async def send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        self._buffer_effect(
            DiscordFollowupEffect(
                content=content,
                ephemeral=True,
                components=components,
            )
        )
        return True

    async def send_followup_public(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        self._buffer_effect(
            DiscordFollowupEffect(
                content=content,
                ephemeral=False,
                components=components,
            )
        )
        return True

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
        self._buffer_effect(
            DiscordModalEffect(
                kind=kind,
                custom_id=custom_id,
                title=title,
                components=components,
            )
        )
        self._mark_initial_response()

    async def respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        self._buffer_effect(DiscordAutocompleteEffect(choices=choices))
        self._mark_initial_response()
