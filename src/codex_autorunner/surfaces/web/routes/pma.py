"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....agents.codex.harness import CodexHarness
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....agents.registry import validate_agent_id
from ....core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from ....core.config import load_repo_config
from ....core.flows.models import FlowRunStatus
from ....core.flows.store import FlowStore
from ....integrations.app_server.event_buffer import format_sse
from ....tickets.files import list_ticket_paths, safe_relpath, ticket_is_done
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from .agents import _available_agents, _serialize_model_catalog
from .shared import SSE_HEADERS

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 240
PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800


def _truncate(text: Optional[str], limit: int) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def _pma_prompt_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / "prompt.md"


def _load_pma_prompt(hub_root: Path) -> str:
    path = _pma_prompt_path(hub_root)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict[str, Any]]:
    db_path = repo_path / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        config = load_repo_config(repo_path)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            if not runs:
                return None
            latest = runs[0]

            ticket_dir = repo_path / ".codex-autorunner" / "tickets"
            total = 0
            done = 0
            for path in list_ticket_paths(ticket_dir):
                total += 1
                try:
                    if ticket_is_done(path):
                        done += 1
                except Exception:
                    continue

            if total == 0:
                return None

            state = latest.state if isinstance(latest.state, dict) else {}
            engine = state.get("ticket_engine") if isinstance(state, dict) else {}
            engine = engine if isinstance(engine, dict) else {}
            current_step = engine.get("total_turns")

            return {
                "status": latest.status.value,
                "done_count": done,
                "total_count": total,
                "current_step": current_step,
            }
    except Exception:
        return None


def _latest_dispatch(
    repo_root: Path, run_id: str, input_data: dict
) -> Optional[dict[str, Any]]:
    try:
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
        history_dir = outbox_paths.dispatch_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return None
        seq_dirs: list[Path] = []
        for child in history_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if len(name) == 4 and name.isdigit():
                seq_dirs.append(child)
        if not seq_dirs:
            return None
        latest_dir = sorted(seq_dirs, key=lambda p: p.name)[-1]
        seq = int(latest_dir.name)
        dispatch_path = latest_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        if errors or dispatch is None:
            return {
                "seq": seq,
                "dir": safe_relpath(latest_dir, repo_root),
                "dispatch": None,
                "errors": errors,
                "files": [],
            }
        files: list[str] = []
        for child in sorted(latest_dir.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            if child.name == "DISPATCH.md":
                continue
            if child.is_file():
                files.append(child.name)
        dispatch_dict = {
            "mode": dispatch.mode,
            "title": _truncate(dispatch.title, PMA_MAX_TEXT),
            "body": _truncate(dispatch.body, PMA_MAX_TEXT),
            "extra": dispatch.extra,
            "is_handoff": dispatch.is_handoff,
        }
        return {
            "seq": seq,
            "dir": safe_relpath(latest_dir, repo_root),
            "dispatch": dispatch_dict,
            "errors": [],
            "files": files,
        }
    except Exception:
        return None


def _gather_inbox(supervisor: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        snapshots = supervisor.list_repos()
    except Exception:
        return []
    for snap in snapshots:
        if not (snap.initialized and snap.exists_on_disk):
            continue
        repo_root = snap.path
        db_path = repo_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            continue
        try:
            config = load_repo_config(repo_root)
            with FlowStore(db_path, durable=config.durable_writes) as store:
                paused = store.list_flow_runs(
                    flow_type="ticket_flow", status=FlowRunStatus.PAUSED
                )
        except Exception:
            continue
        if not paused:
            continue
        for record in paused:
            latest = _latest_dispatch(
                repo_root, str(record.id), dict(record.input_data or {})
            )
            if not latest or not latest.get("dispatch"):
                continue
            messages.append(
                {
                    "repo_id": snap.id,
                    "repo_display_name": snap.display_name,
                    "run_id": record.id,
                    "run_created_at": record.created_at,
                    "seq": latest["seq"],
                    "dispatch": latest["dispatch"],
                    "files": latest.get("files") or [],
                }
            )
    messages.sort(key=lambda m: (m.get("run_created_at") or ""), reverse=True)
    return messages


async def _build_hub_snapshot(request: Request) -> dict[str, Any]:
    supervisor = getattr(request.app.state, "hub_supervisor", None)
    config = getattr(request.app.state, "config", None)
    if supervisor is None or config is None:
        return {"repos": [], "inbox": []}

    snapshots = await asyncio.to_thread(supervisor.list_repos)
    snapshots = sorted(snapshots, key=lambda snap: snap.id)
    repos: list[dict[str, Any]] = []
    for snap in snapshots[:PMA_MAX_REPOS]:
        summary = {
            "id": snap.id,
            "display_name": snap.display_name,
            "status": snap.status.value,
            "last_run_id": snap.last_run_id,
            "last_run_started_at": snap.last_run_started_at,
            "last_run_finished_at": snap.last_run_finished_at,
            "last_exit_code": snap.last_exit_code,
        }
        if snap.initialized and snap.exists_on_disk:
            summary["ticket_flow"] = _get_ticket_flow_summary(snap.path)
        else:
            summary["ticket_flow"] = None
        repos.append(summary)

    inbox = await asyncio.to_thread(_gather_inbox, supervisor)
    inbox = inbox[:PMA_MAX_MESSAGES]
    return {"repos": repos, "inbox": inbox}


def _format_prompt(base_prompt: str, snapshot: dict[str, Any], message: str) -> str:
    snapshot_text = json.dumps(snapshot, sort_keys=True)
    return (
        f"{base_prompt}\n\n"
        "<hub_snapshot>\n"
        f"{snapshot_text}\n"
        "</hub_snapshot>\n\n"
        "<user_message>\n"
        f"{message}\n"
        "</user_message>\n"
    )


def build_pma_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/pma")
    pma_lock = asyncio.Lock()
    pma_event: Optional[asyncio.Event] = None
    pma_active = False

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

    async def _begin_turn() -> bool:
        nonlocal pma_active
        async with pma_lock:
            if pma_active:
                return False
            pma_active = True
            return True

    async def _clear_interrupt_event() -> None:
        nonlocal pma_event
        async with pma_lock:
            pma_event = None

    @router.get("/agents")
    def list_pma_agents(request: Request) -> dict[str, Any]:
        agents, default_agent = _available_agents(request)
        return {"agents": agents, "default": default_agent}

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
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = body.get("agent")
        model = body.get("model")
        reasoning = body.get("reasoning")

        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        if not await _begin_turn():
            raise HTTPException(status_code=409, detail="PMA chat already running")

        try:
            agent_id = validate_agent_id(agent or "")
        except ValueError:
            agent_id = "codex"

        hub_root = request.app.state.config.root
        prompt_base = _load_pma_prompt(hub_root)
        snapshot = await _build_hub_snapshot(request)
        prompt = _format_prompt(prompt_base, snapshot, message)

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            await _set_active(False)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

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
            )

        async def _stream() -> AsyncIterator[str]:
            yield format_sse("status", {"status": "queued"})
            try:
                result = await _run()
                if result.get("status") == "ok":
                    raw_events = result.pop("raw_events", []) or []
                    for event in raw_events:
                        yield format_sse("app-server", event)
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
            except Exception:
                logger.exception("pma chat stream failed")
                yield format_sse("error", {"detail": "PMA chat failed"})
            finally:
                await _set_active(False)
                await _clear_interrupt_event()

        if stream:
            return StreamingResponse(
                _stream(),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            result = await _run()
            return result
        finally:
            await _set_active(False)
            await _clear_interrupt_event()

    @router.post("/interrupt")
    async def pma_interrupt() -> dict[str, Any]:
        event = await _get_interrupt_event()
        event.set()
        return {"status": "ok", "interrupted": True}

    @router.post("/thread/reset")
    async def reset_pma_thread(request: Request) -> dict[str, Any]:
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

    return router


__all__ = ["build_pma_routes"]
