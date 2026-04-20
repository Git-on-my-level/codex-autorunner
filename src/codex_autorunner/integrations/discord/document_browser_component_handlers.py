from __future__ import annotations

from typing import Any


async def _handle_tickets_page_component(service: Any, ctx: Any) -> None:
    await service._handle_ticket_browser_page_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        custom_id=ctx.custom_id or "",
    )


async def _handle_tickets_back_component(service: Any, ctx: Any) -> None:
    await service._handle_ticket_browser_back_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
    )


async def _handle_tickets_chunk_component(service: Any, ctx: Any) -> None:
    await service._handle_ticket_browser_chunk_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        custom_id=ctx.custom_id or "",
    )


async def _handle_contextspace_select_component(service: Any, ctx: Any) -> None:
    await service._handle_contextspace_select_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        values=ctx.values,
    )


async def _handle_contextspace_page_component(service: Any, ctx: Any) -> None:
    await service._handle_contextspace_browser_page_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        custom_id=ctx.custom_id or "",
    )


async def _handle_contextspace_back_component(service: Any, ctx: Any) -> None:
    await service._handle_contextspace_back_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
    )


async def _handle_contextspace_chunk_component(service: Any, ctx: Any) -> None:
    await service._handle_contextspace_chunk_component(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        custom_id=ctx.custom_id or "",
    )


__all__ = [
    "_handle_contextspace_back_component",
    "_handle_contextspace_chunk_component",
    "_handle_contextspace_page_component",
    "_handle_contextspace_select_component",
    "_handle_tickets_back_component",
    "_handle_tickets_chunk_component",
    "_handle_tickets_page_component",
]
