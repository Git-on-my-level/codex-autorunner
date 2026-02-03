"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile

from ....agents.codex.harness import CodexHarness
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....agents.registry import validate_agent_id
from ....bootstrap import (
    ensure_pma_docs,
    pma_about_content,
    pma_active_context_content,
    pma_agents_content,
    pma_context_log_content,
    pma_prompt_content,
)
from ....core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from ....core.filebox import sanitize_filename
from ....core.logging_utils import log_event
from ....core.pma_audit import PmaActionType, PmaAuditLog
from ....core.pma_context import (
    PMA_MAX_TEXT,
    build_hub_snapshot,
    format_pma_prompt,
    load_pma_prompt,
)
from ....core.pma_lifecycle import PmaLifecycleRouter
from ....core.pma_queue import PmaQueue, QueueItemState
from ....core.pma_safety import PmaSafetyChecker, PmaSafetyConfig
from ....core.pma_state import PmaStateStore
from ....core.time_utils import now_iso
from ....core.utils import atomic_write
from .agents import _available_agents, _serialize_model_catalog
from .shared import SSE_HEADERS

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 28800
PMA_CONTEXT_SNAPSHOT_MAX_BYTES = 200_000
PMA_CONTEXT_LOG_SOFT_LIMIT_BYTES = 5_000_000
PMA_BULK_DELETE_SAMPLE_LIMIT = 10


def build_pma_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/pma")
    pma_lock = asyncio.Lock()
    pma_event: Optional[asyncio.Event] = None
    pma_active = False
    pma_current: Optional[dict[str, Any]] = None
    pma_last_result: Optional[dict[str, Any]] = None
    pma_state_store: Optional[PmaStateStore] = None
    pma_state_root: Optional[Path] = None
    pma_safety_checker: Optional[PmaSafetyChecker] = None
    pma_safety_root: Optional[Path] = None
    pma_audit_log: Optional[PmaAuditLog] = None
    pma_queue: Optional[PmaQueue] = None
    pma_queue_root: Optional[Path] = None
    lane_workers: dict[str, asyncio.Task] = {}
    lane_cancel_events: dict[str, asyncio.Event] = {}
    item_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

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
            "active_context_max_lines": int(
                pma_config.get("active_context_max_lines", 200)
            ),
            "max_text_chars": int(pma_config.get("max_text_chars", 800)),
        }

    def _build_idempotency_key(
        *,
        lane_id: str,
        agent: Optional[str],
        model: Optional[str],
        reasoning: Optional[str],
        client_turn_id: Optional[str],
        message: str,
    ) -> str:
        payload = {
            "lane_id": lane_id,
            "agent": agent,
            "model": model,
            "reasoning": reasoning,
            "client_turn_id": client_turn_id,
            "message": message,
        }
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"pma:{digest}"

    def _get_state_store(request: Request) -> PmaStateStore:
        nonlocal pma_state_store, pma_state_root
        hub_root = request.app.state.config.root
        if pma_state_store is None or pma_state_root != hub_root:
            pma_state_store = PmaStateStore(hub_root)
            pma_state_root = hub_root
        return pma_state_store

    def _get_safety_checker(request: Request) -> PmaSafetyChecker:
        nonlocal pma_safety_checker, pma_safety_root, pma_audit_log
        hub_root = request.app.state.config.root
        if pma_safety_checker is None or pma_safety_root != hub_root:
            raw = getattr(request.app.state.config, "raw", {})
            pma_config = raw.get("pma", {}) if isinstance(raw, dict) else {}
            safety_config = PmaSafetyConfig(
                dedup_window_seconds=pma_config.get("dedup_window_seconds", 300),
                max_duplicate_actions=pma_config.get("max_duplicate_actions", 3),
                rate_limit_window_seconds=pma_config.get(
                    "rate_limit_window_seconds", 60
                ),
                max_actions_per_window=pma_config.get("max_actions_per_window", 20),
                circuit_breaker_threshold=pma_config.get(
                    "circuit_breaker_threshold", 5
                ),
                circuit_breaker_cooldown_seconds=pma_config.get(
                    "circuit_breaker_cooldown_seconds", 600
                ),
                enable_dedup=pma_config.get("enable_dedup", True),
                enable_rate_limit=pma_config.get("enable_rate_limit", True),
                enable_circuit_breaker=pma_config.get("enable_circuit_breaker", True),
            )
            pma_audit_log = PmaAuditLog(hub_root)
            pma_safety_checker = PmaSafetyChecker(hub_root, config=safety_config)
            pma_safety_root = hub_root
        return pma_safety_checker

    def _get_pma_queue(request: Request) -> PmaQueue:
        nonlocal pma_queue, pma_queue_root
        hub_root = request.app.state.config.root
        if pma_queue is None or pma_queue_root != hub_root:
            pma_queue = PmaQueue(hub_root)
            pma_queue_root = hub_root
        return pma_queue

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
        client_turn_id: Optional[str],
        *,
        store: Optional[PmaStateStore] = None,
        lane_id: Optional[str] = None,
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
                "lane_id": lane_id or "",
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
        result: dict[str, Any],
        *,
        request: Request,
        store: Optional[PmaStateStore] = None,
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

        if status == "ok":
            action_type = PmaActionType.CHAT_COMPLETED
        elif status == "interrupted":
            action_type = PmaActionType.CHAT_INTERRUPTED
        else:
            action_type = PmaActionType.CHAT_FAILED

        _get_safety_checker(request).record_action(
            action_type=action_type,
            agent=current_snapshot.get("agent"),
            thread_id=pma_last_result.get("thread_id"),
            turn_id=pma_last_result.get("turn_id"),
            client_turn_id=current_snapshot.get("client_turn_id"),
            details={"status": status, "duration_ms": duration_ms},
            status=status,
            error=result.get("detail") if status == "error" else None,
        )
        _get_safety_checker(request).record_chat_result(
            agent=current_snapshot.get("agent") or "",
            status=status,
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

    async def _ensure_lane_worker(lane_id: str, request: Request) -> None:
        nonlocal lane_workers, lane_cancel_events
        if lane_id in lane_workers and not lane_workers[lane_id].done():
            return

        cancel_event = asyncio.Event()
        lane_cancel_events[lane_id] = cancel_event

        async def lane_worker():
            queue = _get_pma_queue(request)
            await queue.replay_pending(lane_id)
            while not cancel_event.is_set():
                item = await queue.dequeue(lane_id)
                if item is None:
                    await queue.wait_for_lane_item(lane_id, cancel_event)
                    continue

                if cancel_event.is_set():
                    await queue.fail_item(item, "cancelled by lane stop")
                    continue

                result_future = item_futures.get(item.item_id)
                try:
                    result = await _execute_queue_item(item, request)
                    await queue.complete_item(item, result)
                    if result_future and not result_future.done():
                        result_future.set_result(result)
                except Exception as exc:
                    logger.exception("Failed to process queue item %s", item.item_id)
                    error_result = {"status": "error", "detail": str(exc)}
                    await queue.fail_item(item, str(exc))
                    if result_future and not result_future.done():
                        result_future.set_result(error_result)
                finally:
                    item_futures.pop(item.item_id, None)

        task = asyncio.create_task(lane_worker())
        lane_workers[lane_id] = task

    async def _execute_queue_item(item: Any, request: Request) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        payload = item.payload

        client_turn_id = payload.get("client_turn_id")
        message = payload.get("message", "")
        agent = payload.get("agent")
        model = _normalize_optional_text(payload.get("model"))
        reasoning = _normalize_optional_text(payload.get("reasoning"))

        store = _get_state_store(request)
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

        safety_checker = _get_safety_checker(request)
        safety_check = safety_checker.check_chat_start(
            agent_id, message, client_turn_id
        )
        if not safety_check.allowed:
            detail = safety_check.reason or "PMA action blocked by safety check"
            if safety_check.details:
                detail = f"{detail}: {safety_check.details}"
            return {"status": "error", "detail": detail}

        started = await _begin_turn(
            client_turn_id, store=store, lane_id=getattr(item, "lane_id", None)
        )
        if not started:
            logger.warning("PMA turn started while another was active")

        if not model and defaults.get("model"):
            model = defaults["model"]
        if not reasoning and defaults.get("reasoning"):
            reasoning = defaults["reasoning"]

        try:
            prompt_base = load_pma_prompt(hub_root)
            supervisor = getattr(request.app.state, "hub_supervisor", None)
            snapshot = await build_hub_snapshot(supervisor, hub_root=hub_root)
            prompt = format_pma_prompt(
                prompt_base, snapshot, message, hub_root=hub_root
            )
        except Exception as exc:
            error_result = {
                "status": "error",
                "detail": str(exc),
                "client_turn_id": client_turn_id or "",
            }
            if started:
                await _finalize_result(error_result, request=request, store=store)
            return error_result

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            result = {"status": "interrupted", "detail": "PMA chat interrupted"}
            if started:
                await _finalize_result(result, request=request, store=store)
            return result

        meta_future: asyncio.Future[tuple[str, str]] = (
            asyncio.get_running_loop().create_future()
        )

        async def _meta(thread_id: str, turn_id: str) -> None:
            await _update_current(
                store=store,
                client_turn_id=client_turn_id or "",
                status="running",
                agent=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

            safety_checker.record_action(
                action_type=PmaActionType.CHAT_STARTED,
                agent=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                details={"message": message[:200]},
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

        try:
            if agent_id == "opencode":
                if opencode is None:
                    result = {"status": "error", "detail": "OpenCode unavailable"}
                    if started:
                        await _finalize_result(result, request=request, store=store)
                    return result
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
                )
            else:
                if supervisor is None or events is None:
                    result = {"status": "error", "detail": "App-server unavailable"}
                    if started:
                        await _finalize_result(result, request=request, store=store)
                    return result
                result = await _execute_app_server(
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
        except Exception as exc:
            if started:
                error_result = {
                    "status": "error",
                    "detail": str(exc),
                    "client_turn_id": client_turn_id or "",
                }
                await _finalize_result(error_result, request=request, store=store)
            raise

        result = dict(result or {})
        result["client_turn_id"] = client_turn_id or ""
        await _finalize_result(result, request=request, store=store)
        return result

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

    @router.get("/audit/recent")
    def get_pma_audit_log(request: Request, limit: int = 100):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        safety_checker = _get_safety_checker(request)
        entries = safety_checker._audit_log.list_recent(limit=limit)
        return {
            "entries": [
                {
                    "entry_id": e.entry_id,
                    "action_type": e.action_type.value,
                    "timestamp": e.timestamp,
                    "agent": e.agent,
                    "thread_id": e.thread_id,
                    "turn_id": e.turn_id,
                    "client_turn_id": e.client_turn_id,
                    "details": e.details,
                    "status": e.status,
                    "error": e.error,
                    "fingerprint": e.fingerprint,
                }
                for e in entries
            ]
        }

    @router.get("/safety/stats")
    def get_pma_safety_stats(request: Request):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        safety_checker = _get_safety_checker(request)
        return safety_checker.get_stats()

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
        agent = _normalize_optional_text(body.get("agent"))
        model = _normalize_optional_text(body.get("model"))
        reasoning = _normalize_optional_text(body.get("reasoning"))
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        max_text_chars = int(pma_config.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(message) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    "message exceeds max_text_chars " f"({max_text_chars} characters)"
                ),
            )

        hub_root = request.app.state.config.root
        queue = _get_pma_queue(request)

        lane_id = "pma:default"
        idempotency_key = _build_idempotency_key(
            lane_id=lane_id,
            agent=agent,
            model=model,
            reasoning=reasoning,
            client_turn_id=client_turn_id,
            message=message,
        )

        payload = {
            "message": message,
            "agent": agent,
            "model": model,
            "reasoning": reasoning,
            "client_turn_id": client_turn_id,
            "stream": stream,
            "hub_root": str(hub_root),
        }

        item, dupe_reason = await queue.enqueue(lane_id, idempotency_key, payload)
        if dupe_reason:
            logger.info("Duplicate PMA turn: %s", dupe_reason)

        if item.state == QueueItemState.DEDUPED:
            return {
                "status": "ok",
                "message": "Duplicate request - already processing",
                "deduped": True,
            }

        result_future = asyncio.get_running_loop().create_future()
        item_futures[item.item_id] = result_future

        await _ensure_lane_worker(lane_id, request)

        try:
            result = await asyncio.wait_for(result_future, timeout=PMA_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"status": "error", "detail": "PMA chat timed out"}
        except Exception:
            logger.exception("PMA chat error")
            return {
                "status": "error",
                "detail": "An error occurred processing your request",
            }

        return result

    @router.post("/interrupt")
    async def pma_interrupt(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        return await _interrupt_active(
            request, reason="PMA chat interrupted", source="user_request"
        )

    @router.post("/stop")
    async def pma_stop(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        body = await request.json() if request.headers.get("content-type") else {}
        lane_id = (body.get("lane_id") or "pma:default").strip()
        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.stop(lane_id=lane_id)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        if lane_id in lane_cancel_events:
            lane_cancel_events[lane_id].set()

        await _interrupt_active(request, reason="Lane stopped", source="user_request")

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/new")
    async def new_pma_session(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        body = await request.json()
        agent = _normalize_optional_text(body.get("agent"))
        lane_id = (body.get("lane_id") or "pma:default").strip()

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.new(agent=agent, lane_id=lane_id)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/reset")
    async def reset_pma_session(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        body = await request.json() if request.headers.get("content-type") else {}
        raw_agent = (body.get("agent") or "").strip().lower()
        agent = raw_agent or None

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.reset(agent=agent)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/compact")
    async def compact_pma_history(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        body = await request.json()
        summary = (body.get("summary") or "").strip()
        agent = _normalize_optional_text(body.get("agent"))
        thread_id = _normalize_optional_text(body.get("thread_id"))

        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.compact(
            summary=summary, agent=agent, thread_id=thread_id
        )

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/thread/reset")
    async def reset_pma_thread(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        body = await request.json()
        raw_agent = (body.get("agent") or "").strip().lower()
        agent = raw_agent or None

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.reset(agent=agent)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "cleared": result.details.get("cleared_threads", []),
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
        }

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

    @router.get("/queue")
    async def pma_queue_status(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        queue = _get_pma_queue(request)
        summary = await queue.get_queue_summary()
        return summary

    @router.get("/queue/{lane_id:path}")
    async def pma_lane_queue_status(request: Request, lane_id: str) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

        queue = _get_pma_queue(request)
        items = await queue.list_items(lane_id)
        return {
            "lane_id": lane_id,
            "items": [
                {
                    "item_id": item.item_id,
                    "state": item.state.value,
                    "enqueued_at": item.enqueued_at,
                    "started_at": item.started_at,
                    "finished_at": item.finished_at,
                    "error": item.error,
                    "dedupe_reason": item.dedupe_reason,
                }
                for item in items
            ],
        }

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
                _get_safety_checker(request).record_action(
                    action_type=PmaActionType.FILE_UPLOADED,
                    details={
                        "box": box,
                        "filename": target_path.name,
                        "size": len(content),
                    },
                )
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
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.FILE_DOWNLOADED,
            details={
                "box": box,
                "filename": file_path.name,
                "size": file_path.stat().st_size,
            },
        )
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
            file_size = file_path.stat().st_size
            file_path.unlink()
            _get_safety_checker(request).record_action(
                action_type=PmaActionType.FILE_DELETED,
                details={"box": box, "filename": file_path.name, "size": file_size},
            )
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
        deleted_files: list[str] = []
        if box_dir.exists():
            for f in box_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    deleted_files.append(f.name)
                    f.unlink()
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.FILE_BULK_DELETED,
            details={
                "box": box,
                "count": len(deleted_files),
                "sample": deleted_files[:PMA_BULK_DELETE_SAMPLE_LIMIT],
            },
        )
        return {"status": "ok"}

    @router.post("/context/snapshot")
    def snapshot_pma_context(request: Request, body: Optional[dict[str, Any]] = None):
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        try:
            ensure_pma_docs(hub_root)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to ensure PMA docs: {exc}"
            ) from exc

        reset = False
        if isinstance(body, dict):
            reset = bool(body.get("reset", False))

        pma_dir = hub_root / ".codex-autorunner" / "pma"
        active_context_path = pma_dir / "active_context.md"
        context_log_path = pma_dir / "context_log.md"

        try:
            active_content = active_context_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read active_context.md: {exc}"
            ) from exc

        timestamp = now_iso()
        snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
        snapshot_content = snapshot_header + active_content
        snapshot_bytes = len(snapshot_content.encode("utf-8"))
        if snapshot_bytes > PMA_CONTEXT_SNAPSHOT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Snapshot too large "
                    f"(max {PMA_CONTEXT_SNAPSHOT_MAX_BYTES} bytes)"
                ),
            )

        try:
            with context_log_path.open("a", encoding="utf-8") as f:
                f.write(snapshot_content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to append context_log.md: {exc}"
            ) from exc

        if reset:
            try:
                atomic_write(active_context_path, pma_active_context_content())
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to reset active_context.md: {exc}"
                ) from exc

        line_count = len(active_content.splitlines())
        response: dict[str, Any] = {
            "status": "ok",
            "timestamp": timestamp,
            "active_context_line_count": line_count,
            "reset": reset,
        }
        try:
            context_log_bytes = context_log_path.stat().st_size
            response["context_log_bytes"] = context_log_bytes
            if context_log_bytes > PMA_CONTEXT_LOG_SOFT_LIMIT_BYTES:
                response["warning"] = (
                    "context_log.md is large "
                    f"({context_log_bytes} bytes); consider pruning"
                )
        except Exception:
            pass

        return response

    PMA_DOC_ORDER = (
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    )
    PMA_DOC_SET = set(PMA_DOC_ORDER)
    PMA_DOC_DEFAULTS = {
        "AGENTS.md": pma_agents_content,
        "active_context.md": pma_active_context_content,
        "context_log.md": pma_context_log_content,
        "ABOUT_CAR.md": pma_about_content,
        "prompt.md": pma_prompt_content,
    }

    @router.get("/docs/default/{name}")
    def get_pma_doc_default(name: str, request: Request) -> dict[str, str]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        content_fn = PMA_DOC_DEFAULTS.get(name)
        if content_fn is None:
            raise HTTPException(status_code=404, detail=f"Default not found: {name}")
        return {"name": name, "content": content_fn()}

    @router.get("/docs")
    def list_pma_docs(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        pma_dir = hub_root / ".codex-autorunner" / "pma"
        result: list[dict[str, Any]] = []
        for doc_name in PMA_DOC_ORDER:
            doc_path = pma_dir / doc_name
            entry: dict[str, Any] = {"name": doc_name}
            if doc_path.exists():
                entry["exists"] = True
                stat = doc_path.stat()
                entry["size"] = stat.st_size
                entry["mtime"] = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()
                if doc_name == "active_context.md":
                    try:
                        entry["line_count"] = len(
                            doc_path.read_text(encoding="utf-8").splitlines()
                        )
                    except Exception:
                        entry["line_count"] = 0
            else:
                entry["exists"] = False
            result.append(entry)
        return {
            "docs": result,
            "active_context_max_lines": int(
                pma_config.get("active_context_max_lines", 200)
            ),
        }

    @router.get("/docs/{name}")
    def get_pma_doc(name: str, request: Request) -> dict[str, str]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        hub_root = request.app.state.config.root
        pma_dir = hub_root / ".codex-autorunner" / "pma"
        doc_path = pma_dir / name
        if not doc_path.exists():
            raise HTTPException(status_code=404, detail=f"Doc not found: {name}")
        try:
            content = doc_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read doc: {exc}"
            ) from exc
        return {"name": name, "content": content}

    @router.put("/docs/{name}")
    def update_pma_doc(
        name: str, request: Request, body: dict[str, str]
    ) -> dict[str, str]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        content = body.get("content", "")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        MAX_DOC_SIZE = 500_000
        if len(content) > MAX_DOC_SIZE:
            raise HTTPException(
                status_code=413, detail=f"Content too large (max {MAX_DOC_SIZE} bytes)"
            )
        hub_root = request.app.state.config.root
        pma_dir = hub_root / ".codex-autorunner" / "pma"
        pma_dir.mkdir(parents=True, exist_ok=True)
        doc_path = pma_dir / name
        try:
            atomic_write(doc_path, content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to write doc: {exc}"
            ) from exc
        details = {
            "name": name,
            "size": len(content.encode("utf-8")),
            "source": "web",
        }
        if name == "active_context.md":
            details["line_count"] = len(content.splitlines())
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.DOC_UPDATED,
            details=details,
        )
        return {"name": name, "status": "ok"}

    return router


__all__ = ["build_pma_routes"]
