"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile

from ....agents.codex.harness import CodexHarness
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....agents.registry import validate_agent_id
from ....core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from ....core.filebox import sanitize_filename
from ....core.logging_utils import log_event
from ....core.pma_context import (
    PMA_MAX_TEXT,
    build_hub_snapshot,
    format_pma_prompt,
    load_pma_prompt,
)
from ....core.pma_state import PmaStateStore
from ....core.text_delta_coalescer import StreamingTextCoalescer
from ....core.time_utils import now_iso
from ....integrations.app_server.event_buffer import format_sse
from .agents import _available_agents, _serialize_model_catalog
from .shared import SSE_HEADERS

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 240


def build_pma_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/pma")
    pma_lock = asyncio.Lock()
    pma_event: Optional[asyncio.Event] = None
    pma_active = False
    pma_current: Optional[dict[str, Any]] = None
    pma_last_result: Optional[dict[str, Any]] = None
    pma_state_store: Optional[PmaStateStore] = None
    pma_state_root: Optional[Path] = None

    def _normalize_optional_text(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
        if not isinstance(pma_config, dict):
            pma_config = {}
        return {
            "enabled": bool(pma_config.get("enabled", True)),
            "default_agent": _normalize_optional_text(pma_config.get("default_agent")),
            "model": _normalize_optional_text(pma_config.get("model")),
            "reasoning": _normalize_optional_text(pma_config.get("reasoning")),
        }

    def _get_state_store(request: Request) -> PmaStateStore:
        nonlocal pma_state_store, pma_state_root
        hub_root = request.app.state.config.root
        if pma_state_store is None or pma_state_root != hub_root:
            pma_state_store = PmaStateStore(hub_root)
            pma_state_root = hub_root
        return pma_state_store

    async def _persist_state(store: Optional[PmaStateStore]) -> None:
        if store is None:
            return
        async with pma_lock:
            state = {
                "version": 1,
                "active": bool(pma_active),
                "current": dict(pma_current or {}),
                "last_result": dict(pma_last_result or {}),
                "updated_at": now_iso(),
            }
        try:
            store.save(state)
        except Exception:
            logger.exception("Failed to persist PMA state")

    def _truncate_text(value: Any, limit: int) -> str:
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 3)] + "..."

    def _format_last_result(
        result: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, Any]:
        status = result.get("status") or "error"
        message = result.get("message")
        detail = result.get("detail")
        text = message if isinstance(message, str) and message else detail
        summary = _truncate_text(text or "", PMA_MAX_TEXT)
        return {
            "status": status,
            "message": summary,
            "detail": (
                _truncate_text(detail or "", PMA_MAX_TEXT)
                if isinstance(detail, str)
                else None
            ),
            "client_turn_id": result.get("client_turn_id") or "",
            "agent": current.get("agent"),
            "thread_id": result.get("thread_id") or current.get("thread_id"),
            "turn_id": result.get("turn_id") or current.get("turn_id"),
            "started_at": current.get("started_at"),
            "finished_at": now_iso(),
        }

    async def _get_interrupt_event() -> asyncio.Event:
        nonlocal pma_event
        async with pma_lock:
            if pma_event is None or pma_event.is_set():
                pma_event = asyncio.Event()
            return pma_event

    async def _set_active(
        active: bool, *, store: Optional[PmaStateStore] = None
    ) -> None:
        nonlocal pma_active
        async with pma_lock:
            pma_active = active
        await _persist_state(store)

    async def _begin_turn(
        client_turn_id: Optional[str], *, store: Optional[PmaStateStore] = None
    ) -> bool:
        nonlocal pma_active, pma_current
        async with pma_lock:
            if pma_active:
                return False
            pma_active = True
            pma_current = {
                "client_turn_id": client_turn_id or "",
                "status": "starting",
                "agent": None,
                "thread_id": None,
                "turn_id": None,
                "started_at": now_iso(),
            }
        await _persist_state(store)
        return True

    async def _clear_interrupt_event() -> None:
        nonlocal pma_event
        async with pma_lock:
            pma_event = None

    async def _update_current(
        *, store: Optional[PmaStateStore] = None, **updates: Any
    ) -> None:
        nonlocal pma_current
        async with pma_lock:
            if pma_current is None:
                pma_current = {}
            pma_current.update(updates)
        await _persist_state(store)

    async def _finalize_result(
        result: dict[str, Any], *, store: Optional[PmaStateStore] = None
    ) -> None:
        nonlocal pma_current, pma_last_result, pma_active, pma_event
        async with pma_lock:
            current_snapshot = dict(pma_current or {})
            pma_last_result = _format_last_result(result or {}, current_snapshot)
            pma_current = None
            pma_active = False
            pma_event = None

        status = result.get("status") or "error"
        started_at = current_snapshot.get("started_at")
        duration_ms = None
        if started_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                duration_ms = int(
                    (datetime.now(timezone.utc) - start_dt).total_seconds() * 1000
                )
            except Exception:
                pass

        log_event(
            logger,
            logging.INFO,
            "pma.turn.completed",
            status=status,
            duration_ms=duration_ms,
            agent=current_snapshot.get("agent"),
            client_turn_id=current_snapshot.get("client_turn_id"),
            thread_id=pma_last_result.get("thread_id"),
            turn_id=pma_last_result.get("turn_id"),
            error=result.get("detail") if status == "error" else None,
        )

        await _persist_state(store)

    async def _get_current_snapshot() -> dict[str, Any]:
        async with pma_lock:
            return dict(pma_current or {})

    async def _interrupt_active(
        request: Request, *, reason: str, source: str = "unknown"
    ) -> dict[str, Any]:
        event = await _get_interrupt_event()
        event.set()
        current = await _get_current_snapshot()
        agent_id = (current.get("agent") or "").strip().lower()
        thread_id = current.get("thread_id")
        turn_id = current.get("turn_id")
        client_turn_id = current.get("client_turn_id")
        hub_root = request.app.state.config.root

        log_event(
            logger,
            logging.INFO,
            "pma.turn.interrupted",
            agent=agent_id or None,
            client_turn_id=client_turn_id or None,
            thread_id=thread_id,
            turn_id=turn_id,
            reason=reason,
            source=source,
        )

        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is not None and thread_id:
                harness = OpenCodeHarness(supervisor)
                await harness.interrupt(hub_root, thread_id, turn_id)
        else:
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            events = getattr(request.app.state, "app_server_events", None)
            if supervisor is not None and events is not None and thread_id and turn_id:
                harness = CodexHarness(supervisor, events)
                try:
                    await harness.interrupt(hub_root, thread_id, turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
        return {
            "status": "ok",
            "interrupted": bool(event.is_set()),
            "detail": reason,
            "agent": agent_id or None,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }

    @router.get("/active")
    async def pma_active_status(
        request: Request, client_turn_id: Optional[str] = None
    ) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        async with pma_lock:
            current = dict(pma_current or {})
            last_result = dict(pma_last_result or {})
            active = bool(pma_active)
        store = _get_state_store(request)
        disk_state = store.load(ensure_exists=True)
        if isinstance(disk_state, dict):
            disk_current = (
                disk_state.get("current")
                if isinstance(disk_state.get("current"), dict)
                else {}
            )
            disk_last = (
                disk_state.get("last_result")
                if isinstance(disk_state.get("last_result"), dict)
                else {}
            )
            if not current and disk_current:
                current = dict(disk_current)
            if not last_result and disk_last:
                last_result = dict(disk_last)
            if not active and disk_state.get("active"):
                active = True
        if client_turn_id:
            # If caller is asking about a specific client turn id, only return the matching last result.
            if last_result.get("client_turn_id") != client_turn_id:
                last_result = {}
            if current.get("client_turn_id") != client_turn_id:
                current = {}
        return {"active": active, "current": current, "last_result": last_result}

    @router.get("/agents")
    def list_pma_agents(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if (
            getattr(request.app.state, "app_server_supervisor", None) is None
            and getattr(request.app.state, "opencode_supervisor", None) is None
        ):
            raise HTTPException(status_code=404, detail="PMA unavailable")
        agents, default_agent = _available_agents(request)
        defaults = _get_pma_config(request)
        payload: dict[str, Any] = {"agents": agents, "default": default_agent}
        if defaults.get("model") or defaults.get("reasoning"):
            payload["defaults"] = {
                key: value
                for key, value in {
                    "model": defaults.get("model"),
                    "reasoning": defaults.get("reasoning"),
                }.items()
                if value
            }
        return payload

    @router.get("/agents/{agent}/models")
    async def list_pma_agent_models(agent: str, request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        agent_id = (agent or "").strip().lower()
        hub_root = request.app.state.config.root
        if agent_id == "codex":
            supervisor = request.app.state.app_server_supervisor
            events = request.app.state.app_server_events
            if supervisor is None:
                raise HTTPException(status_code=404, detail="Codex harness unavailable")
            codex_harness = CodexHarness(supervisor, events)
            catalog = await codex_harness.model_catalog(hub_root)
            return _serialize_model_catalog(catalog)
        if agent_id == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(
                    status_code=404, detail="OpenCode harness unavailable"
                )
            try:
                opencode_harness = OpenCodeHarness(supervisor)
                catalog = await opencode_harness.model_catalog(hub_root)
                return _serialize_model_catalog(catalog)
            except OpenCodeSupervisorError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail="Unknown agent")

    async def _execute_app_server(
        supervisor: Any,
        events: Any,
        hub_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        on_meta: Optional[Any] = None,
    ) -> dict[str, Any]:
        client = await supervisor.get_client(hub_root)

        thread_id = None
        if thread_registry is not None and thread_key:
            thread_id = thread_registry.get_thread_id(thread_key)
        if thread_id:
            try:
                await client.thread_resume(thread_id)
            except Exception:
                thread_id = None

        if not thread_id:
            thread = await client.thread_start(str(hub_root))
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise HTTPException(
                    status_code=502, detail="App-server did not return a thread id"
                )
            if thread_registry is not None and thread_key:
                thread_registry.set_thread_id(thread_key, thread_id)

        turn_kwargs: dict[str, Any] = {}
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
        codex_harness = CodexHarness(supervisor, events)
        if on_meta is not None:
            try:
                maybe = on_meta(thread_id, handle.turn_id)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.exception("pma meta callback failed")

        if interrupt_event.is_set():
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except Exception:
                logger.exception("Failed to interrupt Codex turn")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                {turn_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                try:
                    await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
                turn_task.cancel()
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                try:
                    await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
                turn_task.cancel()
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
            turn_result = await turn_task
        finally:
            timeout_task.cancel()
            interrupt_task.cancel()

        if getattr(turn_result, "errors", None):
            errors = turn_result.errors
            raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
        raw_events = getattr(turn_result, "raw_events", []) or []
        return {
            "status": "ok",
            "message": output,
            "thread_id": thread_id,
            "turn_id": handle.turn_id,
            "raw_events": raw_events,
        }

    async def _execute_opencode(
        supervisor: Any,
        hub_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        stall_timeout_seconds: Optional[float] = None,
        on_meta: Optional[Any] = None,
        part_handler: Optional[Any] = None,
    ) -> dict[str, Any]:
        from ....agents.opencode.runtime import (
            PERMISSION_ALLOW,
            build_turn_id,
            collect_opencode_output,
            extract_session_id,
            parse_message_response,
            split_model_id,
        )

        client = await supervisor.get_client(hub_root)
        session_id = None
        if thread_registry is not None and thread_key:
            session_id = thread_registry.get_thread_id(thread_key)
        if not session_id:
            session = await client.create_session(directory=str(hub_root))
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not isinstance(session_id, str) or not session_id:
                raise HTTPException(
                    status_code=502, detail="OpenCode did not return a session id"
                )
            if thread_registry is not None and thread_key:
                thread_registry.set_thread_id(thread_key, session_id)
        if on_meta is not None:
            try:
                maybe = on_meta(session_id, build_turn_id(session_id))
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.exception("pma meta callback failed")

        opencode_harness = OpenCodeHarness(supervisor)
        if interrupt_event.is_set():
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        model_payload = split_model_id(model)
        await supervisor.mark_turn_started(hub_root)

        ready_event = asyncio.Event()
        output_task = asyncio.create_task(
            collect_opencode_output(
                client,
                session_id=session_id,
                workspace_path=str(hub_root),
                model_payload=model_payload,
                permission_policy=PERMISSION_ALLOW,
                question_policy="auto_first_option",
                should_stop=interrupt_event.is_set,
                ready_event=ready_event,
                part_handler=part_handler,
                stall_timeout_seconds=stall_timeout_seconds,
            )
        )
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        prompt_task = asyncio.create_task(
            client.prompt_async(
                session_id,
                message=prompt,
                model=model_payload,
                variant=reasoning,
            )
        )
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            prompt_response = None
            try:
                prompt_response = await prompt_task
            except Exception as exc:
                interrupt_event.set()
                output_task.cancel()
                await opencode_harness.interrupt(hub_root, session_id, None)
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                output_task.cancel()
                await opencode_harness.interrupt(hub_root, session_id, None)
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                output_task.cancel()
                await opencode_harness.interrupt(hub_root, session_id, None)
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
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
            await supervisor.mark_turn_finished(hub_root)

        if output_result.error:
            raise HTTPException(status_code=502, detail=output_result.error)
        return {
            "status": "ok",
            "message": output_result.text,
            "thread_id": session_id,
            "turn_id": build_turn_id(session_id),
        }

    @router.post("/chat")
    async def pma_chat(request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent")
        model = _normalize_optional_text(body.get("model"))
        reasoning = _normalize_optional_text(body.get("reasoning"))
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        store = _get_state_store(request)
        store.load(ensure_exists=True)
        if not await _begin_turn(client_turn_id, store=store):
            raise HTTPException(status_code=409, detail="PMA chat already running")

        agents, available_default = _available_agents(request)
        available_ids = {entry.get("id") for entry in agents if isinstance(entry, dict)}

        defaults = _get_pma_config(request)

        def _resolve_default_agent() -> str:
            configured_default = defaults.get("default_agent")
            try:
                candidate = validate_agent_id(configured_default or "")
            except ValueError:
                candidate = None
            if candidate and candidate in available_ids:
                return candidate
            return available_default

        try:
            agent_id = validate_agent_id(agent or "")
        except ValueError:
            agent_id = _resolve_default_agent()

        if not model and defaults.get("model"):
            model = defaults["model"]
        if not reasoning and defaults.get("reasoning"):
            reasoning = defaults["reasoning"]

        hub_root = request.app.state.config.root
        prompt_base = load_pma_prompt(hub_root)
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        snapshot = await build_hub_snapshot(supervisor, hub_root=hub_root)
        prompt = format_pma_prompt(prompt_base, snapshot, message)

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            await _set_active(False, store=store)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        meta_future: asyncio.Future[tuple[str, str]] = (
            asyncio.get_running_loop().create_future()
        )
        token_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _meta(thread_id: str, turn_id: str) -> None:
            await _update_current(
                store=store,
                client_turn_id=client_turn_id or "",
                status="running",
                agent=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

            log_event(
                logger,
                logging.INFO,
                "pma.turn.started",
                agent=agent_id,
                client_turn_id=client_turn_id or None,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if not meta_future.done():
                meta_future.set_result((thread_id, turn_id))

        async def _stream_app_server_tokens(
            queue: asyncio.Queue[str],
        ) -> None:
            try:
                thread_id, turn_id = await meta_future
            except asyncio.CancelledError:
                return
            if not thread_id or not turn_id:
                return
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                return
            coalescer = StreamingTextCoalescer()
            try:
                async for chunk in events.stream(thread_id, turn_id):
                    if interrupt_event.is_set():
                        break
                    if not chunk or chunk.startswith(":"):
                        continue
                    event = "message"
                    data_lines: list[str] = []
                    for line in chunk.splitlines():
                        if line.startswith("event:"):
                            event = line[len("event:") :].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:") :].lstrip())
                        elif line.strip():
                            data_lines.append(line)
                    if event != "app-server" or not data_lines:
                        continue
                    try:
                        payload = json.loads("\n".join(data_lines))
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    message = payload.get("message")
                    if not isinstance(message, dict):
                        continue
                    method = message.get("method")
                    if method not in ("item/agentMessage/delta", "turn/streamDelta"):
                        continue
                    params = message.get("params")
                    delta = None
                    if isinstance(params, dict):
                        raw = params.get("delta") or params.get("text")
                        if isinstance(raw, str):
                            delta = raw
                    if not delta:
                        continue
                    for text in coalescer.add(delta):
                        await queue.put(format_sse("token", {"token": text}))
            except asyncio.CancelledError:
                pass
            finally:
                for text in coalescer.flush():
                    await queue.put(format_sse("token", {"token": text}))

        async def _run() -> dict[str, Any]:
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            events = getattr(request.app.state, "app_server_events", None)
            opencode = getattr(request.app.state, "opencode_supervisor", None)
            registry = getattr(request.app.state, "app_server_threads", None)
            stall_timeout_seconds = None
            try:
                stall_timeout_seconds = (
                    request.app.state.config.opencode.session_stall_timeout_seconds
                )
            except Exception:
                stall_timeout_seconds = None

            if agent_id == "opencode":
                if opencode is None:
                    return {"status": "error", "detail": "OpenCode unavailable"}
                part_handler = None
                opencode_coalescer: Optional[StreamingTextCoalescer] = None
                if stream:
                    opencode_coalescer = StreamingTextCoalescer()

                    async def _handle_part(
                        part_type: str, part: dict[str, Any], delta_text: Optional[str]
                    ) -> None:
                        if (
                            part_type == "text"
                            and isinstance(delta_text, str)
                            and delta_text
                        ):
                            for chunk in opencode_coalescer.add(delta_text):
                                await token_queue.put(
                                    format_sse("token", {"token": chunk})
                                )
                        elif part_type == "usage" and isinstance(part, dict):
                            await token_queue.put(format_sse("token_usage", part))

                    part_handler = _handle_part
                result = await _execute_opencode(
                    opencode,
                    hub_root,
                    prompt,
                    interrupt_event,
                    model=model,
                    reasoning=reasoning,
                    thread_registry=registry,
                    thread_key=PMA_OPENCODE_KEY,
                    stall_timeout_seconds=stall_timeout_seconds,
                    on_meta=_meta,
                    part_handler=part_handler,
                )
                if opencode_coalescer is not None:
                    for chunk in opencode_coalescer.flush():
                        await token_queue.put(format_sse("token", {"token": chunk}))
                return result
            if supervisor is None or events is None:
                return {"status": "error", "detail": "App-server unavailable"}
            return await _execute_app_server(
                supervisor,
                events,
                hub_root,
                prompt,
                interrupt_event,
                model=model,
                reasoning=reasoning,
                thread_registry=registry,
                thread_key=PMA_KEY,
                on_meta=_meta,
            )

        async def _stream() -> AsyncIterator[str]:
            # IMPORTANT: shield the underlying turn from client disconnects (e.g. page refresh).
            # We also store the final result server-side so the UI can recover after reload.
            run_task = asyncio.create_task(_run())
            token_task: Optional[asyncio.Task[None]] = None
            if agent_id != "opencode":
                token_task = asyncio.create_task(_stream_app_server_tokens(token_queue))

            async def _finalize() -> None:
                result: dict[str, Any]
                try:
                    result = await run_task
                except Exception as exc:
                    logger.exception("pma chat task failed")
                    result = {
                        "status": "error",
                        "detail": str(exc) or "PMA chat failed",
                    }
                result = dict(result or {})
                result["client_turn_id"] = client_turn_id or ""
                await _finalize_result(result, store=store)

            asyncio.create_task(_finalize())

            yield format_sse("status", {"status": "starting"})
            try:
                result: Optional[dict[str, Any]] = None
                while True:
                    if run_task.done() and token_queue.empty():
                        break
                    queue_get = asyncio.create_task(token_queue.get())
                    done, _ = await asyncio.wait(
                        {queue_get, run_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if queue_get in done:
                        payload = queue_get.result()
                        if payload:
                            yield payload
                        continue
                    queue_get.cancel()
                    result = await asyncio.shield(run_task)
                    break
                if result is None:
                    result = await asyncio.shield(run_task)
                if token_task is not None:
                    token_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await token_task
                while not token_queue.empty():
                    payload = token_queue.get_nowait()
                    if payload:
                        yield payload
                if result.get("status") == "ok":
                    raw_events = result.pop("raw_events", []) or []
                    for event in raw_events:
                        yield format_sse("app-server", event)
                    result["client_turn_id"] = client_turn_id or ""
                    yield format_sse("update", result)
                    yield format_sse("done", {"status": "ok"})
                elif result.get("status") == "interrupted":
                    yield format_sse(
                        "interrupted",
                        {"detail": result.get("detail") or "PMA chat interrupted"},
                    )
                else:
                    yield format_sse(
                        "error", {"detail": result.get("detail") or "PMA chat failed"}
                    )
            except asyncio.CancelledError:
                # Client disconnected; the run continues in the background and can be recovered via /active.
                return
            except Exception:
                logger.exception("pma chat stream failed")
                yield format_sse("error", {"detail": "PMA chat failed"})
            finally:
                if token_task is not None:
                    token_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await token_task

        if stream:
            return StreamingResponse(
                _stream(),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await _run()
            result = dict(result or {})
            result["client_turn_id"] = client_turn_id or ""
            await _finalize_result(result, store=store)
            return result
        finally:
            # _finalize_result already clears active/interrupt state
            pass

    @router.post("/interrupt")
    async def pma_interrupt(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        return await _interrupt_active(
            request, reason="PMA chat interrupted", source="user_request"
        )

    @router.post("/thread/reset")
    async def reset_pma_thread(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        body = await request.json()
        agent = (body.get("agent") or "").strip().lower()
        registry = request.app.state.app_server_threads
        cleared = []
        if agent in ("", "all", None):
            if registry.reset_thread(PMA_KEY):
                cleared.append(PMA_KEY)
            if registry.reset_thread(PMA_OPENCODE_KEY):
                cleared.append(PMA_OPENCODE_KEY)
        elif agent == "opencode":
            if registry.reset_thread(PMA_OPENCODE_KEY):
                cleared.append(PMA_OPENCODE_KEY)
        else:
            if registry.reset_thread(PMA_KEY):
                cleared.append(PMA_KEY)
        return {"status": "ok", "cleared": cleared}

    @router.get("/turns/{turn_id}/events")
    async def stream_pma_turn_events(
        turn_id: str, request: Request, thread_id: str, agent: str = "codex"
    ):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        agent_id = (agent or "").strip().lower()
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Codex events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        if agent_id == "opencode":
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                raise HTTPException(status_code=404, detail="OpenCode unavailable")
            harness = OpenCodeHarness(supervisor)
            return StreamingResponse(
                harness.stream_events(
                    request.app.state.config.root, thread_id, turn_id
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        raise HTTPException(status_code=404, detail="Unknown agent")

    def _serialize_pma_entry(
        entry: dict[str, Any], *, request: Request
    ) -> dict[str, Any]:
        base = request.scope.get("root_path", "") or ""
        box = entry.get("box", "inbox")
        filename = entry.get("name", "")
        download = f"{base}/hub/pma/files/{box}/{filename}"
        return {
            "name": filename,
            "box": box,
            "size": entry.get("size"),
            "modified_at": entry.get("modified_at"),
            "source": "pma",
            "url": download,
        }

    @router.get("/files")
    def list_pma_files(request: Request) -> dict[str, list[dict[str, Any]]]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        pma_dir = hub_root / ".codex-autorunner" / "pma"
        result: dict[str, list[dict[str, Any]]] = {"inbox": [], "outbox": []}
        for box in ["inbox", "outbox"]:
            box_dir = pma_dir / box
            if box_dir.exists():
                files = [
                    {
                        "name": f.name,
                        "box": box,
                        "size": f.stat().st_size if f.is_file() else None,
                        "modified_at": (
                            datetime.fromtimestamp(
                                f.stat().st_mtime, tz=timezone.utc
                            ).isoformat()
                            if f.is_file()
                            else None
                        ),
                    }
                    for f in box_dir.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ]
                result[box] = [
                    _serialize_pma_entry(f, request=request)
                    for f in sorted(files, key=lambda x: x["name"])
                ]
        return result

    @router.post("/files/{box}")
    async def upload_pma_file(box: str, request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        max_upload_bytes = request.app.state.config.pma.max_upload_bytes

        form = await request.form()
        saved = []
        for _form_field_name, file in form.items():
            try:
                if isinstance(file, UploadFile):
                    content = await file.read()
                    filename = file.filename or ""
                else:
                    content = file if isinstance(file, bytes) else str(file).encode()
                    filename = ""
            except Exception as exc:
                logger.warning("Failed to read PMA upload: %s", exc)
                raise HTTPException(
                    status_code=400, detail="Failed to read file"
                ) from exc
            if len(content) > max_upload_bytes:
                logger.warning(
                    "File too large for PMA upload: %s (%d bytes)",
                    filename,
                    len(content),
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large (max {max_upload_bytes} bytes)",
                )
            try:
                target_path = _pma_target_path(hub_root, box, filename)
            except HTTPException:
                logger.warning("Invalid filename in PMA upload: %s", filename)
                raise
            try:
                target_path.write_bytes(content)
                saved.append(target_path.name)
            except Exception as exc:
                logger.warning("Failed to write PMA file: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Failed to save file"
                ) from exc
        return {"status": "ok", "saved": saved}

    def _pma_target_path(hub_root: Path, box: str, filename: str) -> Path:
        """Return a resolved path within the PMA box folder, rejecting traversal attempts."""
        box_dir = hub_root / ".codex-autorunner" / "pma" / box
        box_dir.mkdir(parents=True, exist_ok=True)
        try:
            safe_name = sanitize_filename(filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid filename") from exc
        root = box_dir.resolve()
        candidate = (root / safe_name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid filename") from exc
        if candidate.parent != root:
            raise HTTPException(status_code=400, detail="Invalid filename")
        return candidate

    @router.get("/files/{box}/{filename}")
    def download_pma_file(box: str, filename: str, request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        try:
            file_path = _pma_target_path(hub_root, box, filename)
        except HTTPException:
            logger.warning("Invalid filename in PMA download: %s", filename)
            raise
        if not file_path.exists() or not file_path.is_file():
            logger.warning("File not found in PMA download: %s", filename)
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(file_path, filename=file_path.name)

    @router.delete("/files/{box}/{filename}")
    def delete_pma_file(box: str, filename: str, request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        try:
            file_path = _pma_target_path(hub_root, box, filename)
        except HTTPException:
            logger.warning("Invalid filename in PMA delete: %s", filename)
            raise
        if not file_path.exists() or not file_path.is_file():
            logger.warning("File not found in PMA delete: %s", filename)
            raise HTTPException(status_code=404, detail="File not found")
        try:
            file_path.unlink()
        except Exception as exc:
            logger.warning("Failed to delete PMA file: %s", exc)
            raise HTTPException(
                status_code=500, detail="Failed to delete file"
            ) from exc
        return {"status": "ok"}

    @router.delete("/files/{box}")
    def delete_pma_box(box: str, request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        box_dir = hub_root / ".codex-autorunner" / "pma" / box
        if box_dir.exists():
            for f in box_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    f.unlink()
        return {"status": "ok"}

    return router


__all__ = ["build_pma_routes"]
