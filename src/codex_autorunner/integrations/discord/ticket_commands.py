from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...tickets.snapshot import list_ticket_snapshots
from .interaction_runtime import (
    ensure_component_response_deferred,
    update_runtime_component_message,
)

_logger = logging.getLogger(__name__)

TICKET_PICKER_TOKEN_PREFIX = "ticket@"


def _ticket_dir(workspace_root: Path) -> Path:
    return workspace_root / ".codex-autorunner" / "tickets"


def _ticket_prompt_text(*, search_query: str = "") -> str:
    normalized_query = search_query.strip()
    if not normalized_query:
        return "Select a ticket to view or edit."
    return f"Select a ticket to view or edit. Search: `{normalized_query}`"


def _ticket_picker_value(service: Any, ticket_rel: str) -> str:
    normalized = ticket_rel.strip()
    if len(normalized) <= 100:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{TICKET_PICKER_TOKEN_PREFIX}{digest}"


def _list_ticket_choices(
    service: Any,
    workspace_root: Path,
    *,
    status_filter: str,
    search_query: str = "",
) -> list[tuple[str, str, str]]:
    from ...integrations.chat.picker_filter import filter_picker_items

    snapshots = list_ticket_snapshots(workspace_root, status_filter=status_filter)
    choices: list[tuple[str, str, str]] = [
        (snap.rel_path, snap.label, "done" if snap.is_done else "open")
        for snap in snapshots
    ]
    normalized_query = search_query.strip()
    if not normalized_query or not choices:
        return choices
    search_items = [
        (value, f"{label} {description}".strip())
        for value, label, description in choices
    ]
    filtered_items = filter_picker_items(
        search_items,
        normalized_query,
        limit=len(search_items),
    )
    choice_by_value = {
        value: (value, label, description) for value, label, description in choices
    }
    return [
        choice_by_value[value]
        for value, _label in filtered_items
        if value in choice_by_value
    ]


def _resolve_ticket_picker_value(
    service: Any,
    selected_value: str,
    *,
    workspace_root: Path,
) -> Optional[str]:
    from ...tickets.files import list_ticket_paths, safe_relpath

    normalized = selected_value.strip()
    if not normalized:
        return None
    if not normalized.startswith(TICKET_PICKER_TOKEN_PREFIX):
        return normalized

    digest = normalized[len(TICKET_PICKER_TOKEN_PREFIX) :]
    if not digest:
        return None

    for path in list_ticket_paths(_ticket_dir(workspace_root)):
        rel_path = safe_relpath(path, workspace_root)
        candidate = _ticket_picker_value(service, rel_path)
        if candidate == normalized:
            return rel_path
    return None


def _build_ticket_components(
    service: Any,
    workspace_root: Path,
    *,
    status_filter: str,
    search_query: str = "",
) -> list[dict[str, Any]]:
    from .components import build_ticket_filter_picker, build_ticket_picker

    ticket_choices = _list_ticket_choices(
        service,
        workspace_root,
        status_filter=status_filter,
        search_query=search_query,
    )
    return [
        build_ticket_filter_picker(current_filter=status_filter),
        build_ticket_picker(
            [
                (_ticket_picker_value(service, value), label, description)
                for value, label, description in ticket_choices
            ]
        ),
    ]


async def handle_ticket_filter_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    values: Optional[list[str]],
) -> None:
    if not values:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Please select a filter and try again.",
        )
        return
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
    workspace_root = await service._require_bound_workspace(
        interaction_id, interaction_token, channel_id=channel_id
    )
    if not workspace_root:
        return
    status_filter = values[0].strip().lower()
    if status_filter not in {"all", "open", "done"}:
        status_filter = "all"
    search_query = service._pending_ticket_search_queries.get(channel_id, "")
    service._pending_ticket_filters[channel_id] = status_filter
    await update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        _ticket_prompt_text(search_query=search_query),
        components=_build_ticket_components(
            service,
            workspace_root,
            status_filter=status_filter,
            search_query=search_query,
        ),
    )


async def _open_ticket_modal(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    ticket_rel: str,
) -> None:
    from .effects import DiscordModalEffect, InteractionSessionKind
    from .interaction_registry import TICKETS_MODAL_PREFIX
    from .service import TICKETS_BODY_INPUT_ID

    ticket_dir_path = _ticket_dir(workspace_root).resolve()
    candidate = (workspace_root / ticket_rel).resolve()
    try:
        candidate.relative_to(ticket_dir_path)
    except ValueError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket path is invalid. Re-open the ticket list and try again.",
        )
        return
    if not candidate.exists() or not candidate.is_file():
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket file not found. Re-open the ticket list and try again.",
        )
        return
    try:
        ticket_text = await asyncio.wait_for(
            asyncio.to_thread(candidate.read_text, encoding="utf-8"),
            timeout=1.5,
        )
    except asyncio.TimeoutError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                "Ticket load timed out before opening the modal. "
                "Try again or edit the file directly."
            ),
        )
        return
    except Exception as exc:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Failed to read ticket: {exc}",
        )
        return
    max_len = 4000
    if len(ticket_text) > max_len:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                f"`{ticket_rel}` is too large to edit in a Discord modal "
                f"({len(ticket_text)} characters; limit {max_len}). "
                "Use the web UI or edit the file directly."
            ),
        )
        return

    token = uuid.uuid4().hex[:12]
    service._pending_ticket_context[token] = {
        "workspace_root": str(workspace_root),
        "ticket_rel": ticket_rel,
    }

    title = "Edit ticket"
    await service._apply_discord_effect(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        effect=DiscordModalEffect(
            kind=InteractionSessionKind.COMPONENT,
            custom_id=f"{TICKETS_MODAL_PREFIX}:{token}",
            title=title,
            components=[
                {
                    "type": 18,
                    "label": "Ticket"[:45],
                    "component": {
                        "type": 4,
                        "custom_id": TICKETS_BODY_INPUT_ID,
                        "style": 2,
                        "value": ticket_text[:4000],
                        "required": True,
                        "max_length": 4000,
                    },
                },
            ],
        ),
    )


async def handle_ticket_select_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    values: Optional[list[str]],
) -> None:
    if not values:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Please select a ticket and try again.",
        )
        return
    if values[0] == "none":
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "No tickets available for this filter.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id, interaction_token, channel_id=channel_id
    )
    if not workspace_root:
        return
    ticket_rel = _resolve_ticket_picker_value(
        service,
        values[0],
        workspace_root=workspace_root,
    )
    if not ticket_rel:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket selection is invalid. Re-open the ticket list and try again.",
        )
        return
    await _open_ticket_modal(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        ticket_rel=ticket_rel,
    )


async def handle_ticket_modal_submit(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    values: dict[str, Any],
) -> None:
    from ...tickets.files import safe_relpath
    from .interaction_registry import TICKETS_MODAL_PREFIX
    from .service import TICKETS_BODY_INPUT_ID

    if not custom_id.startswith(f"{TICKETS_MODAL_PREFIX}:"):
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Unknown modal submission.",
        )
        return

    token = custom_id.split(":", 1)[1].strip()
    context = service._pending_ticket_context.pop(token, None)
    if not context:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "This ticket modal has expired. Re-open it and try again.",
        )
        return

    ticket_rel = context.get("ticket_rel")
    if not isinstance(ticket_rel, str) or not ticket_rel or ticket_rel == "none":
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "This ticket selection expired. Re-run `/car tickets` and choose one.",
        )
        return

    ticket_body_raw = values.get(TICKETS_BODY_INPUT_ID)
    ticket_body = ticket_body_raw if isinstance(ticket_body_raw, str) else None
    if ticket_body is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket content is missing. Please try again.",
        )
        return

    workspace_root = Path(context.get("workspace_root", "")).expanduser()
    ticket_dir_path = _ticket_dir(workspace_root).resolve()
    candidate = (workspace_root / ticket_rel).resolve()
    try:
        candidate.relative_to(ticket_dir_path)
    except ValueError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket path is invalid. Re-open the ticket and try again.",
        )
        return

    try:
        candidate.write_text(ticket_body, encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        log_event(
            _logger,
            logging.WARNING,
            "discord.ticket.write_failed",
            path=str(candidate),
            exc=exc,
        )
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Failed to write ticket: {exc}",
        )
        return

    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Saved {safe_relpath(candidate, workspace_root)}.",
    )
