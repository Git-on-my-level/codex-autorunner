"""
Unified file chat routes: AI-powered editing for tickets and workspace docs.

Targets:
- ticket:{index} -> .codex-autorunner/tickets/TICKET-###.md
- workspace:{path} -> .codex-autorunner/workspace/{path}
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.registry import validate_agent_id
from ....core import drafts as draft_utils
from ....core.context_awareness import CAR_AWARENESS_BLOCK, format_file_role_addendum
from ....core.state import now_iso
from ....core.utils import atomic_write, find_repo_root
from ....integrations.app_server.event_buffer import format_sse
from ....workspace.paths import (
    WORKSPACE_DOC_KINDS,
    normalize_workspace_rel_path,
    workspace_doc_path,
)
from .shared import SSE_HEADERS

FILE_CHAT_STATE_NAME = draft_utils.FILE_CHAT_STATE_NAME
FILE_CHAT_TIMEOUT_SECONDS = 180
logger = logging.getLogger(__name__)


class FileChatError(Exception):
    """Base error for file chat failures."""


@dataclass(frozen=True)
class _Target:
    target: str
    kind: str  # "ticket" | "workspace"
    id: str  # "001" | "spec"
    path: Path
    rel_path: str
    state_key: str


@dataclass
class FileChatRoutesState:
    active_chats: Dict[str, asyncio.Event]
    chat_lock: asyncio.Lock
    turn_lock: asyncio.Lock
    current_by_target: Dict[str, Dict[str, Any]]
    current_by_client: Dict[str, Dict[str, Any]]
    last_by_client: Dict[str, Dict[str, Any]]

    def __init__(self) -> None:
        self.active_chats = {}
        self.chat_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.current_by_target = {}
        self.current_by_client = {}
        self.last_by_client = {}


def _state_path(repo_root: Path) -> Path:
    return draft_utils.state_path(repo_root)


def _load_state(repo_root: Path) -> Dict[str, Any]:
    return draft_utils.load_state(repo_root)


def _save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    draft_utils.save_state(repo_root, state)


def _hash_content(content: str) -> str:
    return draft_utils.hash_content(content)


def _resolve_repo_root(request: Optional[Request] = None) -> Path:
    if request is not None:
        engine = getattr(request.app.state, "engine", None)
        repo_root = getattr(engine, "repo_root", None)
        if isinstance(repo_root, Path):
            return repo_root
        if isinstance(repo_root, str):
            try:
                return Path(repo_root)
            except Exception:
                pass
    return find_repo_root()


def _ticket_path(repo_root: Path, index: int) -> Path:
    return repo_root / ".codex-autorunner" / "tickets" / f"TICKET-{index:03d}.md"


def _parse_target(repo_root: Path, raw: str) -> _Target:
    target = (raw or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")

    if target.lower().startswith("ticket:"):
        suffix = target.split(":", 1)[1].strip()
        if not suffix.isdigit():
            raise HTTPException(status_code=400, detail="invalid ticket target")
        idx = int(suffix)
        if idx <= 0:
            raise HTTPException(status_code=400, detail="invalid ticket target")
        path = _ticket_path(repo_root, idx)
        rel = (
            str(path.relative_to(repo_root))
            if path.is_relative_to(repo_root)
            else str(path)
        )
        return _Target(
            target=f"ticket:{idx}",
            kind="ticket",
            id=f"{idx:03d}",
            path=path,
            rel_path=rel,
            state_key=f"ticket_{idx:03d}",
        )

    if target.lower().startswith("workspace:"):
        suffix_raw = target.split(":", 1)[1].strip()
        if not suffix_raw:
            raise HTTPException(status_code=400, detail="invalid workspace target")

        # Allow legacy kind-only targets (active_context/decisions/spec)
        if suffix_raw.lower() in WORKSPACE_DOC_KINDS:
            path = workspace_doc_path(repo_root, suffix_raw)
            rel_suffix = f"{suffix_raw}.md"
        else:
            try:
                path, rel_suffix = normalize_workspace_rel_path(repo_root, suffix_raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        rel = (
            str(path.relative_to(repo_root))
            if path.is_relative_to(repo_root)
            else str(path)
        )
        return _Target(
            target=f"workspace:{rel_suffix}",
            kind="workspace",
            id=rel_suffix,
            path=path,
            rel_path=rel,
            state_key=f"workspace_{rel_suffix.replace('/', '_')}",
        )

    raise HTTPException(status_code=400, detail=f"invalid target: {target}")


def _build_file_chat_prompt(*, target: _Target, message: str, before: str) -> str:
    if target.kind == "ticket":
        file_role_context = (
            f"{format_file_role_addendum('ticket', target.rel_path)}\n"
            "Edits here change what the ticket flow agent will do; keep YAML "
            "frontmatter valid."
        )
    elif target.kind == "workspace":
        file_role_context = (
            f"{format_file_role_addendum('workspace', target.rel_path)}\n"
            "These docs act as shared memory across ticket turns."
        )
    else:
        file_role_context = format_file_role_addendum("other", target.rel_path)

    return (
        f"{CAR_AWARENESS_BLOCK}\n\n"
        "<file_role_context>\n"
        f"{file_role_context}\n"
        "</file_role_context>\n\n"
        "You are editing a single file in Codex Autorunner.\n\n"
        "<target>\n"
        f"{target.target}\n"
        "</target>\n\n"
        "<path>\n"
        f"{target.rel_path}\n"
        "</path>\n\n"
        "<instructions>\n"
        "- This is a single-turn edit request. Don’t ask the user questions.\n"
        "- You may read other files for context, but only modify the target file.\n"
        "- If no changes are needed, explain why without editing the file.\n"
        "- Respond with a short summary of what you did.\n"
        "</instructions>\n\n"
        "<user_request>\n"
        f"{message}\n"
        "</user_request>\n\n"
        "<FILE_CONTENT>\n"
        f"{before[:12000]}\n"
        "</FILE_CONTENT>\n"
    )


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _build_patch(rel_path: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    )
    return "\n".join(diff)


def build_file_chat_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["file-chat"])
    state = FileChatRoutesState()

    def _get_state(request: Request) -> FileChatRoutesState:
        if not hasattr(request.app.state, "file_chat_routes_state"):
            request.app.state.file_chat_routes_state = state
        return request.app.state.file_chat_routes_state

    async def _get_or_create_interrupt_event(
        request: Request, key: str
    ) -> asyncio.Event:
        s = _get_state(request)
        async with s.chat_lock:
            if key not in s.active_chats:
                s.active_chats[key] = asyncio.Event()
            return s.active_chats[key]

    async def _clear_interrupt_event(request: Request, key: str) -> None:
        s = _get_state(request)
        async with s.chat_lock:
            s.active_chats.pop(key, None)

    async def _begin_turn_state(
        request: Request, target: _Target, client_turn_id: Optional[str]
    ) -> None:
        s = _get_state(request)
        async with s.turn_lock:
            state: Dict[str, Any] = {
                "client_turn_id": client_turn_id or "",
                "target": target.target,
                "status": "starting",
                "agent": None,
                "thread_id": None,
                "turn_id": None,
            }
            s.current_by_target[target.state_key] = state
            if client_turn_id:
                s.current_by_client[client_turn_id] = state

    async def _update_turn_state(
        request: Request, target: _Target, **updates: Any
    ) -> None:
        s = _get_state(request)
        async with s.turn_lock:
            state = s.current_by_target.get(target.state_key)
            if not state:
                return
            for key, value in updates.items():
                if value is None:
                    continue
                state[key] = value
            cid = state.get("client_turn_id") or ""
            if cid:
                s.current_by_client[cid] = state

    async def _finalize_turn_state(
        request: Request, target: _Target, result: Dict[str, Any]
    ) -> None:
        s = _get_state(request)
        async with s.turn_lock:
            state = s.current_by_target.pop(target.state_key, None)
            cid = ""
            if state:
                cid = state.get("client_turn_id", "") or ""
            if cid:
                s.current_by_client.pop(cid, None)
                s.last_by_client[cid] = dict(result or {})

    async def _active_for_client(
        request: Request, client_turn_id: Optional[str]
    ) -> Dict[str, Any]:
        if not client_turn_id:
            return {}
        s = _get_state(request)
        async with s.turn_lock:
            return dict(s.current_by_client.get(client_turn_id, {}))

    async def _last_for_client(
        request: Request, client_turn_id: Optional[str]
    ) -> Dict[str, Any]:
        if not client_turn_id:
            return {}
        s = _get_state(request)
        async with s.turn_lock:
            return dict(s.last_by_client.get(client_turn_id, {}))

    @router.get("/file-chat/active")
    async def file_chat_active(
        request: Request, client_turn_id: Optional[str] = None
    ) -> Dict[str, Any]:
        current = await _active_for_client(request, client_turn_id)
        last = await _last_for_client(request, client_turn_id)
        return {"active": bool(current), "current": current, "last_result": last}

    @router.post("/file-chat")
    async def chat_file(request: Request):
        """Chat with a file target - optionally streams SSE events."""
        body = await request.json()
        target_raw = body.get("target")
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, str(target_raw or ""))

        # Ensure target directory exists for workspace docs (write on demand)
        if target.kind == "workspace":
            target.path.parent.mkdir(parents=True, exist_ok=True)

        # Concurrency guard per target
        s = _get_state(request)
        async with s.chat_lock:
            existing = s.active_chats.get(target.state_key)
            if existing is not None and not existing.is_set():
                raise HTTPException(status_code=409, detail="File chat already running")
            s.active_chats[target.state_key] = asyncio.Event()

        await _begin_turn_state(request, target, client_turn_id)

        if stream:
            return StreamingResponse(
                _stream_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result: Dict[str, Any]

            async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
                await _update_turn_state(
                    request,
                    target,
                    agent=agent_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            try:
                result = await _execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            except Exception as exc:
                await _finalize_turn_state(
                    request,
                    target,
                    {
                        "status": "error",
                        "detail": str(exc),
                        "client_turn_id": client_turn_id or "",
                    },
                )
                raise
            result = dict(result or {})
            result["client_turn_id"] = client_turn_id or ""
            await _finalize_turn_state(request, target, result)
            return result
        finally:
            await _clear_interrupt_event(request, target.state_key)

    async def _stream_file_chat(
        request: Request,
        repo_root: Path,
        target: _Target,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        yield format_sse("status", {"status": "queued"})
        try:

            async def _on_meta(agent_id: str, thread_id: str, turn_id: str) -> None:
                await _update_turn_state(
                    request,
                    target,
                    agent=agent_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            run_task = asyncio.create_task(
                _execute_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    on_meta=_on_meta,
                )
            )

            async def _finalize() -> None:
                result = {"status": "error", "detail": "File chat failed"}
                try:
                    result = await run_task
                except Exception as exc:
                    logger.exception("file chat task failed")
                    result = {
                        "status": "error",
                        "detail": str(exc) or "File chat failed",
                    }
                result = dict(result or {})
                result["client_turn_id"] = client_turn_id or ""
                await _finalize_turn_state(request, target, result)

            asyncio.create_task(_finalize())

            try:
                result = await asyncio.shield(run_task)
            except asyncio.CancelledError:
                # client disconnected; turn continues in background
                return

            if result.get("status") == "ok":
                raw_events = result.pop("raw_events", []) or []
                for event in raw_events:
                    yield format_sse("app-server", event)
                usage_parts = result.pop("usage_parts", []) or []
                for usage in usage_parts:
                    yield format_sse("token_usage", usage)
                result["client_turn_id"] = client_turn_id or ""
                yield format_sse("update", result)
                yield format_sse("done", {"status": "ok"})
            elif result.get("status") == "interrupted":
                yield format_sse(
                    "interrupted",
                    {"detail": result.get("detail") or "File chat interrupted"},
                )
            else:
                yield format_sse(
                    "error", {"detail": result.get("detail") or "File chat failed"}
                )
        except Exception:
            logger.exception("file chat stream failed")
            yield format_sse("error", {"detail": "File chat failed"})
        finally:
            await _clear_interrupt_event(request, target.state_key)

    async def _execute_file_chat(
        request: Request,
        repo_root: Path,
        target: _Target,
        message: str,
        *,
        agent: str = "codex",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        on_meta: Optional[Callable[[str, str, str], Any]] = None,
        on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        supervisor = getattr(request.app.state, "app_server_supervisor", None)
        threads = getattr(request.app.state, "app_server_threads", None)
        opencode = getattr(request.app.state, "opencode_supervisor", None)
        engine = getattr(request.app.state, "engine", None)
        events = getattr(request.app.state, "app_server_events", None)
        stall_timeout_seconds = None
        try:
            stall_timeout_seconds = (
                engine.config.opencode.session_stall_timeout_seconds
                if engine is not None
                else None
            )
        except Exception:
            stall_timeout_seconds = None
        if supervisor is None and opencode is None:
            raise FileChatError("No agent supervisor available for file chat")

        before = _read_file(target.path)
        base_hash = _hash_content(before)

        prompt = _build_file_chat_prompt(target=target, message=message, before=before)

        interrupt_event = await _get_or_create_interrupt_event(
            request, target.state_key
        )
        if interrupt_event.is_set():
            return {"status": "interrupted", "detail": "File chat interrupted"}

        try:
            agent_id = validate_agent_id(agent or "")
        except ValueError:
            agent_id = "codex"

        thread_key = f"file_chat.{target.state_key}"
        await _update_turn_state(request, target, status="running", agent=agent_id)

        if agent_id == "opencode":
            if opencode is None:
                return {"status": "error", "detail": "OpenCode supervisor unavailable"}
            result = await _execute_opencode(
                opencode,
                repo_root,
                prompt,
                interrupt_event,
                model=model,
                reasoning=reasoning,
                thread_registry=threads,
                thread_key=thread_key,
                stall_timeout_seconds=stall_timeout_seconds,
                on_meta=on_meta,
                on_usage=on_usage,
            )
        else:
            if supervisor is None:
                return {
                    "status": "error",
                    "detail": "App-server supervisor unavailable",
                }
            result = await _execute_app_server(
                supervisor,
                repo_root,
                prompt,
                interrupt_event,
                agent_id=agent_id,
                model=model,
                reasoning=reasoning,
                thread_registry=threads,
                thread_key=thread_key,
                on_meta=on_meta,
                events=events,
            )

        if result.get("status") != "ok":
            return result

        after = _read_file(target.path)

        # Restore original content; store draft for apply/discard
        if after != before:
            atomic_write(target.path, before)

        agent_message = result.get("agent_message", "File updated")
        response_text = result.get("message", agent_message)

        if after != before:
            patch = _build_patch(target.rel_path, before, after)
            state = _load_state(repo_root)
            drafts = (
                state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
            )
            drafts[target.state_key] = {
                "content": after,
                "patch": patch,
                "agent_message": agent_message,
                "created_at": now_iso(),
                "base_hash": base_hash,
                "target": target.target,
                "rel_path": target.rel_path,
            }
            state["drafts"] = drafts
            _save_state(repo_root, state)
            return {
                "status": "ok",
                "target": target.target,
                "agent": agent_id,
                "agent_message": agent_message,
                "message": response_text,
                "has_draft": True,
                "patch": patch,
                "content": after,
                "base_hash": base_hash,
                "created_at": drafts[target.state_key]["created_at"],
                "thread_id": result.get("thread_id"),
                "turn_id": result.get("turn_id"),
                **(
                    {"raw_events": result.get("raw_events")}
                    if result.get("raw_events")
                    else {}
                ),
            }

        return {
            "status": "ok",
            "target": target.target,
            "agent": agent_id,
            "agent_message": agent_message,
            "message": response_text,
            "has_draft": False,
            "thread_id": result.get("thread_id"),
            "turn_id": result.get("turn_id"),
            **(
                {"raw_events": result.get("raw_events")}
                if result.get("raw_events")
                else {}
            ),
        }

    async def _execute_app_server(
        supervisor: Any,
        repo_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        agent_id: str = "codex",
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        on_meta: Optional[Callable[[str, str, str], Any]] = None,
        events: Optional[Any] = None,
    ) -> Dict[str, Any]:
        client = await supervisor.get_client(repo_root)

        thread_id = None
        if thread_registry is not None and thread_key:
            thread_id = thread_registry.get_thread_id(thread_key)
        if thread_id:
            try:
                await client.thread_resume(thread_id)
            except Exception:
                thread_id = None

        if not thread_id:
            thread = await client.thread_start(str(repo_root))
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise FileChatError("App-server did not return a thread id")
            if thread_registry is not None and thread_key:
                thread_registry.set_thread_id(thread_key, thread_id)

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
        if events is not None:
            try:
                await events.register_turn(thread_id, handle.turn_id)
            except Exception:
                logger.debug("file chat register_turn failed", exc_info=True)
        if on_meta is not None:
            try:
                maybe = on_meta(agent_id, thread_id, handle.turn_id)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.debug("file chat meta callback failed", exc_info=True)

        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(FILE_CHAT_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                {turn_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                turn_task.cancel()
                return {"status": "error", "detail": "File chat timed out"}
            if interrupt_task in done:
                turn_task.cancel()
                return {"status": "interrupted", "detail": "File chat interrupted"}
            turn_result = await turn_task
        finally:
            timeout_task.cancel()
            interrupt_task.cancel()

        if getattr(turn_result, "errors", None):
            errors = turn_result.errors
            raise FileChatError(errors[-1] if errors else "App-server error")

        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
        agent_message = _parse_agent_message(output)
        raw_events = getattr(turn_result, "raw_events", []) or []
        return {
            "status": "ok",
            "agent_message": agent_message,
            "message": output,
            "raw_events": raw_events,
            "thread_id": thread_id,
            "turn_id": getattr(handle, "turn_id", None),
            "agent": agent_id,
        }

    async def _execute_opencode(
        supervisor: Any,
        repo_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        stall_timeout_seconds: Optional[float] = None,
        on_meta: Optional[Callable[[str, str, str], Any]] = None,
        on_usage: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        from ....agents.opencode.runtime import (
            PERMISSION_ALLOW,
            build_turn_id,
            collect_opencode_output,
            extract_session_id,
            parse_message_response,
            split_model_id,
        )

        client = await supervisor.get_client(repo_root)
        session_id = None
        if thread_registry is not None and thread_key:
            session_id = thread_registry.get_thread_id(thread_key)
        if not session_id:
            session = await client.create_session(directory=str(repo_root))
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not isinstance(session_id, str) or not session_id:
                raise FileChatError("OpenCode did not return a session id")
            if thread_registry is not None and thread_key:
                thread_registry.set_thread_id(thread_key, session_id)

        turn_id = build_turn_id(session_id)
        if on_meta is not None:
            try:
                maybe = on_meta("opencode", session_id, turn_id)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.debug("file chat opencode meta failed", exc_info=True)

        model_payload = split_model_id(model)
        await supervisor.mark_turn_started(repo_root)

        usage_parts: list[Dict[str, Any]] = []

        async def _part_handler(
            part_type: str, part: Any, turn_id_arg: Optional[str] | None
        ) -> None:
            if part_type == "usage" and on_usage is not None:
                usage_parts.append(part)
                try:
                    maybe = on_usage(part)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    logger.debug("file chat usage handler failed", exc_info=True)

        ready_event = asyncio.Event()
        output_task = asyncio.create_task(
            collect_opencode_output(
                client,
                session_id=session_id,
                workspace_path=str(repo_root),
                model_payload=model_payload,
                permission_policy=PERMISSION_ALLOW,
                question_policy="auto_first_option",
                should_stop=interrupt_event.is_set,
                ready_event=ready_event,
                part_handler=_part_handler,
                stall_timeout_seconds=stall_timeout_seconds,
            )
        )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ready_event.wait(), timeout=2.0)

        prompt_task = asyncio.create_task(
            client.prompt_async(
                session_id,
                message=prompt,
                model=model_payload,
                variant=reasoning,
            )
        )
        timeout_task = asyncio.create_task(asyncio.sleep(FILE_CHAT_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            prompt_response = None
            try:
                prompt_response = await prompt_task
            except Exception as exc:
                interrupt_event.set()
                output_task.cancel()
                raise FileChatError(f"OpenCode prompt failed: {exc}") from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                output_task.cancel()
                return {"status": "error", "detail": "File chat timed out"}
            if interrupt_task in done:
                output_task.cancel()
                return {"status": "interrupted", "detail": "File chat interrupted"}
            output_result = await output_task
            if (not output_result.text) and prompt_response is not None:
                fallback = parse_message_response(prompt_response)
                if fallback.text:
                    output_result = type(output_result)(
                        text=fallback.text, error=fallback.error
                    )
        finally:
            timeout_task.cancel()
            interrupt_task.cancel()
            await supervisor.mark_turn_finished(repo_root)

        if output_result.error:
            raise FileChatError(output_result.error)
        agent_message = _parse_agent_message(output_result.text)
        result = {
            "status": "ok",
            "agent_message": agent_message,
            "message": output_result.text,
            "thread_id": session_id,
            "turn_id": turn_id,
            "agent": "opencode",
        }
        if usage_parts:
            result["usage_parts"] = usage_parts
        return result

    def _parse_agent_message(output: str) -> str:
        text = (output or "").strip()
        if not text:
            return "File updated via chat."
        for line in text.splitlines():
            if line.lower().startswith("agent:"):
                return line[len("agent:") :].strip() or "File updated via chat."
        first_line = text.splitlines()[0].strip()
        return (first_line[:97] + "...") if len(first_line) > 100 else first_line

    @router.get("/file-chat/pending")
    async def pending_file_patch(request: Request, target: str):
        repo_root = _resolve_repo_root(request)
        resolved = _parse_target(repo_root, target)
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        draft = drafts.get(resolved.state_key)
        if not draft:
            raise HTTPException(status_code=404, detail="No pending patch")
        current_content = _read_file(resolved.path)
        current_hash = _hash_content(current_content)
        return {
            "status": "ok",
            "target": resolved.target,
            "patch": draft.get("patch", ""),
            "content": draft.get("content", ""),
            "agent_message": draft.get("agent_message", ""),
            "created_at": draft.get("created_at", ""),
            "base_hash": draft.get("base_hash", ""),
            "current_hash": current_hash,
            "is_stale": draft.get("base_hash") not in (None, "")
            and draft.get("base_hash") != current_hash,
        }

    @router.post("/file-chat/apply")
    async def apply_file_patch(request: Request):
        body = await request.json()
        repo_root = _resolve_repo_root(request)
        resolved = _parse_target(repo_root, str(body.get("target") or ""))
        force = bool(body.get("force", False))
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        draft = drafts.get(resolved.state_key)
        if not draft:
            raise HTTPException(status_code=404, detail="No pending patch")

        current = _read_file(resolved.path)
        if (
            not force
            and draft.get("base_hash")
            and _hash_content(current) != draft["base_hash"]
        ):
            raise HTTPException(
                status_code=409,
                detail="File changed since draft created; reload before applying.",
            )

        content = draft.get("content", "")
        resolved.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(resolved.path, content)

        drafts.pop(resolved.state_key, None)
        state["drafts"] = drafts
        _save_state(repo_root, state)

        return {
            "status": "ok",
            "target": resolved.target,
            "content": _read_file(resolved.path),
            "agent_message": draft.get("agent_message", "Draft applied"),
        }

    @router.post("/file-chat/discard")
    async def discard_file_patch(request: Request):
        body = await request.json()
        repo_root = _resolve_repo_root(request)
        resolved = _parse_target(repo_root, str(body.get("target") or ""))
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        drafts.pop(resolved.state_key, None)
        state["drafts"] = drafts
        _save_state(repo_root, state)
        return {
            "status": "ok",
            "target": resolved.target,
            "content": _read_file(resolved.path),
        }

    @router.get("/file-chat/turns/{turn_id}/events")
    async def stream_file_chat_turn_events(
        turn_id: str, request: Request, thread_id: str, agent: str = "codex"
    ):
        agent_id = (agent or "").strip().lower()
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(status_code=404, detail="OpenCode unavailable")
            from ....agents.opencode.harness import OpenCodeHarness

            harness = OpenCodeHarness(supervisor)
            repo_root = _resolve_repo_root(request)
            return StreamingResponse(
                harness.stream_events(repo_root, thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        raise HTTPException(status_code=404, detail="Unknown agent")

    @router.post("/file-chat/interrupt")
    async def interrupt_file_chat(request: Request):
        body = await request.json()
        repo_root = _resolve_repo_root(request)
        resolved = _parse_target(repo_root, str(body.get("target") or ""))
        s = _get_state(request)
        async with s.chat_lock:
            ev = s.active_chats.get(resolved.state_key)
            if ev is None:
                return {"status": "ok", "detail": "No active chat to interrupt"}
            ev.set()
            return {"status": "interrupted", "detail": "File chat interrupted"}

    # Legacy ticket endpoints (thin wrappers) to keep older UIs working.

    @router.post("/tickets/{index}/chat")
    async def chat_ticket(index: int, request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent", "codex")
        model = body.get("model")
        reasoning = body.get("reasoning")
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")

        s = _get_state(request)
        async with s.chat_lock:
            existing = s.active_chats.get(target.state_key)
            if existing is not None and not existing.is_set():
                raise HTTPException(
                    status_code=409, detail="Ticket chat already running"
                )
            s.active_chats[target.state_key] = asyncio.Event()
        await _begin_turn_state(request, target, client_turn_id)

        if stream:
            return StreamingResponse(
                _stream_file_chat(
                    request,
                    repo_root,
                    target,
                    message,
                    agent=agent,
                    model=model,
                    reasoning=reasoning,
                    client_turn_id=client_turn_id,
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await _execute_file_chat(
                request,
                repo_root,
                target,
                message,
                agent=agent,
                model=model,
                reasoning=reasoning,
            )
            result = dict(result or {})
            result["client_turn_id"] = client_turn_id or ""
            await _finalize_turn_state(request, target, result)
            return result
        finally:
            await _clear_interrupt_event(request, target.state_key)

    @router.get("/tickets/{index}/chat/pending")
    async def pending_ticket_patch(index: int, request: Request):
        return await pending_file_patch(request, target=f"ticket:{int(index)}")

    @router.post("/tickets/{index}/chat/apply")
    async def apply_ticket_patch(index: int, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        force = bool(body.get("force", False)) if isinstance(body, dict) else False
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        draft = drafts.get(target.state_key)
        if not draft:
            raise HTTPException(status_code=404, detail="No pending patch")

        current = _read_file(target.path)
        if (
            not force
            and draft.get("base_hash")
            and _hash_content(current) != draft["base_hash"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Ticket changed since draft created; reload before applying.",
            )

        content = draft.get("content", "")
        target.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target.path, content)

        drafts.pop(target.state_key, None)
        state["drafts"] = drafts
        _save_state(repo_root, state)

        return {
            "status": "ok",
            "index": int(index),
            "content": _read_file(target.path),
            "agent_message": draft.get("agent_message", "Draft applied"),
        }

    @router.post("/tickets/{index}/chat/discard")
    async def discard_ticket_patch(index: int, request: Request):
        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        state = _load_state(repo_root)
        drafts = (
            state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
        )
        drafts.pop(target.state_key, None)
        state["drafts"] = drafts
        _save_state(repo_root, state)
        return {
            "status": "ok",
            "index": int(index),
            "content": _read_file(target.path),
        }

    @router.post("/tickets/{index}/chat/interrupt")
    async def interrupt_ticket_chat(index: int, request: Request):
        repo_root = _resolve_repo_root(request)
        target = _parse_target(repo_root, f"ticket:{int(index)}")
        s = _get_state(request)
        async with s.chat_lock:
            ev = s.active_chats.get(target.state_key)
            if ev is None:
                return {"status": "ok", "detail": "No active chat to interrupt"}
            ev.set()
            return {"status": "interrupted", "detail": "Ticket chat interrupted"}

    return router
