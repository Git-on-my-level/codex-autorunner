from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import HTTPException, Request

from ....agents.runtime_options import AgentRuntimeOptionsError
from ....core.orchestration import (
    TurnExecutionContractError,
    TurnExecutionRecord,
    TurnExecutionRequest,
)
from ....core.orchestration.turn_execution_contract import TurnExecutionStatus
from ....core.time_utils import now_iso
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
    FileChatAgentSelection,
    build_file_chat_turn_request,
    resolve_file_chat_agent_selection,
)
from ..routes.file_chat_routes.execution import (
    execute_file_chat as execute_file_chat_agent_turn,
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


@dataclass(frozen=True)
class FileChatPreparedTurn:
    repo_root: Path
    target: FileChatTarget
    turn_request: TurnExecutionRequest
    turn_record: TurnExecutionRecord
    selection: FileChatAgentSelection


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
    turn_request: TurnExecutionRequest,
    turn_record: TurnExecutionRecord,
    already_running_detail: str,
) -> None:
    state = get_state(request)
    async with state.chat_lock:
        existing = state.active_chats.get(target.state_key)
        if existing is not None and not existing.is_set():
            raise HTTPException(status_code=409, detail=already_running_detail)
        state.active_chats[target.state_key] = asyncio.Event()
    await begin_turn_state(
        request,
        target,
        client_turn_id,
        turn_request=turn_request,
        turn_record=turn_record,
    )


async def prepare_turn(
    request: Request,
    payload: FileChatTurnRequest,
) -> FileChatPreparedTurn:
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    repo_root = resolve_repo_root(request)
    target = parse_target(repo_root, str(payload.target or ""))
    if target.kind == "contextspace":
        target.path.parent.mkdir(parents=True, exist_ok=True)

    try:
        canonical = build_file_chat_turn_request(
            request,
            repo_root,
            target,
            message,
            agent=payload.agent,
            profile=payload.profile,
            model=payload.model,
            reasoning=payload.reasoning,
            client_turn_id=payload.client_turn_id,
        )
    except (AgentRuntimeOptionsError, TurnExecutionContractError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    turn_record = TurnExecutionRecord(
        request=canonical.request,
        execution_id=canonical.request.request_id,
        status="claiming",
        queued_at=now_iso(),
        metadata={"surface": "file_chat"},
    )
    await _claim_turn(
        request,
        target,
        client_turn_id=payload.client_turn_id,
        turn_request=canonical.request,
        turn_record=turn_record,
        already_running_detail=payload.already_running_detail,
    )
    return FileChatPreparedTurn(
        repo_root=repo_root,
        target=target,
        turn_request=canonical.request,
        turn_record=turn_record,
        selection=canonical.selection,
    )


def _terminal_record_for_result(
    prepared: FileChatPreparedTurn,
    result: Dict[str, Any],
) -> TurnExecutionRecord:
    status = str(result.get("status") or "").strip().lower()
    record_status: TurnExecutionStatus
    if status == "ok":
        record_status = "completed"
    elif status == "interrupted":
        record_status = "interrupted"
    else:
        record_status = "failed"
    final_request = prepared.turn_request
    raw_turn_request = result.get("turn_request")
    if isinstance(raw_turn_request, dict):
        try:
            final_request = TurnExecutionRequest.from_mapping(raw_turn_request)
        except TurnExecutionContractError:
            final_request = prepared.turn_request
    return TurnExecutionRecord(
        request=final_request,
        execution_id=prepared.turn_record.execution_id,
        status=record_status,
        queued_at=prepared.turn_record.queued_at,
        claimed_at=prepared.turn_record.claimed_at,
        started_at=prepared.turn_record.started_at,
        terminal_at=now_iso(),
        backend_conversation_id=result.get("thread_id"),
        backend_turn_id=result.get("turn_id"),
        assistant_text=result.get("message") or result.get("agent_message"),
        error_text=result.get("detail") if record_status == "failed" else None,
        metadata={"surface": "file_chat"},
    )


async def _execute_turn_lifecycle(
    request: Request,
    prepared: FileChatPreparedTurn,
    message: str,
    *,
    client_turn_id: Optional[str],
) -> Dict[str, Any]:
    repo_root = prepared.repo_root
    target = prepared.target
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
                agent=prepared.turn_request.agent,
                profile=prepared.turn_request.profile,
                model=prepared.turn_request.model,
                reasoning=prepared.turn_request.reasoning,
                turn_request=prepared.turn_request,
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
                "execution_id": prepared.turn_record.execution_id,
                "turn_request": prepared.turn_request.to_dict(),
            }
            result["turn_record"] = _terminal_record_for_result(
                prepared,
                result,
            ).to_dict()
            await finalize_turn_state(request, target, result)
            raise
        result = dict(result or {})
        result["client_turn_id"] = client_turn_id or ""
        result["execution_id"] = prepared.turn_record.execution_id
        if "turn_request" not in result:
            result["turn_request"] = prepared.turn_request.to_dict()
        result["turn_record"] = _terminal_record_for_result(
            prepared,
            result,
        ).to_dict()
        await finalize_turn_state(request, target, result)
        return result
    finally:
        await clear_interrupt_event(request, target.state_key)


async def run_turn(
    request: Request,
    payload: FileChatTurnRequest,
) -> Dict[str, Any]:
    prepared = await prepare_turn(request, payload)
    return await _execute_turn_lifecycle(
        request,
        prepared,
        payload.message.strip(),
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
    prepared: FileChatPreparedTurn,
) -> AsyncIterator[FileChatStreamItem]:
    yield FileChatStreamItem("status", {"status": "queued"})
    run_task = asyncio.create_task(
        _execute_turn_lifecycle(
            request,
            prepared,
            payload.message.strip(),
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
    prepared = await prepare_turn(request, payload)
    return stream_prepared_turn(
        request,
        payload,
        prepared=prepared,
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
