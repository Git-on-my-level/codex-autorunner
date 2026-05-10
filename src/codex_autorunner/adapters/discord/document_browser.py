from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ...core.chat_document_browser import (
    DocumentBrowserDocument,
    DocumentBrowserSource,
    browser_page_slice,
    list_document_browser_items,
    read_document_browser_document,
)
from .components import (
    build_action_row,
    build_button,
    build_select_menu,
    build_select_option,
)
from .interaction_runtime import (
    ensure_component_response_deferred,
    update_runtime_component_message,
)
from .rendering import chunk_discord_message

_DISCORD_BROWSER_BODY_MAX_LEN = 1400
_DISCORD_BROWSER_PAGE_SIZE = 10


@dataclass
class DiscordDocumentBrowserState:
    source: DocumentBrowserSource
    query: str = ""
    list_page: int = 0
    document_id: Optional[str] = None
    chunk_index: int = 0


def _browser_state_key(channel_id: str, user_id: Optional[str], source: str) -> str:
    normalized_user_id = str(user_id or "unknown").strip() or "unknown"
    return f"{source}:{channel_id}:{normalized_user_id}"


def _parse_custom_id_index(custom_id: str, prefix: str) -> Optional[int]:
    token = str(custom_id or "").strip()
    if not token.startswith(f"{prefix}:"):
        return None
    raw_value = token.split(":", 1)[1].strip()
    if not raw_value.isdigit():
        return None
    return int(raw_value)


async def handle_ticket_browser(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    query = service._normalize_search_query(options.get("search"))
    state = DiscordDocumentBrowserState(source="tickets", query=query)
    _store_browser_state(service, channel_id=channel_id, user_id=user_id, state=state)
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=state.source,
    ):
        content, components = await _render_browser_view_async(
            service,
            workspace_root,
            state,
        )
    await service.respond_ephemeral_with_components(
        interaction_id,
        interaction_token,
        content,
        components,
    )


async def handle_contextspace_browser(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    workspace_root: Path,
) -> None:
    state = DiscordDocumentBrowserState(source="contextspace")
    _store_browser_state(service, channel_id=channel_id, user_id=user_id, state=state)
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=state.source,
    ):
        content, components = await _render_browser_view_async(
            service,
            workspace_root,
            state,
        )
    await service.respond_ephemeral_with_components(
        interaction_id,
        interaction_token,
        content,
        components,
    )


async def handle_ticket_select_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    values: Optional[list[str]],
) -> None:
    await _handle_select_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="tickets",
        values=values,
    )


async def handle_ticket_page_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    custom_id: str,
) -> None:
    await _handle_list_page_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="tickets",
        custom_id=custom_id,
        prefix="tickets_page",
    )


async def handle_ticket_back_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
) -> None:
    await _handle_back_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="tickets",
    )


async def handle_ticket_chunk_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    custom_id: str,
) -> None:
    await _handle_chunk_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="tickets",
        custom_id=custom_id,
        prefix="tickets_chunk",
    )


async def handle_contextspace_select_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    values: Optional[list[str]],
) -> None:
    await _handle_select_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="contextspace",
        values=values,
    )


async def handle_contextspace_back_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
) -> None:
    await _handle_back_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="contextspace",
    )


async def handle_contextspace_chunk_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    custom_id: str,
) -> None:
    await _handle_chunk_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="contextspace",
        custom_id=custom_id,
        prefix="contextspace_chunk",
    )


async def handle_contextspace_page_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    custom_id: str,
) -> None:
    await _handle_list_page_component(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        user_id=user_id,
        source="contextspace",
        custom_id=custom_id,
        prefix="contextspace_page",
    )


async def _handle_select_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
    values: Optional[list[str]],
) -> None:
    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if workspace_root is None:
        return
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=source,
    ):
        state = _get_browser_state(
            service, channel_id=channel_id, user_id=user_id, source=source
        )
        if state is None or not values:
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Document selection expired. Re-run the command and try again.",
            )
            return
        state.document_id = values[0]
        state.chunk_index = 0
        await _update_browser_message(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            state=state,
        )


async def _handle_list_page_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
    custom_id: str,
    prefix: str,
) -> None:
    page = _parse_custom_id_index(custom_id, prefix)
    if page is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Invalid page request.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if workspace_root is None:
        return
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=source,
    ):
        state = _get_browser_state(
            service, channel_id=channel_id, user_id=user_id, source=source
        )
        if state is None:
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Browser state expired. Re-run the command and try again.",
            )
            return
        state.document_id = None
        state.chunk_index = 0
        state.list_page = page
        await _update_browser_message(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            state=state,
        )


async def _handle_back_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
) -> None:
    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if workspace_root is None:
        return
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=source,
    ):
        state = _get_browser_state(
            service, channel_id=channel_id, user_id=user_id, source=source
        )
        if state is None:
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Browser state expired. Re-run the command and try again.",
            )
            return
        state.document_id = None
        state.chunk_index = 0
        await _update_browser_message(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            state=state,
        )


async def _handle_chunk_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
    custom_id: str,
    prefix: str,
) -> None:
    chunk_index = _parse_custom_id_index(custom_id, prefix)
    if chunk_index is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Invalid part request.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if workspace_root is None:
        return
    async with _browser_session_lock(
        service,
        channel_id=channel_id,
        user_id=user_id,
        source=source,
    ):
        state = _get_browser_state(
            service, channel_id=channel_id, user_id=user_id, source=source
        )
        if state is None or not state.document_id:
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Browser state expired. Re-run the command and try again.",
            )
            return
        state.chunk_index = chunk_index
        await _update_browser_message(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            state=state,
        )


async def _update_browser_message(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    state: DiscordDocumentBrowserState,
) -> None:
    deferred = await ensure_component_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    if not deferred:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Discord interaction acknowledgement failed. Please retry.",
        )
        return
    content, components = await _render_browser_view_async(
        service,
        workspace_root,
        state,
    )
    await update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        content,
        components=components,
    )


async def _render_browser_view_async(
    service: Any,
    workspace_root: Path,
    state: DiscordDocumentBrowserState,
) -> tuple[str, list[dict[str, Any]]]:
    render_state = DiscordDocumentBrowserState(
        source=state.source,
        query=state.query,
        list_page=state.list_page,
        document_id=state.document_id,
        chunk_index=state.chunk_index,
    )
    if render_state.source == "tickets":
        content, components = await asyncio.to_thread(
            _render_browser_view,
            service,
            workspace_root,
            render_state,
        )
    else:
        content, components = _render_browser_view(
            service, workspace_root, render_state
        )
    state.list_page = render_state.list_page
    state.document_id = render_state.document_id
    state.chunk_index = render_state.chunk_index
    return content, components


def _render_browser_view(
    service: Any,
    workspace_root: Path,
    state: DiscordDocumentBrowserState,
) -> tuple[str, list[dict[str, Any]]]:
    if state.document_id:
        document = read_document_browser_document(
            workspace_root,
            state.source,
            state.document_id,
        )
        if document is not None:
            return _render_document_view(document, state)
        state.document_id = None
        state.chunk_index = 0
    return _render_list_view(service, workspace_root, state)


def _render_list_view(
    service: Any,
    workspace_root: Path,
    state: DiscordDocumentBrowserState,
) -> tuple[str, list[dict[str, Any]]]:
    items = list_document_browser_items(
        workspace_root,
        state.source,
        query=state.query,
    )
    state.list_page, total_pages, page_items = browser_page_slice(
        items,
        page=state.list_page,
        page_size=_DISCORD_BROWSER_PAGE_SIZE,
    )
    title = "Browse tickets" if state.source == "tickets" else "Browse contextspace"
    if state.query:
        title = f"{title} matching `{state.query}`"
    lines = [title]
    if total_pages > 1:
        lines.append(f"Page {state.list_page + 1}/{total_pages}")
    if not page_items:
        lines.extend(("", "No documents matched this query."))
        return "\n".join(lines), []
    lines.append("")
    for item in page_items:
        lines.append(f"- {item.label}")
        lines.append(f"  {item.description}")
    components = []
    options = [
        build_select_option(
            label=item.label,
            value=item.document_id,
            description=item.description,
        )
        for item in page_items
    ]
    select_id = "tickets_select" if state.source == "tickets" else "contextspace_select"
    placeholder = (
        "Select a ticket..." if state.source == "tickets" else "Select a document..."
    )
    components.append(
        build_action_row(
            [
                build_select_menu(
                    select_id,
                    options,
                    placeholder=placeholder,
                )
            ]
        )
    )
    if total_pages > 1:
        page_prefix = (
            "tickets_page" if state.source == "tickets" else "contextspace_page"
        )
        buttons = [
            build_button(
                "Prev",
                f"{page_prefix}:{state.list_page - 1}",
                disabled=state.list_page <= 0,
            ),
            build_button(
                "Next",
                f"{page_prefix}:{state.list_page + 1}",
                disabled=state.list_page >= (total_pages - 1),
            ),
        ]
        components.append(build_action_row(buttons))
    return "\n".join(lines), components


def _render_document_view(
    document: DocumentBrowserDocument,
    state: DiscordDocumentBrowserState,
) -> tuple[str, list[dict[str, Any]]]:
    lines = [document.title, document.description, document.rel_path]
    if not document.exists:
        lines.extend(("", "This file is missing from the current workspace."))
        back_id = (
            "tickets_back" if document.source == "tickets" else "contextspace_back"
        )
        return "\n".join(lines), [build_action_row([build_button("Back", back_id)])]
    chunks = chunk_discord_message(
        document.content.strip() or "(File is empty.)",
        max_len=_DISCORD_BROWSER_BODY_MAX_LEN,
        with_numbering=False,
    )
    if not chunks:
        chunks = ["(File is empty.)"]
    state.chunk_index = max(0, min(state.chunk_index, len(chunks) - 1))
    lines.extend(
        (
            "",
            f"Part {state.chunk_index + 1}/{len(chunks)}",
            "",
            chunks[state.chunk_index],
        )
    )
    back_id = "tickets_back" if document.source == "tickets" else "contextspace_back"
    chunk_prefix = (
        "tickets_chunk" if document.source == "tickets" else "contextspace_chunk"
    )
    buttons = [build_button("Back", back_id)]
    if state.chunk_index > 0:
        buttons.append(build_button("Prev", f"{chunk_prefix}:{state.chunk_index - 1}"))
    if state.chunk_index < (len(chunks) - 1):
        buttons.append(build_button("Next", f"{chunk_prefix}:{state.chunk_index + 1}"))
    return "\n".join(lines), [build_action_row(buttons)]


def _store_browser_state(
    service: Any,
    *,
    channel_id: str,
    user_id: Optional[str],
    state: DiscordDocumentBrowserState,
) -> None:
    service._document_browser_sessions[
        _browser_state_key(channel_id, user_id, state.source)
    ] = state


def _get_browser_state(
    service: Any,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
) -> Optional[DiscordDocumentBrowserState]:
    return cast(
        Optional[DiscordDocumentBrowserState],
        service._document_browser_sessions.get(
            _browser_state_key(channel_id, user_id, source)
        ),
    )


def _browser_session_locks(service: Any) -> dict[str, asyncio.Lock]:
    locks = getattr(service, "_document_browser_session_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        service._document_browser_session_locks = locks
    return cast(dict[str, asyncio.Lock], locks)


def _browser_session_lock(
    service: Any,
    *,
    channel_id: str,
    user_id: Optional[str],
    source: DocumentBrowserSource,
) -> asyncio.Lock:
    key = _browser_state_key(channel_id, user_id, source)
    locks = _browser_session_locks(service)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock
