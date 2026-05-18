from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import HTTPException, Request

from ..routes.file_chat_routes.drafts import (
    apply_file_patch as apply_draft_patch,
)
from ..routes.file_chat_routes.drafts import (
    discard_file_patch as discard_draft_patch,
)
from ..routes.file_chat_routes.drafts import (
    pending_file_patch as pending_draft_patch,
)
from ..routes.file_chat_routes.execution import (
    execute_file_chat as execute_file_chat_agent_turn,
)
from ..routes.file_chat_routes.execution import (
    resolve_file_chat_agent_selection,
)
from ..routes.file_chat_routes.execution_agents import FileChatError
from ..routes.file_chat_routes.runtime import (
    active_for_client,
    begin_turn_state,
    clear_interrupt_event,
    finalize_turn_state,
    get_state,
    last_for_client,
    update_turn_state,
)
from ..routes.file_chat_routes.targets import (
    FileChatTarget,
    parse_target,
    resolve_repo_root,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileChatTurnRequest:
    target: str
    message: str
    agent: object = "codex"
    profile: object = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    client_turn_id: Optional[str] = None
    already_running_detail: str = "File chat already running"


@dataclass(frozen=True)
class FileChatStreamItem:
    event: str
    payload: Any


@dataclass(frozen=True)
class FileChatActiveSnapshot:
    active: bool
    current: Dict[str, Any]
    last_result: Dict[str, Any]


@dataclass(frozen=True)
class FileChatInterruptResult:
    status: str
    detail: str


@dataclass(frozen=True)
class FileChatThreadResetResult:
    status: str
    index: int
    target: str
    chat_scope: str
    key: str
    cleared: bool


def resolve_target(request: Request, target_raw: str) -> FileChatTarget:
    return parse_target(resolve_repo_root(request), target_raw)


def resolve_ticket_target(request: Request, index: int) -> FileChatTarget:
    return resolve_target(request, f"ticket:{int(index)}")


async def get_active_snapshot(
    request: Request, client_turn_id: Optional[str]
) -> FileChatActiveSnapshot:
    current = await active_for_client(request, client_turn_id)
    last = await last_for_client(request, client_turn_id)
    return FileChatActiveSnapshot(
        active=bool(current),
        current=current,
        last_result=last,
    )


async def _claim_turn(
    request: Request,
    target: FileChatTarget,
    *,
    client_turn_id: Optional[str],
    already_running_detail: str,
) -> None:
    state = get_state(request)
    async with state.chat_lock:
        existing = state.active_chats.get(target.state_key)
        if existing is not None and not existing.is_set():
            raise HTTPException(status_code=409, detail=already_running_detail)
        state.active_chats[target.state_key] = asyncio.Event()
    await begin_turn_state(request, target, client_turn_id)


async def prepare_turn(
    request: Request,
    payload: FileChatTurnRequest,
) -> tuple[Path, FileChatTarget, str, Optional[str]]:
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    repo_root = resolve_repo_root(request)
    target = parse_target(repo_root, str(payload.target or ""))
    if target.kind == "contextspace":
        target.path.parent.mkdir(parents=True, exist_ok=True)

    selection = resolve_file_chat_agent_selection(
        request,
        target,
        agent=payload.agent,
        profile=payload.profile,
    )
    await _claim_turn(
        request,
        target,
        client_turn_id=payload.client_turn_id,
        already_running_detail=payload.already_running_detail,
    )
    return repo_root, target, selection.agent_id, selection.profile


async def _execute_turn_lifecycle(
    request: Request,
    repo_root: Path,
    target: FileChatTarget,
    message: str,
    *,
    agent: str,
    profile: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    client_turn_id: Optional[str],
) -> Dict[str, Any]:
    try:

        async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
            await update_turn_state(
                request,
                target,
                agent=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

        try:
            result = await execute_file_chat_agent_turn(
                request,
                repo_root,
                target,
                message,
                agent=agent,
                profile=profile,
                model=model,
                reasoning=reasoning,
                on_meta=_on_meta,
            )
        except (
            RuntimeError,
            asyncio.CancelledError,
            OSError,
            FileChatError,
        ) as exc:
            result = {
                "status": "error",
                "detail": str(exc),
                "client_turn_id": client_turn_id or "",
            }
            await finalize_turn_state(request, target, result)
            raise
        result = dict(result or {})
        result["client_turn_id"] = client_turn_id or ""
        await finalize_turn_state(request, target, result)
        return result
    finally:
        await clear_interrupt_event(request, target.state_key)


async def run_turn(
    request: Request,
    payload: FileChatTurnRequest,
) -> Dict[str, Any]:
    repo_root, target, agent_id, profile = await prepare_turn(request, payload)
    return await _execute_turn_lifecycle(
        request,
        repo_root,
        target,
        payload.message.strip(),
        agent=agent_id,
        profile=profile,
        model=payload.model,
        reasoning=payload.reasoning,
        client_turn_id=payload.client_turn_id,
    )


def _stream_events_for_result(
    result: Dict[str, Any], *, client_turn_id: Optional[str]
) -> list[FileChatStreamItem]:
    status = result.get("status")
    if status == "ok":
        events: list[FileChatStreamItem] = []
        raw_events = result.pop("raw_events", None) or []
        for event in raw_events:
            events.append(FileChatStreamItem("app-server", event))
        usage_parts = result.pop("usage_parts", None) or []
        for usage in usage_parts:
            events.append(FileChatStreamItem("token_usage", usage))
        result["client_turn_id"] = client_turn_id or ""
        events.append(FileChatStreamItem("update", result))
        events.append(FileChatStreamItem("done", {"status": "ok"}))
        return events
    if status == "interrupted":
        return [
            FileChatStreamItem(
                "interrupted",
                {"detail": result.get("detail") or "File chat interrupted"},
            )
        ]
    return [
        FileChatStreamItem(
            "error",
            {"detail": result.get("detail") or "File chat failed"},
        )
    ]


async def stream_prepared_turn(
    request: Request,
    payload: FileChatTurnRequest,
    *,
    repo_root: Path,
    target: FileChatTarget,
    agent_id: str,
    profile: Optional[str],
) -> AsyncIterator[FileChatStreamItem]:
    yield FileChatStreamItem("status", {"status": "queued"})
    run_task = asyncio.create_task(
        _execute_turn_lifecycle(
            request,
            repo_root,
            target,
            payload.message.strip(),
            agent=agent_id,
            profile=profile,
            model=payload.model,
            reasoning=payload.reasoning,
            client_turn_id=payload.client_turn_id,
        )
    )
    try:
        result = await asyncio.shield(run_task)
        for event in _stream_events_for_result(
            result,
            client_turn_id=payload.client_turn_id,
        ):
            yield event
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("file chat stream failed")
        yield FileChatStreamItem("error", {"detail": "File chat failed"})


async def open_stream_turn(
    request: Request,
    payload: FileChatTurnRequest,
) -> AsyncIterator[FileChatStreamItem]:
    repo_root, target, agent_id, profile = await prepare_turn(request, payload)
    return stream_prepared_turn(
        request,
        payload,
        repo_root=repo_root,
        target=target,
        agent_id=agent_id,
        profile=profile,
    )


async def interrupt_turn(
    request: Request,
    target_raw: str,
    *,
    detail: str = "File chat interrupted",
) -> FileChatInterruptResult:
    resolved = resolve_target(request, target_raw)
    state = get_state(request)
    async with state.chat_lock:
        event = state.active_chats.get(resolved.state_key)
        if event is None:
            return FileChatInterruptResult(
                status="ok",
                detail="No active chat to interrupt",
            )
        event.set()
        return FileChatInterruptResult(status="interrupted", detail=detail)


async def pending_patch(request: Request, target: str) -> Dict[str, Any]:
    return await pending_draft_patch(request, target)


async def apply_patch(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await apply_draft_patch(request, payload)


async def discard_patch(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await discard_draft_patch(request, payload)


async def reset_ticket_thread(
    request: Request,
    index: int,
    *,
    agent: object = "codex",
    profile: object = None,
) -> FileChatThreadResetResult:
    target = resolve_ticket_target(request, index)
    selection = resolve_file_chat_agent_selection(
        request,
        target,
        agent=agent,
        profile=profile,
    )
    registry = getattr(request.app.state, "app_server_threads", None)
    cleared = False
    if registry is not None:
        try:
            cleared = bool(registry.reset_thread(selection.thread_key))
        except (
            AttributeError,
            KeyError,
            RuntimeError,
            OSError,
        ):
            logger.debug(
                "ticket chat thread reset failed for key=%s",
                selection.thread_key,
                exc_info=True,
            )
    return FileChatThreadResetResult(
        status="ok",
        index=int(index),
        target=target.target,
        chat_scope=target.chat_scope,
        key=selection.thread_key,
        cleared=cleared,
    )
