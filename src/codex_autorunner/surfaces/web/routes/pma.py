"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
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
    pma_doc_path,
    pma_docs_dir,
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
from ....core.pma_dispatches import (
    find_pma_dispatch_path,
    list_pma_dispatches,
    list_pma_dispatches_for_turn,
    resolve_pma_dispatch,
)
from ....core.pma_lane_worker import PmaLaneWorker
from ....core.pma_lifecycle import PmaLifecycleRouter
from ....core.pma_queue import PmaQueue, QueueItemState
from ....core.pma_safety import PmaSafetyChecker, PmaSafetyConfig
from ....core.pma_sink import PmaActiveSinkStore
from ....core.pma_state import PmaStateStore
from ....core.pma_transcripts import PmaTranscriptStore
from ....core.time_utils import now_iso
from ....core.utils import atomic_write
from ....integrations.pma_delivery import deliver_pma_output_to_active_sink
from ....integrations.telegram.adapter import chunk_message
from ....integrations.telegram.config import DEFAULT_STATE_FILE
from ....integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ....integrations.telegram.state import OutboxRecord, TelegramStateStore
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
    lane_workers: dict[str, PmaLaneWorker] = {}
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
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        if supervisor is not None:
            try:
                return supervisor.get_pma_safety_checker()
            except Exception:
                pass
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

    def _resolve_telegram_state_path(request: Request) -> Path:
        hub_root = request.app.state.config.root
        raw = getattr(request.app.state.config, "raw", {})
        telegram_cfg = raw.get("telegram_bot") if isinstance(raw, dict) else {}
        if not isinstance(telegram_cfg, dict):
            telegram_cfg = {}
        state_file = telegram_cfg.get("state_file")
        if not isinstance(state_file, str) or not state_file.strip():
            state_file = DEFAULT_STATE_FILE
        state_path = Path(state_file)
        if not state_path.is_absolute():
            state_path = (hub_root / state_path).resolve()
        return state_path

    async def _deliver_to_active_sink(
        *,
        request: Request,
        result: dict[str, Any],
        current: dict[str, Any],
        lifecycle_event: Optional[dict[str, Any]],
        turn_id: Optional[str] = None,
    ) -> None:
        if not lifecycle_event:
            return
        status = result.get("status") or "error"
        if status != "ok":
            return
        assistant_text = _resolve_transcript_text(result)
        if not assistant_text.strip():
            return

        hub_root = request.app.state.config.root
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = _resolve_transcript_turn_id(result, current)
        state_path = _resolve_telegram_state_path(request)
        await deliver_pma_output_to_active_sink(
            hub_root=hub_root,
            assistant_text=assistant_text,
            turn_id=turn_id,
            lifecycle_event=lifecycle_event,
            telegram_state_path=state_path,
        )

    async def _deliver_dispatches_to_active_sink(
        *,
        request: Request,
        turn_id: Optional[str],
    ) -> None:
        if not isinstance(turn_id, str) or not turn_id:
            return
        hub_root = request.app.state.config.root
        dispatches = list_pma_dispatches_for_turn(hub_root, turn_id)
        if not dispatches:
            return

        sink_store = PmaActiveSinkStore(hub_root)
        sink = sink_store.load()
        if not isinstance(sink, dict) or sink.get("kind") != "telegram":
            return

        chat_id = sink.get("chat_id")
        thread_id = sink.get("thread_id")
        if not isinstance(chat_id, int):
            return
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None

        state_path = _resolve_telegram_state_path(request)
        store = TelegramStateStore(state_path)
        try:
            for dispatch in dispatches:
                title = dispatch.title or "PMA dispatch"
                priority = dispatch.priority or "info"
                header = f"**PMA dispatch** ({priority})\n{title}"
                body = dispatch.body.strip()
                link_lines = []
                for link in dispatch.links:
                    label = link.get("label", "")
                    href = link.get("href", "")
                    if label and href:
                        link_lines.append(f"- {label}: {href}")
                details = "\n".join(
                    line for line in [body, "\n".join(link_lines)] if line
                ).strip()
                message = header
                if details:
                    message = f"{header}\n\n{details}"

                chunks = chunk_message(
                    message, max_len=TELEGRAM_MAX_MESSAGE_LENGTH, with_numbering=True
                )
                for idx, chunk in enumerate(chunks, 1):
                    record_id = f"pma-dispatch:{dispatch.dispatch_id}:{idx}"
                    record = OutboxRecord(
                        record_id=record_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        reply_to_message_id=None,
                        placeholder_message_id=None,
                        text=chunk,
                        created_at=now_iso(),
                        operation="send",
                        outbox_key=record_id,
                    )
                    await store.enqueue_outbox(record)
        except Exception:
            logger.exception("Failed to enqueue PMA dispatch to Telegram outbox")
        finally:
            await store.close()

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

    def _resolve_transcript_turn_id(
        result: dict[str, Any], current: dict[str, Any]
    ) -> str:
        for candidate in (
            result.get("turn_id"),
            current.get("turn_id"),
            current.get("client_turn_id"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return f"local-{uuid.uuid4()}"

    def _resolve_transcript_text(result: dict[str, Any]) -> str:
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message
        detail = result.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
        return ""

    def _build_transcript_metadata(
        *,
        result: dict[str, Any],
        current: dict[str, Any],
        prompt_message: Optional[str],
        lifecycle_event: Optional[dict[str, Any]],
        model: Optional[str],
        reasoning: Optional[str],
        duration_ms: Optional[int],
        finished_at: str,
    ) -> dict[str, Any]:
        trigger = "lifecycle_event" if lifecycle_event else "user_prompt"
        metadata: dict[str, Any] = {
            "status": result.get("status") or "error",
            "agent": current.get("agent"),
            "thread_id": result.get("thread_id") or current.get("thread_id"),
            "turn_id": _resolve_transcript_turn_id(result, current),
            "client_turn_id": current.get("client_turn_id") or "",
            "lane_id": current.get("lane_id") or "",
            "trigger": trigger,
            "model": model,
            "reasoning": reasoning,
            "started_at": current.get("started_at"),
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "user_prompt": prompt_message or "",
        }
        if lifecycle_event:
            metadata["lifecycle_event"] = dict(lifecycle_event)
            metadata["event_id"] = lifecycle_event.get("event_id")
            metadata["event_type"] = lifecycle_event.get("event_type")
            metadata["repo_id"] = lifecycle_event.get("repo_id")
            metadata["run_id"] = lifecycle_event.get("run_id")
            metadata["event_timestamp"] = lifecycle_event.get("timestamp")
        return metadata

    async def _persist_transcript(
        *,
        request: Request,
        result: dict[str, Any],
        current: dict[str, Any],
        prompt_message: Optional[str],
        lifecycle_event: Optional[dict[str, Any]],
        model: Optional[str],
        reasoning: Optional[str],
        duration_ms: Optional[int],
        finished_at: str,
    ) -> Optional[dict[str, Any]]:
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        assistant_text = _resolve_transcript_text(result)
        metadata = _build_transcript_metadata(
            result=result,
            current=current,
            prompt_message=prompt_message,
            lifecycle_event=lifecycle_event,
            model=model,
            reasoning=reasoning,
            duration_ms=duration_ms,
            finished_at=finished_at,
        )
        try:
            pointer = store.write_transcript(
                turn_id=metadata["turn_id"],
                metadata=metadata,
                assistant_text=assistant_text,
            )
        except Exception:
            logger.exception("Failed to write PMA transcript")
            return None
        return {
            "turn_id": pointer.turn_id,
            "metadata_path": pointer.metadata_path,
            "content_path": pointer.content_path,
            "created_at": pointer.created_at,
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
        prompt_message: Optional[str] = None,
        lifecycle_event: Optional[dict[str, Any]] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
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
        finished_at = now_iso()
        if started_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                duration_ms = int(
                    (datetime.now(timezone.utc) - start_dt).total_seconds() * 1000
                )
            except Exception:
                pass

        transcript_pointer = await _persist_transcript(
            request=request,
            result=result,
            current=current_snapshot,
            prompt_message=prompt_message,
            lifecycle_event=lifecycle_event,
            model=model,
            reasoning=reasoning,
            duration_ms=duration_ms,
            finished_at=finished_at,
        )
        if transcript_pointer is not None:
            pma_last_result = dict(pma_last_result or {})
            pma_last_result["transcript"] = transcript_pointer
            if not pma_last_result.get("turn_id"):
                pma_last_result["turn_id"] = transcript_pointer.get("turn_id")

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

        delivery_turn_id = None
        if isinstance(pma_last_result, dict):
            candidate = pma_last_result.get("turn_id")
            if isinstance(candidate, str) and candidate:
                delivery_turn_id = candidate
        await _deliver_to_active_sink(
            request=request,
            result=result,
            current=current_snapshot,
            lifecycle_event=lifecycle_event,
            turn_id=delivery_turn_id,
        )
        await _deliver_dispatches_to_active_sink(
            request=request,
            turn_id=delivery_turn_id,
        )
        _get_safety_checker(request).record_chat_result(
            agent=current_snapshot.get("agent") or "",
            status=status,
            error=result.get("detail") if status == "error" else None,
        )
        if lifecycle_event:
            _get_safety_checker(request).record_reactive_result(
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
        nonlocal lane_workers
        existing = lane_workers.get(lane_id)
        if existing is not None and existing.is_running:
            return

        async def _on_result(item, result: dict[str, Any]) -> None:
            result_future = item_futures.get(item.item_id)
            if result_future and not result_future.done():
                result_future.set_result(result)
            item_futures.pop(item.item_id, None)

        queue = _get_pma_queue(request)
        worker = PmaLaneWorker(
            lane_id,
            queue,
            lambda item: _execute_queue_item(item, request),
            log=logger,
            on_result=_on_result,
        )
        lane_workers[lane_id] = worker
        await worker.start()

    async def _stop_lane_worker(lane_id: str) -> None:
        worker = lane_workers.get(lane_id)
        if worker is None:
            return
        await worker.stop()
        lane_workers.pop(lane_id, None)

    class _AppRequest:
        def __init__(self, app: Any) -> None:
            self.app = app

    async def _ensure_lane_worker_for_app(app: Any, lane_id: str) -> None:
        await _ensure_lane_worker(lane_id, _AppRequest(app))

    async def _stop_lane_worker_for_app(app: Any, lane_id: str) -> None:
        _ = app
        await _stop_lane_worker(lane_id)

    async def _execute_queue_item(item: Any, request: Request) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        payload = item.payload

        client_turn_id = payload.get("client_turn_id")
        message = payload.get("message", "")
        agent = payload.get("agent")
        model = _normalize_optional_text(payload.get("model"))
        reasoning = _normalize_optional_text(payload.get("reasoning"))
        lifecycle_event = payload.get("lifecycle_event")
        if not isinstance(lifecycle_event, dict):
            lifecycle_event = None

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
                await _finalize_result(
                    error_result,
                    request=request,
                    store=store,
                    prompt_message=message,
                    lifecycle_event=lifecycle_event,
                    model=model,
                    reasoning=reasoning,
                )
            return error_result

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            result = {"status": "interrupted", "detail": "PMA chat interrupted"}
            if started:
                await _finalize_result(
                    result,
                    request=request,
                    store=store,
                    prompt_message=message,
                    lifecycle_event=lifecycle_event,
                    model=model,
                    reasoning=reasoning,
                )
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
                        await _finalize_result(
                            result,
                            request=request,
                            store=store,
                            prompt_message=message,
                            lifecycle_event=lifecycle_event,
                            model=model,
                            reasoning=reasoning,
                        )
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
                        await _finalize_result(
                            result,
                            request=request,
                            store=store,
                            prompt_message=message,
                            lifecycle_event=lifecycle_event,
                            model=model,
                            reasoning=reasoning,
                        )
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
                await _finalize_result(
                    error_result,
                    request=request,
                    store=store,
                    prompt_message=message,
                    lifecycle_event=lifecycle_event,
                    model=model,
                    reasoning=reasoning,
                )
            raise

        result = dict(result or {})
        result["client_turn_id"] = client_turn_id or ""
        await _finalize_result(
            result,
            request=request,
            store=store,
            prompt_message=message,
            lifecycle_event=lifecycle_event,
            model=model,
            reasoning=reasoning,
        )
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

    @router.get("/history")
    def list_pma_history(request: Request, limit: int = 50) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        entries = store.list_recent(limit=limit)
        return {"entries": entries}

    @router.get("/history/{turn_id}")
    def get_pma_history(turn_id: str, request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        transcript = store.read_transcript(turn_id)
        if not transcript:
            raise HTTPException(status_code=404, detail="Transcript not found")
        return transcript

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
                    f"message exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        hub_root = request.app.state.config.root
        try:
            PmaActiveSinkStore(hub_root).set_web()
        except Exception:
            logger.exception("Failed to update PMA active sink for web")
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

        await _stop_lane_worker(lane_id)

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
            "item_type": "pma_file",
            "next_action": "process_uploaded_file",
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

        docs_dir = _pma_docs_dir(hub_root)
        docs_dir.mkdir(parents=True, exist_ok=True)
        active_context_path = docs_dir / "active_context.md"
        if not active_context_path.exists():
            raise HTTPException(
                status_code=404, detail="Doc not found: active_context.md"
            )
        context_log_path = docs_dir / "context_log.md"

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
                    f"Snapshot too large (max {PMA_CONTEXT_SNAPSHOT_MAX_BYTES} bytes)"
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

    def _pma_docs_dir(hub_root: Path) -> Path:
        return pma_docs_dir(hub_root)

    def _normalize_doc_name(name: str) -> str:
        try:
            return sanitize_filename(name)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid doc name: {name}"
            ) from exc

    def _sorted_doc_names(docs_dir: Path) -> list[str]:
        names: set[str] = set()
        if docs_dir.exists():
            try:
                for path in docs_dir.iterdir():
                    if not path.is_file():
                        continue
                    if path.name.startswith("."):
                        continue
                    names.add(path.name)
            except OSError:
                pass
        ordered: list[str] = []
        for doc_name in PMA_DOC_ORDER:
            if doc_name in names:
                ordered.append(doc_name)
        remaining = sorted(name for name in names if name not in ordered)
        ordered.extend(remaining)
        return ordered

    def _write_doc_history(
        hub_root: Path, doc_name: str, content: str
    ) -> Optional[Path]:
        docs_dir = _pma_docs_dir(hub_root)
        history_root = docs_dir / "_history" / doc_name
        try:
            history_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history_path = history_root / f"{timestamp}.md"
            atomic_write(history_path, content)
            return history_path
        except Exception:
            logger.exception("Failed to write PMA doc history for %s", doc_name)
            return None

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
        try:
            ensure_pma_docs(hub_root)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to ensure PMA docs: {exc}"
            ) from exc
        docs_dir = _pma_docs_dir(hub_root)
        result: list[dict[str, Any]] = []
        for doc_name in _sorted_doc_names(docs_dir):
            doc_path = docs_dir / doc_name
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
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        doc_path = pma_doc_path(hub_root, name)
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
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
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
        docs_dir.mkdir(parents=True, exist_ok=True)
        doc_path = docs_dir / name
        try:
            atomic_write(doc_path, content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to write doc: {exc}"
            ) from exc
        _write_doc_history(hub_root, name, content)
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

    @router.get("/docs/history/{name}")
    def list_pma_doc_history(
        name: str, request: Request, limit: int = 50
    ) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
        history_dir = docs_dir / "_history" / name
        entries: list[dict[str, Any]] = []
        if history_dir.exists():
            try:
                for path in sorted(
                    (p for p in history_dir.iterdir() if p.is_file()),
                    key=lambda p: p.name,
                    reverse=True,
                ):
                    if len(entries) >= limit:
                        break
                    try:
                        stat = path.stat()
                        entries.append(
                            {
                                "id": path.name,
                                "size": stat.st_size,
                                "mtime": datetime.fromtimestamp(
                                    stat.st_mtime, tz=timezone.utc
                                ).isoformat(),
                            }
                        )
                    except OSError:
                        continue
            except OSError:
                pass
        return {"name": name, "entries": entries}

    @router.get("/docs/history/{name}/{version_id}")
    def get_pma_doc_history(
        name: str, version_id: str, request: Request
    ) -> dict[str, str]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        name = _normalize_doc_name(name)
        version_id = _normalize_doc_name(version_id)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
        history_path = docs_dir / "_history" / name / version_id
        if not history_path.exists():
            raise HTTPException(status_code=404, detail="History entry not found")
        try:
            content = history_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read history entry: {exc}"
            ) from exc
        return {"name": name, "version_id": version_id, "content": content}

    @router.get("/dispatches")
    def list_pma_dispatches_endpoint(
        request: Request, include_resolved: bool = False, limit: int = 100
    ) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        dispatches = list_pma_dispatches(
            hub_root, include_resolved=include_resolved, limit=limit
        )
        return {
            "items": [
                {
                    "id": item.dispatch_id,
                    "title": item.title,
                    "body": item.body,
                    "priority": item.priority,
                    "links": item.links,
                    "created_at": item.created_at,
                    "resolved_at": item.resolved_at,
                    "source_turn_id": item.source_turn_id,
                }
                for item in dispatches
            ]
        }

    @router.get("/dispatches/{dispatch_id}")
    def get_pma_dispatch(dispatch_id: str, request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        path = find_pma_dispatch_path(hub_root, dispatch_id)
        if not path:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        # Use list helper to normalize output
        items = list_pma_dispatches(hub_root, include_resolved=True)
        match = next((item for item in items if item.dispatch_id == dispatch_id), None)
        if not match:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        return {
            "dispatch": {
                "id": match.dispatch_id,
                "title": match.title,
                "body": match.body,
                "priority": match.priority,
                "links": match.links,
                "created_at": match.created_at,
                "resolved_at": match.resolved_at,
                "source_turn_id": match.source_turn_id,
            }
        }

    @router.post("/dispatches/{dispatch_id}/resolve")
    def resolve_pma_dispatch_endpoint(
        dispatch_id: str, request: Request
    ) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")
        hub_root = request.app.state.config.root
        path = find_pma_dispatch_path(hub_root, dispatch_id)
        if not path:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        dispatch, errors = resolve_pma_dispatch(path)
        if errors or dispatch is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to resolve dispatch: " + "; ".join(errors),
            )
        return {
            "dispatch": {
                "id": dispatch.dispatch_id,
                "title": dispatch.title,
                "body": dispatch.body,
                "priority": dispatch.priority,
                "links": dispatch.links,
                "created_at": dispatch.created_at,
                "resolved_at": dispatch.resolved_at,
                "source_turn_id": dispatch.source_turn_id,
            }
        }

    router._pma_start_lane_worker = _ensure_lane_worker_for_app
    router._pma_stop_lane_worker = _stop_lane_worker_for_app
    return router


__all__ = ["build_pma_routes"]
