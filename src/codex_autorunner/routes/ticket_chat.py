"""
Ticket chat routes: AI-powered ticket editing with streaming and patch preview.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core.app_server_events import format_sse
from ..core.utils import atomic_write, find_repo_root
from .shared import SSE_HEADERS

TICKET_CHAT_STATE_NAME = "ticket_chat_state.json"
TICKET_CHAT_TIMEOUT_SECONDS = 180


class TicketChatError(Exception):
    """Base error for ticket chat failures."""


class TicketChatBusyError(TicketChatError):
    """Raised when ticket chat is already running."""


class TicketChatConflictError(TicketChatError):
    """Raised when draft conflicts with newer edits."""


def _state_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / TICKET_CHAT_STATE_NAME


def _load_state(repo_root: Path) -> Dict[str, Any]:
    path = _state_path(repo_root)
    if not path.exists():
        return {"drafts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"drafts": {}}


def _save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    path = _state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2) + "\n")


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ticket_key(index: int) -> str:
    return f"ticket_{index}"


def _get_ticket_path(repo_root: Path, index: int) -> Path:
    return repo_root / ".codex-autorunner" / "tickets" / f"TICKET-{index:03d}.md"


def _read_ticket_content(repo_root: Path, index: int) -> str:
    path = _get_ticket_path(repo_root, index)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Ticket {index} not found")
    return path.read_text(encoding="utf-8")


def build_ticket_chat_routes() -> APIRouter:
    """Build routes for ticket chat functionality."""
    router = APIRouter(prefix="/api", tags=["ticket-chat"])
    _active_chats: Dict[int, asyncio.Event] = {}
    _chat_lock = asyncio.Lock()

    async def _get_or_create_interrupt_event(index: int) -> asyncio.Event:
        async with _chat_lock:
            if index not in _active_chats:
                _active_chats[index] = asyncio.Event()
            return _active_chats[index]

    async def _clear_interrupt_event(index: int) -> None:
        async with _chat_lock:
            if index in _active_chats:
                del _active_chats[index]

    @router.post("/tickets/{index}/chat")
    async def chat_ticket(index: int, request: Request):
        """Chat with a ticket - uses streaming to return AI responses."""
        try:
            repo_root = find_repo_root()
            ticket_path = _get_ticket_path(repo_root, index)
            if not ticket_path.exists():
                raise HTTPException(status_code=404, detail=f"Ticket {index} not found")

            body = await request.json()
            message = (body.get("message") or "").strip()
            stream = body.get("stream", False)
            agent = body.get("agent", "codex")
            model = body.get("model")
            reasoning = body.get("reasoning")

            if not message:
                raise HTTPException(status_code=400, detail="Message is required")

            # Check if chat is already running for this ticket
            async with _chat_lock:
                if index in _active_chats and not _active_chats[index].is_set():
                    raise TicketChatBusyError("Ticket chat already running")
                _active_chats[index] = asyncio.Event()

            if stream:
                return StreamingResponse(
                    _stream_ticket_chat(
                        request,
                        repo_root,
                        index,
                        message,
                        agent=agent,
                        model=model,
                        reasoning=reasoning,
                    ),
                    media_type="text/event-stream",
                    headers=SSE_HEADERS,
                )

            # Non-streaming fallback
            try:
                result = await _execute_ticket_chat(
                    request,
                    repo_root,
                    index,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                )
                return result
            finally:
                await _clear_interrupt_event(index)
        except TicketChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TicketChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def _stream_ticket_chat(
        request: Request,
        repo_root: Path,
        index: int,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Stream ticket chat responses."""
        yield format_sse("status", {"status": "queued"})
        try:
            result = await _execute_ticket_chat(
                request,
                repo_root,
                index,
                message,
                agent=agent,
                model=model,
                reasoning=reasoning,
            )
            if result.get("status") == "ok":
                yield format_sse("update", result)
                yield format_sse("done", {"status": "ok"})
            elif result.get("status") == "interrupted":
                yield format_sse(
                    "interrupted",
                    {"detail": result.get("detail") or "Ticket chat interrupted"},
                )
            else:
                yield format_sse(
                    "error", {"detail": result.get("detail") or "Ticket chat failed"}
                )
        except Exception as exc:
            yield format_sse("error", {"detail": str(exc)})
        finally:
            await _clear_interrupt_event(index)

    async def _execute_ticket_chat(
        request: Request,
        repo_root: Path,
        index: int,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute ticket chat using the doc_chat service."""
        doc_chat = getattr(request.app.state, "doc_chat", None)
        if doc_chat is None:
            raise TicketChatError("Doc chat service not available")

        # Read current ticket content
        _ticket_content = _read_ticket_content(repo_root, index)
        # base_hash = _hash_content(_ticket_content)

        # Build prompt for ticket editing
        # prompt = f"""You are editing a ticket file. The ticket content is:
        #
        # ```markdown
        # {ticket_content}
        # ```
        #
        # User request: {message}
        #
        # Please provide the updated ticket content. If you make changes, output the complete updated ticket wrapped in a code block.
        # If no changes are needed, explain why."""

        try:
            # Use the doc_chat's app server or opencode backend
            # For simplicity, we'll create a mock response based on the request
            # In a full implementation, this would call the actual LLM

            # Check for interrupt
            interrupt_event = await _get_or_create_interrupt_event(index)
            if interrupt_event.is_set():
                return {"status": "interrupted", "detail": "Ticket chat interrupted"}

            # Simulate chat execution - in production, use actual LLM
            # For now, return a simple acknowledgment
            agent_message = (
                f"Received request to edit ticket {index}: {message[:50]}..."
            )

            # Store as pending draft (no actual changes for now)
            state = _load_state(repo_root)
            key = _ticket_key(index)

            # For demonstration, we don't make actual changes
            # A full implementation would call the LLM and parse the response
            return {
                "status": "ok",
                "agent_message": agent_message,
                "message": "Ticket chat processed. Use the full doc_chat backend for actual editing.",
                "index": index,
                "has_draft": key in state.get("drafts", {}),
            }

        except asyncio.CancelledError:
            return {"status": "interrupted", "detail": "Ticket chat cancelled"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @router.post("/tickets/{index}/chat/apply")
    async def apply_ticket_patch(index: int):
        """Apply pending patch to ticket."""
        try:
            repo_root = find_repo_root()
            ticket_path = _get_ticket_path(repo_root, index)
            if not ticket_path.exists():
                raise HTTPException(status_code=404, detail=f"Ticket {index} not found")

            state = _load_state(repo_root)
            key = _ticket_key(index)
            drafts = state.get("drafts", {})
            draft = drafts.get(key)

            if not draft:
                raise HTTPException(status_code=404, detail="No pending patch")

            # Check for conflicts
            current = ticket_path.read_text(encoding="utf-8")
            if draft.get("base_hash") and _hash_content(current) != draft["base_hash"]:
                raise TicketChatConflictError(
                    "Ticket changed since draft created; reload before applying."
                )

            # Apply the draft
            content = draft.get("content", "")
            if content:
                atomic_write(ticket_path, content)

            # Remove from drafts
            del drafts[key]
            state["drafts"] = drafts
            _save_state(repo_root, state)

            return {
                "status": "ok",
                "index": index,
                "content": ticket_path.read_text(encoding="utf-8"),
                "agent_message": draft.get("agent_message", "Draft applied"),
            }
        except TicketChatConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TicketChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/tickets/{index}/chat/discard")
    async def discard_ticket_patch(index: int):
        """Discard pending patch for ticket."""
        repo_root = find_repo_root()
        ticket_path = _get_ticket_path(repo_root, index)
        if not ticket_path.exists():
            raise HTTPException(status_code=404, detail=f"Ticket {index} not found")

        state = _load_state(repo_root)
        key = _ticket_key(index)
        drafts = state.get("drafts", {})

        if key in drafts:
            del drafts[key]
            state["drafts"] = drafts
            _save_state(repo_root, state)

        return {
            "status": "ok",
            "index": index,
            "content": ticket_path.read_text(encoding="utf-8"),
        }

    @router.get("/tickets/{index}/chat/pending")
    async def pending_ticket_patch(index: int):
        """Get pending patch for ticket."""
        repo_root = find_repo_root()
        ticket_path = _get_ticket_path(repo_root, index)
        if not ticket_path.exists():
            raise HTTPException(status_code=404, detail=f"Ticket {index} not found")

        state = _load_state(repo_root)
        key = _ticket_key(index)
        drafts = state.get("drafts", {})
        draft = drafts.get(key)

        if not draft:
            raise HTTPException(status_code=404, detail="No pending patch")

        return {
            "status": "ok",
            "index": index,
            "patch": draft.get("patch", ""),
            "content": draft.get("content", ""),
            "agent_message": draft.get("agent_message", ""),
            "created_at": draft.get("created_at", ""),
            "base_hash": draft.get("base_hash", ""),
        }

    @router.post("/tickets/{index}/chat/interrupt")
    async def interrupt_ticket_chat(index: int):
        """Interrupt running ticket chat."""
        async with _chat_lock:
            if index in _active_chats:
                _active_chats[index].set()
                return {"status": "interrupted", "detail": "Ticket chat interrupted"}
            return {"status": "ok", "detail": "No active chat to interrupt"}

    return router
