from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Optional

from .errors import DiscordAPIError
from .rendering import truncate_for_discord
from .rest import DiscordRestClient

DISCORD_EPHEMERAL_FLAG = 64
PREPARED_INTERACTION_CACHE_LIMIT = 512


class DiscordResponder:
    def __init__(
        self,
        rest: DiscordRestClient,
        config: Any,
        logger: logging.Logger,
    ) -> None:
        self._rest = rest
        self._config = config
        self._logger = logger
        self._prepared_interaction_policies: OrderedDict[str, str] = OrderedDict()

    def prepared_interaction_policy(
        self,
        interaction_token: str,
    ) -> Optional[str]:
        token = interaction_token.strip()
        if not token:
            return None
        policy = self._prepared_interaction_policies.get(token)
        if policy is None:
            return None
        self._prepared_interaction_policies.move_to_end(token)
        return policy

    def remember_prepared_interaction_policy(
        self,
        *,
        interaction_token: str,
        policy: str,
    ) -> None:
        token = interaction_token.strip()
        if not token:
            return
        self._prepared_interaction_policies[token] = policy
        self._prepared_interaction_policies.move_to_end(token)
        while (
            len(self._prepared_interaction_policies) > PREPARED_INTERACTION_CACHE_LIMIT
        ):
            self._prepared_interaction_policies.popitem(last=False)

    async def send_followup(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ) -> bool:
        application_id = (self._config.application_id or "").strip()
        if not application_id:
            return False
        payload: dict[str, Any] = {
            "content": content,
        }
        if ephemeral:
            payload["flags"] = DISCORD_EPHEMERAL_FLAG
        if components:
            payload["components"] = components
        try:
            await self._rest.create_followup_message(
                application_id=application_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except (
            Exception
        ):  # intentional: best-effort followup, any failure returns False
            return False
        return True

    async def edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        application_id = (self._config.application_id or "").strip()
        if not application_id:
            return False
        max_len = max(int(self._config.max_message_length), 32)
        payload: dict[str, Any] = {
            "content": truncate_for_discord(text, max_len=max_len),
            "components": components or [],
        }
        try:
            await self._rest.edit_original_interaction_response(
                application_id=application_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to edit original interaction response: %s",
                exc,
            )
            return False
        return True

    async def respond(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        prepared_policy = self.prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self.edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=[],
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self.send_followup(
                interaction_token=interaction_token,
                content=content,
                ephemeral=ephemeral,
            )
            if sent_followup:
                return
        data: dict[str, Any] = {"content": content}
        if ephemeral:
            data["flags"] = DISCORD_EPHEMERAL_FLAG
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={"type": 4, "data": data},
            )
        except Exception as exc:  # intentional: primary response failed, try followup
            sent_followup = await self.send_followup(
                interaction_token=interaction_token,
                content=content,
                ephemeral=ephemeral,
            )
            if not sent_followup:
                label = "ephemeral" if ephemeral else "public"
                self._logger.error(
                    "Failed to send %s response: %s (interaction_id=%s)",
                    label,
                    exc,
                    interaction_id,
                )

    async def defer(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        ephemeral: bool = False,
    ) -> bool:
        if self.prepared_interaction_policy(interaction_token) is not None:
            return True
        payload: dict[str, Any] = {"type": 5}
        if ephemeral:
            payload["data"] = {"flags": DISCORD_EPHEMERAL_FLAG}
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except Exception as exc:  # intentional: primary response failed, try followup
            label = "ephemeral" if ephemeral else "public"
            self._logger.warning(
                "Failed to defer %s response: %s (interaction_id=%s)",
                label,
                exc,
                interaction_id,
            )
            return False
        policy = "defer_ephemeral" if ephemeral else "defer_public"
        self.remember_prepared_interaction_policy(
            interaction_token=interaction_token,
            policy=policy,
        )
        return True

    async def send_or_respond(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        ephemeral: bool = False,
    ) -> None:
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self.send_followup(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
                ephemeral=ephemeral,
            )
            if sent:
                return
        await self.respond(interaction_id, interaction_token, text, ephemeral=ephemeral)

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
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self.send_followup(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
                components=components,
                ephemeral=ephemeral,
            )
            if sent:
                return
        await self.respond_with_components(
            interaction_id,
            interaction_token,
            text,
            components,
            ephemeral=ephemeral,
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
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        prepared_policy = self.prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self.edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=components,
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self.send_followup(
                interaction_token=interaction_token,
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
            if sent_followup:
                return
        data: dict[str, Any] = {"content": content, "components": components}
        if ephemeral:
            data["flags"] = DISCORD_EPHEMERAL_FLAG
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={"type": 4, "data": data},
            )
        except DiscordAPIError as exc:
            sent_followup = await self.send_followup(
                interaction_token=interaction_token,
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
            if not sent_followup:
                label = "ephemeral" if ephemeral else "public"
                self._logger.error(
                    "Failed to send %s component response: %s (interaction_id=%s)",
                    label,
                    exc,
                    interaction_id,
                )
