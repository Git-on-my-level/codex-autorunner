"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
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
from ....core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
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

    async def _get_interrupt_event() -> asyncio.Event:
        nonlocal pma_event
        async with pma_lock:
            if pma_event is None or pma_event.is_set():
                pma_event = asyncio.Event()
            return pma_event

    async def _set_active(active: bool) -> None:
        nonlocal pma_active
        async with pma_lock:
            pma_active = active

    async def _begin_turn(client_turn_id: Optional[str]) -> bool:
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
            }
            return True

    async def _clear_interrupt_event() -> None:
        nonlocal pma_event
        async with pma_lock:
            pma_event = None

    async def _update_current(**updates: Any) -> None:
        nonlocal pma_current
        async with pma_lock:
            if pma_current is None:
                pma_current = {}
            pma_current.update(updates)

    async def _finalize_result(result: dict[str, Any]) -> None:
        nonlocal pma_current, pma_last_result, pma_active, pma_event
        async with pma_lock:
            pma_last_result = dict(result or {})
            pma_current = None
            pma_active = False
            pma_event = None

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
        if client_turn_id:
            # If caller is asking about a specific client turn id, only return the matching last result.
            if last_result.get("client_turn_id") != client_turn_id:
                last_result = {}
            if current.get("client_turn_id") != client_turn_id:
                current = {}
        return {"active": active, "current": current, "last_result": last_result}

    @router.get("/agents")
    def list_pma_agents(request: Request) -> dict[str, Any]:
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
        if on_meta is not None:
            try:
                maybe = on_meta(thread_id, handle.turn_id)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.exception("pma meta callback failed")

        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                {turn_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                turn_task.cancel()
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
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
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                output_task.cancel()
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                output_task.cancel()
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

        if not await _begin_turn(client_turn_id):
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
            await _set_active(False)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        async def _meta(thread_id: str, turn_id: str) -> None:
            await _update_current(
                client_turn_id=client_turn_id or "",
                status="running",
                agent=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

        async def _run() -> dict[str, Any]:
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
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
                return await _execute_opencode(
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
                )
            if supervisor is None:
                return {"status": "error", "detail": "App-server unavailable"}
            return await _execute_app_server(
                supervisor,
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
                await _finalize_result(result)

            asyncio.create_task(_finalize())

            yield format_sse("status", {"status": "queued"})
            try:
                result = await asyncio.shield(run_task)
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
            await _finalize_result(result)
            return result
        finally:
            # _finalize_result already clears active/interrupt state
            pass

    @router.post("/interrupt")
    async def pma_interrupt(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        event = await _get_interrupt_event()
        event.set()
        return {"status": "ok", "interrupted": True}

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
