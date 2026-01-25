"""
Ticket chat routes: AI-powered ticket editing with streaming and patch preview.
"""

import asyncio
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agents.registry import validate_agent_id
from ..core.app_server_events import format_sse
from ..core.state import now_iso
from ..core.utils import atomic_write, find_repo_root
from .shared import SSE_HEADERS

TICKET_CHAT_STATE_NAME = "ticket_chat_state.json"
TICKET_CHAT_TIMEOUT_SECONDS = 180

# Template for ticket chat prompts
TICKET_CHAT_PROMPT_TEMPLATE = """You are editing a single ticket file for a task tracking system.

Ticket path: {ticket_path}

Instructions:
- This run is non-interactive. Do not ask the user questions.
- The current ticket content is provided below.
- Edit the ticket file directly based on the user's request.
- If no changes are needed, explain why without editing the file.
- Respond with a short summary of what you did.

User request:
{message}

<TICKET_CONTENT>
{ticket_content}
</TICKET_CONTENT>
"""


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
                # Stream raw events first so client can display agent activity
                raw_events = result.pop("raw_events", []) or []
                for event in raw_events:
                    yield format_sse("app-server", event)

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
        """Execute ticket chat using the doc_chat service's backends."""
        doc_chat = getattr(request.app.state, "doc_chat", None)
        if doc_chat is None:
            raise TicketChatError("Doc chat service not available")

        # Read current ticket content
        ticket_path = _get_ticket_path(repo_root, index)
        ticket_content = _read_ticket_content(repo_root, index)
        base_hash = _hash_content(ticket_content)

        # Determine relative path for display
        try:
            rel_path = str(ticket_path.relative_to(repo_root))
        except ValueError:
            rel_path = str(ticket_path)

        # Build prompt for ticket editing
        prompt = TICKET_CHAT_PROMPT_TEMPLATE.format(
            ticket_path=rel_path,
            message=message,
            ticket_content=ticket_content[:8000],  # Limit content size
        )

        try:
            # Check for interrupt before starting
            interrupt_event = await _get_or_create_interrupt_event(index)
            if interrupt_event.is_set():
                return {"status": "interrupted", "detail": "Ticket chat interrupted"}

            # Validate agent ID
            try:
                agent_id = validate_agent_id(agent or "")
            except ValueError:
                agent_id = "codex"

            # Backup original content before LLM runs
            backup_content = ticket_content

            # Execute using doc_chat's backend
            if agent_id == "opencode":
                result = await _execute_ticket_chat_opencode(
                    doc_chat,
                    prompt,
                    ticket_path,
                    backup_content,
                    interrupt_event,
                    model=model,
                    reasoning=reasoning,
                )
            else:
                result = await _execute_ticket_chat_app_server(
                    doc_chat,
                    prompt,
                    ticket_path,
                    backup_content,
                    interrupt_event,
                    model=model,
                    reasoning=reasoning,
                )

            if result.get("status") != "ok":
                return result

            # Check if file was modified by the LLM
            try:
                new_content = ticket_path.read_text(encoding="utf-8")
            except OSError:
                new_content = backup_content

            # Restore original content (we'll store changes as draft for user to review)
            if new_content != backup_content:
                atomic_write(ticket_path, backup_content)

            agent_message = result.get("agent_message", "Ticket updated")
            response_text = result.get("message", agent_message)

            # Create draft if content changed
            if new_content != backup_content:
                patch = _build_patch(rel_path, backup_content, new_content)
                state = _load_state(repo_root)
                key = _ticket_key(index)
                drafts = state.get("drafts", {})
                drafts[key] = {
                    "content": new_content,
                    "patch": patch,
                    "agent_message": agent_message,
                    "created_at": now_iso(),
                    "base_hash": base_hash,
                }
                state["drafts"] = drafts
                _save_state(repo_root, state)

                return {
                    "status": "ok",
                    "agent_message": agent_message,
                    "message": response_text,
                    "index": index,
                    "has_draft": True,
                    "patch": patch,
                    "content": new_content,
                    "base_hash": base_hash,
                    "created_at": drafts[key]["created_at"],
                }

            return {
                "status": "ok",
                "agent_message": agent_message,
                "message": response_text,
                "index": index,
                "has_draft": False,
            }

        except asyncio.CancelledError:
            return {"status": "interrupted", "detail": "Ticket chat cancelled"}
        except TicketChatError:
            raise
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    async def _execute_ticket_chat_app_server(
        doc_chat: Any,
        prompt: str,
        ticket_path: Path,
        backup_content: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute ticket chat using the app-server backend."""
        try:
            supervisor = doc_chat._ensure_app_server()
            client = await supervisor.get_client(doc_chat.engine.repo_root)

            # Start a new thread for ticket chat
            thread = await client.thread_start(str(doc_chat.engine.repo_root))
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise TicketChatError("App-server did not return a thread id")

            turn_kwargs: Dict[str, Any] = {}
            if model:
                turn_kwargs["model"] = model
            if reasoning:
                turn_kwargs["effort"] = reasoning

            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="on-request",
                sandbox_policy="dangerFullAccess",
                **turn_kwargs,
            )

            turn_task = asyncio.create_task(handle.wait(timeout=None))
            timeout_task = asyncio.create_task(
                asyncio.sleep(TICKET_CHAT_TIMEOUT_SECONDS)
            )
            interrupt_task = asyncio.create_task(interrupt_event.wait())

            try:
                tasks = {turn_task, timeout_task, interrupt_task}
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                if timeout_task in done:
                    turn_task.cancel()
                    return {"status": "error", "detail": "Ticket chat timed out"}

                if interrupt_task in done:
                    turn_task.cancel()
                    return {
                        "status": "interrupted",
                        "detail": "Ticket chat interrupted",
                    }

                turn_result = await turn_task
            finally:
                timeout_task.cancel()
                interrupt_task.cancel()

            if turn_result.errors:
                raise TicketChatError(turn_result.errors[-1])

            output = "\n".join(turn_result.agent_messages).strip()
            agent_message = _parse_agent_message(output)

            # Include raw events for streaming to client
            raw_events = getattr(turn_result, "raw_events", []) or []

            return {
                "status": "ok",
                "agent_message": agent_message,
                "message": output,
                "raw_events": raw_events,
            }

        except TicketChatError:
            raise
        except Exception as exc:
            raise TicketChatError(f"App-server error: {exc}") from exc

    async def _execute_ticket_chat_opencode(
        doc_chat: Any,
        prompt: str,
        ticket_path: Path,
        backup_content: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute ticket chat using the opencode backend."""
        from ..agents.opencode.runtime import (
            PERMISSION_ALLOW,
            collect_opencode_output,
            extract_session_id,
            parse_message_response,
            split_model_id,
        )

        try:
            supervisor = doc_chat._ensure_opencode()
            client = await supervisor.get_client(doc_chat.engine.repo_root)

            # Create a new session for ticket chat
            session = await client.create_session(
                directory=str(doc_chat.engine.repo_root)
            )
            thread_id = extract_session_id(session, allow_fallback_id=True)
            if not isinstance(thread_id, str) or not thread_id:
                raise TicketChatError("OpenCode did not return a session id")

            model_payload = split_model_id(model)
            await supervisor.mark_turn_started(doc_chat.engine.repo_root)

            ready_event = asyncio.Event()
            output_task = asyncio.create_task(
                collect_opencode_output(
                    client,
                    session_id=thread_id,
                    workspace_path=str(doc_chat.engine.repo_root),
                    model_payload=model_payload,
                    permission_policy=PERMISSION_ALLOW,
                    question_policy="auto_first_option",
                    should_stop=interrupt_event.is_set,
                    ready_event=ready_event,
                    stall_timeout_seconds=doc_chat.engine.config.opencode.session_stall_timeout_seconds,
                )
            )

            # Wait briefly for output collection to be ready
            try:
                await asyncio.wait_for(ready_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            prompt_task = asyncio.create_task(
                client.prompt_async(
                    thread_id,
                    message=prompt,
                    model=model_payload,
                    variant=reasoning,
                )
            )
            timeout_task = asyncio.create_task(
                asyncio.sleep(TICKET_CHAT_TIMEOUT_SECONDS)
            )
            interrupt_task = asyncio.create_task(interrupt_event.wait())

            try:
                prompt_response = None
                try:
                    prompt_response = await prompt_task
                except Exception as exc:
                    interrupt_event.set()
                    output_task.cancel()
                    raise TicketChatError(f"OpenCode prompt failed: {exc}") from exc

                tasks = {output_task, timeout_task, interrupt_task}
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                if timeout_task in done:
                    output_task.cancel()
                    return {"status": "error", "detail": "Ticket chat timed out"}

                if interrupt_task in done:
                    output_task.cancel()
                    return {
                        "status": "interrupted",
                        "detail": "Ticket chat interrupted",
                    }

                output_result = await output_task
                if output_result.text or output_result.error:
                    pass
                elif prompt_response is not None:
                    fallback = parse_message_response(prompt_response)
                    if fallback.text:
                        output_result = type(output_result)(
                            text=fallback.text, error=fallback.error
                        )
            finally:
                timeout_task.cancel()
                interrupt_task.cancel()
                await supervisor.mark_turn_finished(doc_chat.engine.repo_root)

            if output_result.error:
                raise TicketChatError(output_result.error)

            agent_message = _parse_agent_message(output_result.text)

            return {
                "status": "ok",
                "agent_message": agent_message,
                "message": output_result.text,
            }

        except TicketChatError:
            raise
        except Exception as exc:
            raise TicketChatError(f"OpenCode error: {exc}") from exc

    def _parse_agent_message(output: str) -> str:
        """Extract agent message from output."""
        text = (output or "").strip()
        if not text:
            return "Ticket updated via chat."
        for line in text.splitlines():
            if line.lower().startswith("agent:"):
                return line[len("agent:") :].strip() or "Ticket updated via chat."
        # Return first line as summary
        first_line = text.splitlines()[0].strip()
        if len(first_line) > 100:
            return first_line[:97] + "..."
        return first_line

    def _build_patch(rel_path: str, before: str, after: str) -> str:
        """Build a unified diff patch."""
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
        return "\n".join(diff)

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
