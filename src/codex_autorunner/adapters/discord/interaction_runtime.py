from __future__ import annotations

from typing import Any, Optional, Protocol

# This module is the only handler-facing boundary for Discord interaction
# ack/defer state. Business handlers should use these helpers instead of
# reaching into low-level service defer/followup/session methods directly.


class DiscordInteractionRuntime(Protocol):
    def interaction_has_initial_response(self, interaction_token: str) -> bool: ...

    def interaction_is_deferred(self, interaction_token: str) -> bool: ...

    async def defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool: ...

    async def defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool: ...

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool: ...

    async def respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None: ...

    async def respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None: ...

    async def respond_ephemeral_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def respond_public_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None: ...

    async def send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None: ...

    async def send_or_respond_ephemeral_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def send_or_respond_public_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def send_or_update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None: ...

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool: ...

    async def send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool: ...

    async def send_followup_public(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool: ...

    async def respond_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: Any,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None: ...

    async def respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None: ...


def interaction_response_deferred(
    runtime: DiscordInteractionRuntime, interaction_token: str
) -> bool:
    return runtime.interaction_has_initial_response(
        interaction_token
    ) or runtime.interaction_is_deferred(interaction_token)


async def ensure_component_response_deferred(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(runtime, interaction_token):
        return True
    return bool(
        await runtime.defer_component_update(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
    )


async def ensure_ephemeral_response_deferred(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(runtime, interaction_token):
        return True
    return bool(
        await runtime.defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
    )


async def ensure_public_response_deferred(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(runtime, interaction_token):
        return True
    return bool(
        await runtime.defer_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
    )


async def send_runtime_ephemeral(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
    text: str,
) -> None:
    if interaction_response_deferred(runtime, interaction_token):
        await runtime.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
        )
        return
    await runtime.respond_ephemeral(interaction_id, interaction_token, text)


async def send_runtime_components_ephemeral(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
    text: str,
    components: list[dict[str, Any]],
) -> None:
    if interaction_response_deferred(runtime, interaction_token):
        await runtime.send_or_respond_ephemeral_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
            components=components,
        )
        return
    await runtime.respond_ephemeral_with_components(
        interaction_id,
        interaction_token,
        text,
        components,
    )


async def update_runtime_component_message(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
    text: str,
    *,
    components: Optional[list[dict[str, Any]]] = None,
) -> None:
    if interaction_response_deferred(runtime, interaction_token):
        await runtime.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
            components=components,
        )
        return
    await runtime.update_component_message(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        text=text,
        components=components or [],
    )


async def defer_and_update_runtime_component_message(
    runtime: DiscordInteractionRuntime,
    interaction_id: str,
    interaction_token: str,
    text: str,
    *,
    components: Optional[list[dict[str, Any]]] = None,
) -> bool:
    deferred = await ensure_component_response_deferred(
        runtime,
        interaction_id,
        interaction_token,
    )
    await runtime.send_or_update_component_message(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
        components=components,
    )
    return deferred
