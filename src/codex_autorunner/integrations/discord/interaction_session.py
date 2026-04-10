from __future__ import annotations

import enum
import logging
from collections import OrderedDict
from typing import Any, Optional

from .components import DISCORD_SELECT_OPTION_MAX_OPTIONS
from .errors import DiscordAPIError
from .rendering import truncate_for_discord
from .rest import DiscordRestClient

DISCORD_EPHEMERAL_FLAG = 64
INTERACTION_SESSION_CACHE_LIMIT = 512


class InteractionSessionError(RuntimeError):
    """Raised when a Discord interaction is used through an invalid response path."""


class InteractionSessionKind(str, enum.Enum):
    UNKNOWN = "unknown"
    SLASH_COMMAND = "slash_command"
    COMPONENT = "component"
    MODAL_SUBMIT = "modal_submit"
    AUTOCOMPLETE = "autocomplete"


class InteractionInitialResponse(str, enum.Enum):
    IMMEDIATE_MESSAGE = "immediate_message"
    DEFER_PUBLIC = "defer_public"
    DEFER_EPHEMERAL = "defer_ephemeral"
    DEFER_COMPONENT_UPDATE = "defer_component_update"
    COMPONENT_UPDATE = "component_update"
    AUTOCOMPLETE = "autocomplete"
    MODAL = "modal"


class DiscordInteractionSession:
    def __init__(
        self,
        *,
        rest: DiscordRestClient,
        config: Any,
        logger: logging.Logger,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
    ) -> None:
        self._rest = rest
        self._config = config
        self._logger = logger
        self.interaction_id = interaction_id
        self.interaction_token = interaction_token
        self.kind = kind
        self.initial_response: Optional[InteractionInitialResponse] = None

    def refresh(
        self,
        *,
        interaction_id: Optional[str] = None,
        kind: Optional[InteractionSessionKind] = None,
    ) -> "DiscordInteractionSession":
        if interaction_id:
            self.interaction_id = interaction_id
        if kind is not None and kind != InteractionSessionKind.UNKNOWN:
            self.kind = kind
        return self

    def has_initial_response(self) -> bool:
        return self.initial_response is not None

    def is_deferred(self) -> bool:
        return self.initial_response in {
            InteractionInitialResponse.DEFER_PUBLIC,
            InteractionInitialResponse.DEFER_EPHEMERAL,
            InteractionInitialResponse.DEFER_COMPONENT_UPDATE,
        }

    def prepared_policy(self) -> Optional[str]:
        if self.initial_response in {
            InteractionInitialResponse.DEFER_PUBLIC,
            InteractionInitialResponse.DEFER_EPHEMERAL,
            InteractionInitialResponse.DEFER_COMPONENT_UPDATE,
        }:
            return self.initial_response.value
        return None

    def _max_message_length(self) -> int:
        return max(int(self._config.max_message_length), 32)

    def _application_id(self) -> str:
        return (self._config.application_id or "").strip()

    def _sanitize_content(self, text: str) -> str:
        return truncate_for_discord(text, max_len=self._max_message_length())

    def _set_initial_response(self, mode: InteractionInitialResponse) -> None:
        if self.initial_response is not None:
            raise InteractionSessionError(
                f"interaction {self.interaction_id} already acknowledged via "
                f"{self.initial_response.value}"
            )
        self.initial_response = mode

    def _require_unacknowledged(
        self,
        *,
        operation: str,
        allowed_kinds: Optional[set[InteractionSessionKind]] = None,
    ) -> None:
        if allowed_kinds is not None and self.kind not in allowed_kinds:
            allowed = ", ".join(kind.value for kind in sorted(allowed_kinds, key=str))
            raise InteractionSessionError(
                f"{operation} is not allowed for {self.kind.value} interactions "
                f"(expected one of: {allowed})"
            )
        if self.initial_response is not None:
            raise InteractionSessionError(
                f"{operation} is not allowed after {self.initial_response.value}"
            )

    def _require_followup_allowed(self, *, operation: str) -> None:
        if self.initial_response is None:
            raise InteractionSessionError(
                f"{operation} requires an acknowledged interaction"
            )
        if self.initial_response in {
            InteractionInitialResponse.AUTOCOMPLETE,
            InteractionInitialResponse.MODAL,
        }:
            raise InteractionSessionError(
                f"{operation} is not allowed after {self.initial_response.value}"
            )

    def _require_original_edit_allowed(self, *, operation: str) -> None:
        if self.initial_response not in {
            InteractionInitialResponse.DEFER_PUBLIC,
            InteractionInitialResponse.DEFER_EPHEMERAL,
            InteractionInitialResponse.DEFER_COMPONENT_UPDATE,
        }:
            mode = self.initial_response.value if self.initial_response else "none"
            raise InteractionSessionError(
                f"{operation} requires a deferred interaction state, found {mode}"
            )

    async def _create_initial_response(
        self,
        *,
        payload: dict[str, Any],
        mode: InteractionInitialResponse,
    ) -> bool:
        self._set_initial_response(mode)
        try:
            await self._rest.create_interaction_response(
                interaction_id=self.interaction_id,
                interaction_token=self.interaction_token,
                payload=payload,
            )
        except Exception:
            self.initial_response = None
            raise
        return True

    async def _send_followup_payload(self, payload: dict[str, Any]) -> bool:
        application_id = self._application_id()
        if not application_id:
            return False
        try:
            await self._rest.create_followup_message(
                application_id=application_id,
                interaction_token=self.interaction_token,
                payload=payload,
            )
        except Exception:
            return False
        return True

    async def defer_public(self) -> bool:
        self._require_unacknowledged(operation="defer_public")
        try:
            return await self._create_initial_response(
                payload={"type": 5},
                mode=InteractionInitialResponse.DEFER_PUBLIC,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to defer public response: %s (interaction_id=%s)",
                exc,
                self.interaction_id,
            )
            return False

    async def defer_ephemeral(self) -> bool:
        self._require_unacknowledged(operation="defer_ephemeral")
        try:
            return await self._create_initial_response(
                payload={"type": 5, "data": {"flags": DISCORD_EPHEMERAL_FLAG}},
                mode=InteractionInitialResponse.DEFER_EPHEMERAL,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to defer ephemeral response: %s (interaction_id=%s)",
                exc,
                self.interaction_id,
            )
            return False

    async def defer_component_update(self) -> bool:
        self._require_unacknowledged(
            operation="defer_component_update",
            allowed_kinds={InteractionSessionKind.COMPONENT},
        )
        try:
            return await self._create_initial_response(
                payload={"type": 6},
                mode=InteractionInitialResponse.DEFER_COMPONENT_UPDATE,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to defer component update: %s (interaction_id=%s)",
                exc,
                self.interaction_id,
            )
            return False

    async def send_message(
        self,
        text: str,
        *,
        ephemeral: bool = False,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        content = self._sanitize_content(text)
        if self.initial_response is None:
            data: dict[str, Any] = {"content": content}
            if components is not None:
                data["components"] = components
            if ephemeral:
                data["flags"] = DISCORD_EPHEMERAL_FLAG
            try:
                await self._create_initial_response(
                    payload={"type": 4, "data": data},
                    mode=InteractionInitialResponse.IMMEDIATE_MESSAGE,
                )
                return
            except Exception as exc:
                sent_followup = await self._send_followup_payload(
                    self._followup_payload(
                        content=content,
                        components=components,
                        ephemeral=ephemeral,
                    )
                )
                if sent_followup:
                    self.initial_response = InteractionInitialResponse.IMMEDIATE_MESSAGE
                    return
                label = "ephemeral" if ephemeral else "public"
                self._logger.error(
                    "Failed to send %s response: %s (interaction_id=%s)",
                    label,
                    exc,
                    self.interaction_id,
                )
                return

        if self.initial_response == InteractionInitialResponse.DEFER_COMPONENT_UPDATE:
            updated = await self.edit_original_message(
                text=content,
                components=components or [],
            )
            if updated:
                return

        self._require_followup_allowed(operation="send_message")
        sent_followup = await self._send_followup_payload(
            self._followup_payload(
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
        )
        if not sent_followup:
            label = "ephemeral" if ephemeral else "public"
            self._logger.error(
                "Failed to send %s followup: interaction_id=%s",
                label,
                self.interaction_id,
            )

    async def send_followup(
        self,
        *,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ) -> bool:
        self._require_followup_allowed(operation="send_followup")
        return await self._send_followup_payload(
            self._followup_payload(
                content=self._sanitize_content(content),
                components=components,
                ephemeral=ephemeral,
            )
        )

    async def edit_original_message(
        self,
        *,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        self._require_original_edit_allowed(operation="edit_original_message")
        application_id = self._application_id()
        if not application_id:
            return False
        payload: dict[str, Any] = {"content": self._sanitize_content(text)}
        if components is not None:
            payload["components"] = components
        try:
            await self._rest.edit_original_interaction_response(
                application_id=application_id,
                interaction_token=self.interaction_token,
                payload=payload,
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to edit original interaction response: %s",
                exc,
            )
            return False
        return True

    async def update_component_message(
        self,
        *,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        content = self._sanitize_content(text)
        if self.initial_response is None:
            self._require_unacknowledged(
                operation="update_component_message",
                allowed_kinds={InteractionSessionKind.COMPONENT},
            )
            try:
                await self._create_initial_response(
                    payload={
                        "type": 7,
                        "data": {
                            "content": content,
                            "components": components,
                        },
                    },
                    mode=InteractionInitialResponse.COMPONENT_UPDATE,
                )
                return
            except DiscordAPIError as exc:
                sent_followup = await self._send_followup_payload(
                    self._followup_payload(
                        content=content,
                        components=components,
                        ephemeral=True,
                    )
                )
                if sent_followup:
                    self.initial_response = InteractionInitialResponse.COMPONENT_UPDATE
                    return
                self._logger.error(
                    "Failed to update component message: %s (interaction_id=%s)",
                    exc,
                    self.interaction_id,
                )
                return
        if self.initial_response != InteractionInitialResponse.DEFER_COMPONENT_UPDATE:
            raise InteractionSessionError(
                "update_component_message requires an unacknowledged component "
                "interaction or defer_component_update"
            )
        updated = await self.edit_original_message(text=content, components=components)
        if updated:
            return
        await self._send_followup_payload(
            self._followup_payload(
                content=content,
                components=components,
                ephemeral=True,
            )
        )

    async def respond_autocomplete(
        self,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        self._require_unacknowledged(
            operation="respond_autocomplete",
            allowed_kinds={InteractionSessionKind.AUTOCOMPLETE},
        )
        sanitized_choices: list[dict[str, str]] = []
        for choice in choices[:DISCORD_SELECT_OPTION_MAX_OPTIONS]:
            name = choice.get("name", "")
            value = choice.get("value", "")
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            normalized_name = name.strip()
            normalized_value = value.strip()
            if not normalized_name or not normalized_value:
                continue
            sanitized_choices.append(
                {
                    "name": normalized_name[:100],
                    "value": normalized_value[:100],
                }
            )
        try:
            await self._create_initial_response(
                payload={"type": 8, "data": {"choices": sanitized_choices}},
                mode=InteractionInitialResponse.AUTOCOMPLETE,
            )
        except Exception as exc:
            self._logger.error(
                "Failed to send autocomplete response: %s (interaction_id=%s)",
                exc,
                self.interaction_id,
            )

    async def respond_modal(
        self,
        *,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None:
        self._require_unacknowledged(
            operation="respond_modal",
            allowed_kinds={
                InteractionSessionKind.SLASH_COMMAND,
                InteractionSessionKind.COMPONENT,
            },
        )
        try:
            await self._create_initial_response(
                payload={
                    "type": 9,
                    "data": {
                        "custom_id": custom_id[:100],
                        "title": title[:45],
                        "components": components,
                    },
                },
                mode=InteractionInitialResponse.MODAL,
            )
        except Exception as exc:
            self._logger.error(
                "Failed to send modal response: %s (interaction_id=%s)",
                exc,
                self.interaction_id,
            )

    def _followup_payload(
        self,
        *,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if components:
            payload["components"] = components
        if ephemeral:
            payload["flags"] = DISCORD_EPHEMERAL_FLAG
        return payload


class DiscordInteractionSessionManager:
    def __init__(
        self,
        *,
        rest: DiscordRestClient,
        config: Any,
        logger: logging.Logger,
    ) -> None:
        self._rest = rest
        self._config = config
        self._logger = logger
        self._sessions: OrderedDict[str, DiscordInteractionSession] = OrderedDict()

    def start_session(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
    ) -> DiscordInteractionSession:
        token = interaction_token.strip()
        if not token:
            raise InteractionSessionError("interaction token is required")
        session = self._sessions.get(token)
        if session is None:
            session = DiscordInteractionSession(
                rest=self._rest,
                config=self._config,
                logger=self._logger,
                interaction_id=interaction_id,
                interaction_token=token,
                kind=kind,
            )
            self._sessions[token] = session
        else:
            session.refresh(interaction_id=interaction_id, kind=kind)
            self._sessions.move_to_end(token)
        while len(self._sessions) > INTERACTION_SESSION_CACHE_LIMIT:
            self._sessions.popitem(last=False)
        return session

    def get_session(
        self,
        interaction_token: str,
    ) -> Optional[DiscordInteractionSession]:
        token = interaction_token.strip()
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        self._sessions.move_to_end(token)
        return session
