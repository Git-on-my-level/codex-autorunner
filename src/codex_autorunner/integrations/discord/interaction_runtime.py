from __future__ import annotations

from typing import Any, Optional


def interaction_response_deferred(service: Any, interaction_token: str) -> bool:
    has_initial_response = getattr(service, "_interaction_has_initial_response", None)
    if callable(has_initial_response):
        try:
            if has_initial_response(interaction_token):
                return True
        except AttributeError:
            return False
    is_deferred = getattr(service, "_interaction_is_deferred", None)
    if callable(is_deferred):
        try:
            if is_deferred(interaction_token):
                return True
        except AttributeError:
            return False
    return False


async def ensure_component_response_deferred(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(service, interaction_token):
        return True
    defer_component_update = getattr(service, "_defer_component_update", None)
    if not callable(defer_component_update):
        return False
    try:
        return bool(
            await defer_component_update(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        )
    except AttributeError:
        return False


async def ensure_ephemeral_response_deferred(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(service, interaction_token):
        return True
    defer_ephemeral = getattr(service, "_defer_ephemeral", None)
    if not callable(defer_ephemeral):
        return False
    try:
        return bool(
            await defer_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        )
    except AttributeError:
        return False


async def ensure_public_response_deferred(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if interaction_response_deferred(service, interaction_token):
        return True
    defer_public = getattr(service, "_defer_public", None)
    if not callable(defer_public):
        return False
    try:
        return bool(
            await defer_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        )
    except AttributeError:
        return False


async def send_runtime_ephemeral(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    text: str,
) -> None:
    if interaction_response_deferred(service, interaction_token):
        await service._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
        )
        return
    await service._respond_ephemeral(interaction_id, interaction_token, text)


async def send_runtime_components_ephemeral(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    text: str,
    components: list[dict[str, Any]],
) -> None:
    if interaction_response_deferred(service, interaction_token):
        await service._send_or_respond_with_components_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
            components=components,
        )
        return
    await service._respond_with_components(
        interaction_id,
        interaction_token,
        text,
        components,
    )


async def update_runtime_component_message(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    text: str,
    *,
    components: Optional[list[dict[str, Any]]] = None,
) -> None:
    if interaction_response_deferred(service, interaction_token):
        await service._send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=True,
            text=text,
            components=components,
        )
        return
    await service._update_component_message(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        text=text,
        components=components or [],
    )
