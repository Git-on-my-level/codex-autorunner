"""
Hub-level PMA routes (chat + models + events).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
from ....core import filebox
from ....core.app_server_logging import AppServerEventFormatter
from ....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from ....core.logging_utils import log_event
from ....core.pma_audit import PmaActionType, PmaAuditLog
from ....core.pma_context import (
    PMA_MAX_TEXT,
    build_hub_snapshot,
    format_pma_discoverability_preamble,
    format_pma_prompt,
    get_active_context_auto_prune_meta,
    load_pma_prompt,
)
from ....core.pma_dispatches import (
    find_pma_dispatch_path,
    list_pma_dispatches,
    resolve_pma_dispatch,
)
from ....core.pma_lane_worker import PmaLaneWorker
from ....core.pma_lifecycle import PmaLifecycleRouter
from ....core.pma_queue import PmaQueue, QueueItemState
from ....core.pma_safety import PmaSafetyChecker, PmaSafetyConfig
from ....core.pma_state import PmaStateStore
from ....core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from ....core.pma_transcripts import PmaTranscriptStore
from ....core.redaction import redact_text
from ....core.state_roots import is_within_allowed_root
from ....core.time_utils import now_iso
from ....core.utils import atomic_write
from ....integrations.app_server.threads import PMA_KEY, PMA_OPENCODE_KEY
from ....integrations.discord.rendering import (
    chunk_discord_message,
    format_discord_message,
)
from ....integrations.discord.state import (
    DiscordStateStore,
)
from ....integrations.discord.state import (
    OutboxRecord as DiscordOutboxRecord,
)
from ....integrations.github.context_injection import maybe_inject_github_context
from ....integrations.telegram.state import (
    OutboxRecord as TelegramOutboxRecord,
)
from ....integrations.telegram.state import (
    TelegramStateStore,
    parse_topic_key,
    topic_key,
)
from ....manifest import load_manifest
from ..schemas import (
    PmaManagedThreadCompactRequest,
    PmaManagedThreadCreateRequest,
    PmaManagedThreadMessageRequest,
    PmaManagedThreadResumeRequest,
)
from ..services.pma.common import (
    build_idempotency_key as service_build_idempotency_key,
    normalize_optional_text as service_normalize_optional_text,
    pma_config_from_raw,
)
from .agents import _available_agents, _serialize_model_catalog
from .pma_routes import (
    build_automation_routes,
    build_managed_thread_crud_routes,
    build_managed_thread_tail_routes,
)
from .pma_routes.automation_adapter import (
    discover_automation_store_class as _discover_automation_store_class,
    first_callable as _first_callable,
    normalize_optional_text as _normalize_optional_text,
)
from .pma_routes.managed_threads import _truncate_text
from .pma_routes.publish import PMA_PUBLISH_RETRY_DELAYS_SECONDS
from .pma_routes.runtime_state import PmaRuntimeState
from .pma_routes.tail_stream import (
    coerce_dict as _coerce_dict,
    iso_from_event_ms as _iso_from_event_ms,
    normalize_tail_level as _normalize_tail_level,
    resolve_resume_after as _resolve_resume_after,
)
from .shared import SSE_HEADERS

logger = logging.getLogger(__name__)

_pma_runtime_state = PmaRuntimeState()

PMA_TIMEOUT_SECONDS = 7200
PMA_CONTEXT_SNAPSHOT_MAX_BYTES = 200_000
PMA_CONTEXT_LOG_SOFT_LIMIT_BYTES = 5_000_000
PMA_BULK_DELETE_SAMPLE_LIMIT = 10
# PMA_PUBLISH_RETRY_DELAYS_SECONDS imported from .pma_routes.publish
PMA_DISCORD_MESSAGE_MAX_LEN = 1900
MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"
MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR = "Failed to interrupt backend turn"
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def build_pma_routes() -> APIRouter:
    def _require_pma_enabled(request: Request) -> None:
        pma_config = _get_pma_config(request)
        if not pma_config.get("enabled", True):
            raise HTTPException(status_code=404, detail="PMA is disabled")

    router = APIRouter(prefix="/hub/pma", dependencies=[Depends(_require_pma_enabled)])
    pma_lock: Optional[asyncio.Lock] = None
    pma_lock_loop: Optional[asyncio.AbstractEventLoop] = None
    pma_event: Optional[asyncio.Event] = None
    pma_event_loop: Optional[asyncio.AbstractEventLoop] = None
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
    pma_automation_store: Optional[Any] = None
    pma_automation_root: Optional[Path] = None
    lane_workers: dict[str, PmaLaneWorker] = {}
    item_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # _normalize_optional_text imported from automation_adapter.py

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return pma_config_from_raw(raw)

    def _build_idempotency_key(
        *,
        lane_id: str,
        agent: Optional[str],
        model: Optional[str],
        reasoning: Optional[str],
        client_turn_id: Optional[str],
        message: str,
    ) -> str:
        return service_build_idempotency_key(
            lane_id=lane_id,
            agent=agent,
            model=model,
            reasoning=reasoning,
            client_turn_id=client_turn_id,
            message=message,
        )

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
                checker = supervisor.get_pma_safety_checker()
                if isinstance(checker, PmaSafetyChecker):
                    return checker
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

    async def _await_if_needed(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _call_with_fallbacks(
        method: Any, attempts: list[tuple[tuple[Any, ...], dict[str, Any]]]
    ) -> Any:
        last_type_error: Optional[TypeError] = None
        for args, kwargs in attempts:
            try:
                return await _await_if_needed(method(*args, **kwargs))
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        raise RuntimeError("No automation method call attempts were provided")

    # _first_callable imported from automation_adapter.py
    # _discover_automation_store_class imported from automation_adapter.py

    async def _call_store_create_with_payload(
        store: Any, method_names: tuple[str, ...], payload: dict[str, Any]
    ) -> Any:
        method = _first_callable(store, method_names)
        if method is None:
            raise HTTPException(status_code=503, detail="Automation action unavailable")
        return await _call_with_fallbacks(
            method,
            [
                ((payload,), {}),
                ((), {"payload": payload}),
                ((), dict(payload)),
            ],
        )

    async def _call_store_list(
        store: Any, method_names: tuple[str, ...], filters: dict[str, Any]
    ) -> Any:
        method = _first_callable(store, method_names)
        if method is None:
            raise HTTPException(status_code=503, detail="Automation action unavailable")
        return await _call_with_fallbacks(
            method,
            [
                ((), dict(filters)),
                ((dict(filters),), {}),
                ((), {}),
            ],
        )

    async def _call_store_action_with_id(
        store: Any,
        method_names: tuple[str, ...],
        item_id: str,
        payload: dict[str, Any],
        *,
        id_aliases: tuple[str, ...],
    ) -> Any:
        method = _first_callable(store, method_names)
        if method is None:
            raise HTTPException(status_code=503, detail="Automation action unavailable")
        item_kwargs: dict[str, Any] = {}
        for alias in id_aliases:
            item_kwargs[alias] = item_id
        merged_with_id = dict(payload)
        if id_aliases:
            merged_with_id[id_aliases[0]] = item_id
        return await _call_with_fallbacks(
            method,
            [
                ((item_id, dict(payload)), {}),
                ((item_id,), dict(payload)),
                ((item_id,), {"payload": dict(payload)}),
                ((), item_kwargs),
                ((), merged_with_id),
                ((item_id,), {}),
            ],
        )

    async def _get_automation_store(
        request: Request, *, required: bool = True
    ) -> Optional[Any]:
        nonlocal pma_automation_store, pma_automation_root

        hub_root = request.app.state.config.root
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        if supervisor is not None:
            for name in ("get_pma_automation_store", "get_automation_store"):
                accessor = getattr(supervisor, name, None)
                if not callable(accessor):
                    continue
                for args in ((), (hub_root,)):
                    try:
                        store = await _await_if_needed(accessor(*args))
                    except TypeError:
                        continue
                    except Exception:
                        logger.exception(
                            "Failed to resolve automation store from %s", name
                        )
                        break
                    if store is not None:
                        return store
                    break
            for name in ("pma_automation_store", "automation_store"):
                store = getattr(supervisor, name, None)
                if store is not None:
                    return store

        if pma_automation_store is not None and pma_automation_root == hub_root:
            return pma_automation_store

        klass = _discover_automation_store_class()
        if klass is not None:
            for args in ((hub_root,), ()):
                try:
                    store = klass(*args)
                except TypeError:
                    continue
                except Exception:
                    logger.exception("Failed to initialize automation store")
                    break
                pma_automation_store = store
                pma_automation_root = hub_root
                return store

        if required:
            raise HTTPException(
                status_code=503, detail="Hub automation store unavailable"
            )
        return None

    async def _notify_hub_automation_transition(
        request: Request,
        *,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        from_state: str,
        to_state: str,
        reason: Optional[str] = None,
        timestamp: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "from_state": (from_state or "").strip(),
            "to_state": (to_state or "").strip(),
            "reason": _normalize_optional_text(reason) or "",
            "timestamp": _normalize_optional_text(timestamp) or now_iso(),
        }
        normalized_repo_id = _normalize_optional_text(repo_id)
        normalized_run_id = _normalize_optional_text(run_id)
        normalized_thread_id = _normalize_optional_text(thread_id)
        if normalized_repo_id:
            payload["repo_id"] = normalized_repo_id
        if normalized_run_id:
            payload["run_id"] = normalized_run_id
        if normalized_thread_id:
            payload["thread_id"] = normalized_thread_id
        if isinstance(extra, dict):
            payload.update(extra)

        supervisor = getattr(request.app.state, "hub_supervisor", None)
        store = await _get_automation_store(request, required=False)
        if store is None:
            return

        method = _first_callable(
            store,
            (
                "notify_transition",
                "record_transition",
                "handle_transition",
                "on_transition",
                "process_transition",
            ),
        )
        if method is None:
            return
        try:
            await _call_with_fallbacks(
                method,
                [
                    ((dict(payload),), {}),
                    ((), {"payload": dict(payload)}),
                    ((), dict(payload)),
                ],
            )
        except Exception:
            logger.exception("Failed to notify hub automation transition")
            return

        process_now = (
            getattr(supervisor, "process_pma_automation_now", None)
            if supervisor is not None
            else None
        )
        if not callable(process_now):
            return
        try:
            await _await_if_needed(process_now(include_timers=False))
        except TypeError:
            try:
                await _await_if_needed(process_now())
            except Exception:
                logger.exception("Failed immediate PMA automation processing")
        except Exception:
            logger.exception("Failed immediate PMA automation processing")

    async def _notify_managed_thread_terminal_transition(
        request: Request,
        *,
        thread: dict[str, Any],
        managed_thread_id: str,
        managed_turn_id: str,
        to_state: str,
        reason: str,
    ) -> None:
        normalized_to_state = (to_state or "").strip().lower() or "failed"
        await _notify_hub_automation_transition(
            request,
            repo_id=_normalize_optional_text(thread.get("repo_id")),
            run_id=None,
            thread_id=managed_thread_id,
            from_state="running",
            to_state=normalized_to_state,
            reason=reason,
            extra={
                "event_type": f"managed_thread_{normalized_to_state}",
                "transition_id": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
                "idempotency_key": (
                    f"managed_turn:{managed_turn_id}:{normalized_to_state}"
                ),
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "agent": _normalize_optional_text(thread.get("agent")) or "",
            },
        )

    def _is_within_root(path: Path, root: Path) -> bool:
        return is_within_allowed_root(path, allowed_roots=[root], resolve=True)

    def _normalize_workspace_root_input(workspace_root: str) -> PurePosixPath:
        cleaned = (workspace_root or "").strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="workspace_root is invalid")
        if "\\" in cleaned or "\x00" in cleaned or _DRIVE_PREFIX_RE.match(cleaned):
            raise HTTPException(status_code=400, detail="workspace_root is invalid")
        normalized = PurePosixPath(cleaned)
        if ".." in normalized.parts:
            raise HTTPException(status_code=400, detail="workspace_root is invalid")
        return normalized

    def _resolve_workspace_from_repo_id(request: Request, repo_id: str) -> Path:
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        if supervisor is None:
            raise HTTPException(status_code=500, detail="Hub supervisor unavailable")
        snapshots = supervisor.list_repos()
        for snapshot in snapshots:
            if getattr(snapshot, "id", None) == repo_id:
                repo_path = getattr(snapshot, "path", None)
                if isinstance(repo_path, str):
                    repo_path = Path(repo_path)
                if not isinstance(repo_path, Path):
                    continue
                return repo_path.absolute()
        raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")

    def _resolve_workspace_from_input(hub_root: Path, workspace_root: str) -> Path:
        normalized = _normalize_workspace_root_input(workspace_root)
        hub_root_resolved = hub_root.absolute()
        workspace = Path(normalized)
        if not workspace.is_absolute():
            workspace = (hub_root_resolved / workspace).absolute()
        else:
            workspace = workspace.absolute()
        if not _is_within_root(workspace, hub_root):
            raise HTTPException(
                status_code=400,
                detail="workspace_root is invalid",
            )
        return workspace

    def _resolve_managed_thread_workspace(hub_root: Path, workspace_root: Any) -> Path:
        raw_workspace = _normalize_optional_text(workspace_root)
        if raw_workspace is None:
            raise HTTPException(
                status_code=500, detail="Managed thread has invalid workspace_root"
            )
        resolved_workspace = Path(raw_workspace).absolute()
        if not _is_within_root(resolved_workspace, hub_root):
            raise HTTPException(
                status_code=400, detail="Managed thread workspace_root is invalid"
            )
        return resolved_workspace

    def _sanitize_managed_thread_result_error(detail: Any) -> str:
        sanitized = _normalize_optional_text(detail)
        if sanitized in {"PMA chat timed out", "PMA chat interrupted"}:
            return sanitized
        return MANAGED_THREAD_PUBLIC_EXECUTION_ERROR

    def _normalize_notify_on(value: Any) -> Optional[str]:
        normalized = _normalize_optional_text(value)
        if normalized is None:
            return None
        notify_on = normalized.lower()
        if notify_on != "terminal":
            raise HTTPException(
                status_code=400, detail="notify_on must be 'terminal' when provided"
            )
        return notify_on

    def _build_terminal_notify_subscription_payload(
        *,
        managed_thread_id: str,
        lane_id: Optional[str],
        notify_once: bool,
        idempotency_key: Optional[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_types": ["managed_thread_completed", "managed_thread_failed"],
            "thread_id": managed_thread_id,
            "lane_id": lane_id,
            "notify_once": notify_once,
            "metadata": {"notify_once": notify_once},
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return payload

    async def _register_managed_thread_terminal_notify(
        request: Request,
        *,
        managed_thread_id: str,
        lane_id: Optional[str],
        notify_once: bool,
        idempotency_key: Optional[str],
    ) -> Optional[dict[str, Any]]:
        store = await _get_automation_store(request, required=False)
        if store is None:
            return None
        created = await _call_store_create_with_payload(
            store,
            (
                "create_subscription",
                "add_subscription",
                "upsert_subscription",
            ),
            _build_terminal_notify_subscription_payload(
                managed_thread_id=managed_thread_id,
                lane_id=lane_id,
                notify_once=notify_once,
                idempotency_key=idempotency_key,
            ),
        )
        if isinstance(created, dict) and "subscription" in created:
            return created
        return {"subscription": created}

    # _coerce_dict imported from tail_stream.py

    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _parse_tail_duration_seconds(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        raw = value.strip().lower()
        if not raw:
            raise HTTPException(status_code=400, detail="since must not be empty")
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        total_seconds = 0
        idx = 0
        size = len(raw)
        while idx < size:
            start = idx
            while idx < size and raw[idx].isdigit():
                idx += 1
            if start == idx or idx >= size:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                        "or combined 1h30m."
                    ),
                )
            amount_text = raw[start:idx]
            # Keep duration parsing bounded and predictable.
            if len(amount_text) > 9:
                raise HTTPException(
                    status_code=400, detail="since duration component is too large"
                )
            unit = raw[idx]
            multiplier = multipliers.get(unit)
            if multiplier is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                        "or combined 1h30m."
                    ),
                )
            idx += 1
            total_seconds += int(amount_text) * multiplier
        if total_seconds <= 0:
            raise HTTPException(status_code=400, detail="since must be > 0")
        return total_seconds

    def _since_ms_from_duration(value: Optional[str]) -> Optional[int]:
        seconds = _parse_tail_duration_seconds(value)
        if seconds is None:
            return None
        return int((datetime.now(timezone.utc).timestamp() - seconds) * 1000)

    # _normalize_tail_level imported from tail_stream.py
    # _resolve_resume_after imported from tail_stream.py
    # _iso_from_event_ms imported from tail_stream.py

    def _redact_nested(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {str(k): _redact_nested(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_redact_nested(item) for item in value]
        return value

    def _tail_event_type_from_message(message: Any) -> str:
        payload = _coerce_dict(message)
        method = str(payload.get("method") or "").strip().lower()
        params = _coerce_dict(payload.get("params"))
        item = _coerce_dict(params.get("item"))

        if method == "turn/completed":
            status = str(params.get("status") or "").strip().lower()
            if status in {"interrupt", "interrupted"}:
                return "turn_interrupted"
            if status in {"error", "failed"}:
                return "turn_failed"
            return "turn_completed"
        if method == "error":
            return "turn_failed"
        if method in {
            "item/commandexecution/requestapproval",
            "item/filechange/requestapproval",
        }:
            return "tool_started"
        if method == "item/completed":
            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"commandexecution", "filechange", "tool"}:
                exit_code = item.get("exitCode")
                if isinstance(exit_code, int) and exit_code != 0:
                    return "tool_failed"
                return "tool_completed"
        if "reasoning" in method:
            return "assistant_update"
        return "progress"

    def _serialize_tail_event(
        event: dict[str, Any],
        *,
        level: str,
        formatter: AppServerEventFormatter,
        since_ms: Optional[int],
    ) -> Optional[dict[str, Any]]:
        event_id = int(event.get("id") or 0)
        if event_id <= 0:
            return None
        received_at_ms = int(event.get("received_at") or 0)
        if since_ms is not None and received_at_ms and received_at_ms < since_ms:
            return None
        message = _coerce_dict(event.get("message"))
        lines = [
            redact_text(str(line).strip())
            for line in formatter.format_event(message)
            if isinstance(line, str) and line.strip()
        ]
        if not lines:
            fallback = str(message.get("method") or "").strip()
            if fallback:
                lines = [fallback]
        summary = _truncate_text(lines[0], 220) if lines else ""
        payload: dict[str, Any] = {
            "event_id": event_id,
            "event_type": _tail_event_type_from_message(message),
            "summary": summary,
            "lines": lines[:8],
            "received_at_ms": received_at_ms if received_at_ms > 0 else None,
            "received_at": _iso_from_event_ms(received_at_ms),
        }
        if level == "debug":
            payload["raw"] = _redact_nested(message)
        return payload

    def _compose_compacted_prompt(compact_seed: str, message: str) -> str:
        return (
            "Context summary (from compaction):\n"
            f"{compact_seed}\n\n"
            "User message:\n"
            f"{message}"
        )

    def _resolve_chat_state_path(
        request: Request, *, section: str, default_state_file: str
    ) -> Path:
        hub_root = request.app.state.config.root
        raw = getattr(request.app.state.config, "raw", {})
        section_cfg = raw.get(section) if isinstance(raw, dict) else {}
        if not isinstance(section_cfg, dict):
            section_cfg = {}
        state_file = section_cfg.get("state_file")
        if not isinstance(state_file, str) or not state_file.strip():
            state_file = default_state_file
        state_path = Path(state_file)
        if not state_path.is_absolute():
            state_path = (hub_root / state_path).resolve()
        return state_path

    def _normalize_workspace_path_for_repo_lookup(
        value: Any, *, hub_root: Path
    ) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = hub_root / candidate
        try:
            return str(candidate.resolve())
        except Exception:
            return str(candidate.absolute())

    def _repo_id_by_workspace_path(request: Request) -> dict[str, str]:
        hub_root = request.app.state.config.root
        manifest_path = request.app.state.config.manifest_path
        if not manifest_path.exists():
            return {}
        try:
            manifest = load_manifest(manifest_path, hub_root)
        except Exception:
            logger.exception("Failed loading manifest for PMA publish target lookup")
            return {}
        mapping: dict[str, str] = {}
        for repo in manifest.repos:
            try:
                workspace_path = (hub_root / repo.path).resolve()
            except Exception:
                continue
            mapping[str(workspace_path)] = repo.id
        return mapping

    def _resolve_binding_repo_id(
        *,
        repo_id: Any,
        workspace_path: Any,
        repo_id_by_workspace: dict[str, str],
        hub_root: Path,
    ) -> Optional[str]:
        normalized_repo_id = _normalize_optional_text(repo_id)
        if normalized_repo_id:
            return normalized_repo_id
        normalized_workspace = _normalize_workspace_path_for_repo_lookup(
            workspace_path, hub_root=hub_root
        )
        if normalized_workspace:
            mapped_repo = repo_id_by_workspace.get(normalized_workspace)
            if isinstance(mapped_repo, str) and mapped_repo.strip():
                return mapped_repo.strip()
        return None

    def _resolve_publish_repo_id(
        *,
        request: Request,
        lifecycle_event: Optional[dict[str, Any]],
        wake_up: Optional[dict[str, Any]],
    ) -> Optional[str]:
        for candidate in (
            lifecycle_event.get("repo_id") if lifecycle_event else None,
            wake_up.get("repo_id") if wake_up else None,
        ):
            normalized = _normalize_optional_text(candidate)
            if normalized:
                return normalized

        thread_id = (
            _normalize_optional_text(wake_up.get("thread_id"))
            if isinstance(wake_up, dict)
            else None
        )
        if not thread_id:
            return None
        try:
            thread = PmaThreadStore(request.app.state.config.root).get_thread(thread_id)
        except Exception:
            logger.exception(
                "Failed resolving managed thread repo for publish thread_id=%s",
                thread_id,
            )
            return None
        if not isinstance(thread, dict):
            return None
        return _normalize_optional_text(thread.get("repo_id"))

    def _build_publish_correlation_id(
        *,
        result: dict[str, Any],
        client_turn_id: Optional[str],
        wake_up: Optional[dict[str, Any]],
    ) -> str:
        for candidate in (
            client_turn_id,
            result.get("client_turn_id"),
            result.get("turn_id"),
            wake_up.get("wakeup_id") if isinstance(wake_up, dict) else None,
        ):
            normalized = _normalize_optional_text(candidate)
            if normalized:
                return normalized
        return f"pma-{uuid.uuid4().hex[:12]}"

    def _build_publish_message(
        *,
        result: dict[str, Any],
        lifecycle_event: Optional[dict[str, Any]],
        wake_up: Optional[dict[str, Any]],
        correlation_id: str,
    ) -> str:
        trigger = (
            _normalize_optional_text(lifecycle_event.get("event_type"))
            if isinstance(lifecycle_event, dict)
            else None
        )
        if not trigger and isinstance(wake_up, dict):
            trigger = _normalize_optional_text(wake_up.get("event_type")) or (
                _normalize_optional_text(wake_up.get("source")) or "automation"
            )
        trigger = trigger or "automation"

        repo_id = (
            _normalize_optional_text(lifecycle_event.get("repo_id"))
            if isinstance(lifecycle_event, dict)
            else None
        )
        if not repo_id and isinstance(wake_up, dict):
            repo_id = _normalize_optional_text(wake_up.get("repo_id"))

        run_id = (
            _normalize_optional_text(lifecycle_event.get("run_id"))
            if isinstance(lifecycle_event, dict)
            else None
        )
        if not run_id and isinstance(wake_up, dict):
            run_id = _normalize_optional_text(wake_up.get("run_id"))

        thread_id = (
            _normalize_optional_text(wake_up.get("thread_id"))
            if isinstance(wake_up, dict)
            else None
        )
        status = _normalize_optional_text(result.get("status")) or "error"
        detail = _normalize_optional_text(result.get("detail"))
        output = _normalize_optional_text(result.get("message"))

        lines: list[str] = [f"PMA update ({trigger})"]
        if repo_id:
            lines.append(f"repo_id: {repo_id}")
        if run_id:
            lines.append(f"run_id: {run_id}")
        if thread_id:
            lines.append(f"thread_id: {thread_id}")
        lines.append(f"correlation_id: {correlation_id}")
        lines.append("")

        if status == "ok":
            lines.append(output or "Turn completed with no assistant output.")
        else:
            lines.append(f"status: {status}")
            lines.append(f"error: {detail or 'Turn failed without detail.'}")
            lines.append(
                "next_action: run /pma status and inspect PMA history if needed."
            )
        return "\n".join(lines).strip()

    def _delivery_status_from_counts(
        *,
        published: int,
        duplicates: int,
        failed: int,
        targets: int,
    ) -> str:
        if targets <= 0:
            return "skipped"
        if published > 0 and failed <= 0:
            return "success"
        if published > 0 and failed > 0:
            return "partial_success"
        if published <= 0 and failed <= 0 and duplicates > 0:
            return "duplicate_only"
        if failed > 0:
            return "failed"
        return "skipped"

    async def _enqueue_with_retry(
        enqueue_call: Any,
    ) -> None:
        last_error: Optional[Exception] = None
        for delay_seconds in PMA_PUBLISH_RETRY_DELAYS_SECONDS:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            try:
                await enqueue_call()
                return
            except Exception as exc:  # pragma: no cover - guarded by caller assertions
                last_error = exc
        if last_error is not None:
            raise last_error

    async def _publish_automation_result(
        *,
        request: Request,
        result: dict[str, Any],
        client_turn_id: Optional[str],
        lifecycle_event: Optional[dict[str, Any]],
        wake_up: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        correlation_id = _build_publish_correlation_id(
            result=result,
            client_turn_id=client_turn_id,
            wake_up=wake_up,
        )
        target_repo_id = _resolve_publish_repo_id(
            request=request,
            lifecycle_event=lifecycle_event,
            wake_up=wake_up,
        )
        repo_id_by_workspace = _repo_id_by_workspace_path(request)
        message = _build_publish_message(
            result=result,
            lifecycle_event=lifecycle_event,
            wake_up=wake_up,
            correlation_id=correlation_id,
        )

        published = 0
        duplicates = 0
        failed = 0
        targets = 0
        errors: list[str] = []

        discord_store: Optional[DiscordStateStore] = None
        telegram_store: Optional[TelegramStateStore] = None
        try:
            discord_state_path = _resolve_chat_state_path(
                request,
                section="discord_bot",
                default_state_file=DISCORD_STATE_FILE_DEFAULT,
            )
            if discord_state_path.exists():
                discord_store = DiscordStateStore(discord_state_path)
                bindings = await discord_store.list_bindings()
                seen_channels: set[str] = set()
                for binding in bindings:
                    if not bool(binding.get("pma_enabled")):
                        continue
                    channel_id = _normalize_optional_text(binding.get("channel_id"))
                    if not channel_id or channel_id in seen_channels:
                        continue
                    binding_repo_id = _resolve_binding_repo_id(
                        repo_id=binding.get("repo_id"),
                        workspace_path=binding.get("workspace_path"),
                        repo_id_by_workspace=repo_id_by_workspace,
                        hub_root=hub_root,
                    )
                    prev_repo_id = _normalize_optional_text(
                        binding.get("pma_prev_repo_id")
                    )
                    if target_repo_id and target_repo_id not in {
                        binding_repo_id,
                        prev_repo_id,
                    }:
                        continue
                    seen_channels.add(channel_id)
                    targets += 1
                    chunks = chunk_discord_message(
                        format_discord_message(message),
                        max_len=PMA_DISCORD_MESSAGE_MAX_LEN,
                        with_numbering=False,
                    )
                    if not chunks:
                        chunks = [format_discord_message(message)]
                    for index, chunk in enumerate(chunks, start=1):
                        digest = hashlib.sha256(
                            f"{correlation_id}:discord:{channel_id}:{index}".encode(
                                "utf-8"
                            )
                        ).hexdigest()[:24]
                        record_id = f"pma:{digest}"
                        existing = await discord_store.get_outbox(record_id)
                        if existing is not None:
                            duplicates += 1
                            continue
                        record = DiscordOutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                            created_at=now_iso(),
                        )
                        try:
                            await _enqueue_with_retry(
                                lambda record=record: discord_store.enqueue_outbox(
                                    record
                                )
                            )
                        except Exception as exc:
                            failed += 1
                            errors.append(f"discord:{channel_id}:{exc}")
                            logger.warning(
                                "Failed to enqueue PMA discord publish: channel_id=%s correlation_id=%s error=%s",
                                channel_id,
                                correlation_id,
                                exc,
                            )
                        else:
                            published += 1

            telegram_state_path = _resolve_chat_state_path(
                request,
                section="telegram_bot",
                default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
            )
            if telegram_state_path.exists():
                telegram_store = TelegramStateStore(telegram_state_path)
                topics = await telegram_store.list_topics()
                seen_topics: set[tuple[int, Optional[int]]] = set()
                for key, topic in topics.items():
                    if not bool(getattr(topic, "pma_enabled", False)):
                        continue
                    try:
                        chat_id, thread_id, scope = parse_topic_key(key)
                    except Exception:
                        continue
                    base_key = topic_key(chat_id, thread_id)
                    current_scope = await telegram_store.get_topic_scope(base_key)
                    if scope != current_scope:
                        continue
                    identity = (chat_id, thread_id)
                    if identity in seen_topics:
                        continue
                    binding_repo_id = _resolve_binding_repo_id(
                        repo_id=getattr(topic, "repo_id", None),
                        workspace_path=getattr(topic, "workspace_path", None),
                        repo_id_by_workspace=repo_id_by_workspace,
                        hub_root=hub_root,
                    )
                    prev_repo_id = _normalize_optional_text(
                        getattr(topic, "pma_prev_repo_id", None)
                    )
                    if target_repo_id and target_repo_id not in {
                        binding_repo_id,
                        prev_repo_id,
                    }:
                        continue
                    seen_topics.add(identity)
                    targets += 1
                    digest = hashlib.sha256(
                        f"{correlation_id}:telegram:{chat_id}:{thread_id or 'root'}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:24]
                    record_id = f"pma:{digest}"
                    outbox_key = (
                        f"pma:{correlation_id}:{chat_id}:{thread_id or 'root'}:send"
                    )
                    existing = await telegram_store.get_outbox(record_id)
                    if existing is not None:
                        duplicates += 1
                        continue
                    record = TelegramOutboxRecord(
                        record_id=record_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        reply_to_message_id=None,
                        placeholder_message_id=None,
                        text=message,
                        created_at=now_iso(),
                        operation="send",
                        message_id=None,
                        outbox_key=outbox_key,
                    )
                    try:
                        await _enqueue_with_retry(
                            lambda record=record: telegram_store.enqueue_outbox(record)
                        )
                    except Exception as exc:
                        failed += 1
                        errors.append(f"telegram:{chat_id}:{thread_id or 'root'}:{exc}")
                        logger.warning(
                            "Failed to enqueue PMA telegram publish: chat_id=%s thread_id=%s correlation_id=%s error=%s",
                            chat_id,
                            thread_id,
                            correlation_id,
                            exc,
                        )
                    else:
                        published += 1
        finally:
            if discord_store is not None:
                try:
                    await discord_store.close()
                except Exception:
                    logger.exception("Failed to close discord publish store")
            if telegram_store is not None:
                try:
                    await telegram_store.close()
                except Exception:
                    logger.exception("Failed to close telegram publish store")

        delivery_status = _delivery_status_from_counts(
            published=published,
            duplicates=duplicates,
            failed=failed,
            targets=targets,
        )
        delivery_outcome = {
            "published": published,
            "duplicates": duplicates,
            "failed": failed,
            "targets": targets,
            "repo_id": target_repo_id,
            "correlation_id": correlation_id,
            "errors": errors[:5],
        }
        log_event(
            logger,
            logging.INFO,
            "pma.turn.publish",
            delivery_status=delivery_status,
            targets=targets,
            published=published,
            duplicates=duplicates,
            failed=failed,
            repo_id=target_repo_id,
            correlation_id=correlation_id,
        )
        return {
            "delivery_status": delivery_status,
            "delivery_outcome": delivery_outcome,
        }

    async def _get_pma_lock() -> asyncio.Lock:
        nonlocal pma_lock, pma_lock_loop, pma_event, pma_event_loop
        loop = asyncio.get_running_loop()
        lock = pma_lock
        if (
            lock is None
            or pma_lock_loop is None
            or pma_lock_loop is not loop
            or pma_lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            pma_lock = lock
            pma_lock_loop = loop
            pma_event = None
            pma_event_loop = None
        return lock

    async def _persist_state(store: Optional[PmaStateStore]) -> None:
        if store is None:
            return
        async with await _get_pma_lock():
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

    async def _append_text_file(path: Path, content: str) -> None:
        def _append() -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)

        await asyncio.to_thread(_append)

    async def _atomic_write_async(path: Path, content: str) -> None:
        await asyncio.to_thread(atomic_write, path, content)

    def _consume_task_result(task: asyncio.Task[Any], *, name: str) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("PMA task failed: %s", name)

    def _cancel_background_task(task: asyncio.Task[Any], *, name: str) -> None:
        if task.done():
            _consume_task_result(task, name=name)
            return

        def _on_done(done_task: asyncio.Future[Any]) -> None:
            if isinstance(done_task, asyncio.Task):
                _consume_task_result(done_task, name=name)

        task.add_done_callback(_on_done)
        task.cancel()

    # _truncate_text imported from managed_threads.py

    def _format_last_result(
        result: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, Any]:
        status = result.get("status") or "error"
        message = result.get("message")
        detail = result.get("detail")
        text = message if isinstance(message, str) and message else detail
        summary = _truncate_text(text or "", PMA_MAX_TEXT)
        payload = {
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
        delivery_status = _normalize_optional_text(result.get("delivery_status"))
        if delivery_status:
            payload["delivery_status"] = delivery_status
        delivery_outcome = result.get("delivery_outcome")
        if isinstance(delivery_outcome, dict):
            payload["delivery_outcome"] = dict(delivery_outcome)
        return payload

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
        nonlocal pma_event, pma_event_loop
        async with await _get_pma_lock():
            loop = asyncio.get_running_loop()
            if (
                pma_event is None
                or pma_event.is_set()
                or pma_event_loop is None
                or pma_event_loop is not loop
                or pma_event_loop.is_closed()
            ):
                pma_event = asyncio.Event()
                pma_event_loop = loop
            return pma_event

    async def _set_active(
        active: bool, *, store: Optional[PmaStateStore] = None
    ) -> None:
        nonlocal pma_active
        async with await _get_pma_lock():
            pma_active = active
        await _persist_state(store)

    async def _begin_turn(
        client_turn_id: Optional[str],
        *,
        store: Optional[PmaStateStore] = None,
        lane_id: Optional[str] = None,
    ) -> bool:
        nonlocal pma_active, pma_current
        async with await _get_pma_lock():
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
        nonlocal pma_event, pma_event_loop
        async with await _get_pma_lock():
            pma_event = None
            pma_event_loop = None

    async def _update_current(
        *, store: Optional[PmaStateStore] = None, **updates: Any
    ) -> None:
        nonlocal pma_current
        async with await _get_pma_lock():
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
        nonlocal pma_current, pma_last_result, pma_active, pma_event, pma_event_loop
        async with await _get_pma_lock():
            current_snapshot = dict(pma_current or {})
            pma_last_result = _format_last_result(result or {}, current_snapshot)
            pma_current = None
            pma_active = False
            pma_event = None
            pma_event_loop = None

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
        async with await _get_pma_lock():
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
                opencode_harness = OpenCodeHarness(supervisor)
                await opencode_harness.interrupt(hub_root, thread_id, turn_id)
        else:
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            events = getattr(request.app.state, "app_server_events", None)
            if supervisor is not None and events is not None and thread_id and turn_id:
                codex_harness = CodexHarness(supervisor, events)
                try:
                    await codex_harness.interrupt(hub_root, thread_id, turn_id)
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

    async def _ensure_lane_worker(lane_id: str, request: Any) -> None:
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

    async def _stop_all_lane_workers_for_app(app: Any) -> None:
        _ = app
        for lane_id in list(lane_workers.keys()):
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
        wake_up = payload.get("wake_up")
        if not isinstance(wake_up, dict):
            wake_up = None
        automation_trigger = lifecycle_event is not None or wake_up is not None

        store = _get_state_store(request)
        agents, available_default = _available_agents(request)
        available_ids = {entry.get("id") for entry in agents if isinstance(entry, dict)}
        defaults = _get_pma_config(request)
        started = False

        async def _finalize_queue_result_payload(
            result_payload: dict[str, Any],
            *,
            persist: bool = True,
        ) -> dict[str, Any]:
            payload_result = dict(result_payload or {})
            if automation_trigger:
                try:
                    payload_result.update(
                        await _publish_automation_result(
                            request=request,
                            result=payload_result,
                            client_turn_id=(
                                _normalize_optional_text(client_turn_id) or None
                            ),
                            lifecycle_event=lifecycle_event,
                            wake_up=wake_up,
                        )
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed publishing PMA automation result: client_turn_id=%s",
                        client_turn_id,
                    )
                    payload_result["delivery_status"] = "failed"
                    payload_result["delivery_outcome"] = {
                        "published": 0,
                        "duplicates": 0,
                        "failed": 1,
                        "targets": 0,
                        "repo_id": None,
                        "correlation_id": (
                            _normalize_optional_text(client_turn_id)
                            or f"pma-{uuid.uuid4().hex[:12]}"
                        ),
                        "errors": [str(exc)],
                    }
            if persist and started:
                await _finalize_result(
                    payload_result,
                    request=request,
                    store=store,
                    prompt_message=message,
                    lifecycle_event=lifecycle_event,
                    model=model,
                    reasoning=reasoning,
                )
            return payload_result

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
            return await _finalize_queue_result_payload(
                {"status": "error", "detail": detail},
                persist=False,
            )

        started = await _begin_turn(
            client_turn_id, store=store, lane_id=getattr(item, "lane_id", None)
        )
        if not started:
            detail = "Another PMA turn is already active; queue item was not started"
            logger.warning("PMA queue item rejected: %s", detail)
            return await _finalize_queue_result_payload(
                {
                    "status": "error",
                    "detail": detail,
                    "client_turn_id": client_turn_id or "",
                },
                persist=False,
            )

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
            prompt, _ = await maybe_inject_github_context(
                prompt_text=prompt,
                link_source_text=message,
                workspace_root=hub_root,
                logger=logger,
                event_prefix="web.pma.github_context",
                allow_cross_repo=True,
            )
        except Exception as exc:
            error_result = {
                "status": "error",
                "detail": str(exc),
                "client_turn_id": client_turn_id or "",
            }
            return await _finalize_queue_result_payload(error_result)

        interrupt_event = await _get_interrupt_event()
        if interrupt_event.is_set():
            result = {"status": "interrupted", "detail": "PMA chat interrupted"}
            return await _finalize_queue_result_payload(result)

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
                    return await _finalize_queue_result_payload(result)
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
                    return await _finalize_queue_result_payload(result)
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
            error_result = {
                "status": "error",
                "detail": str(exc),
                "client_turn_id": client_turn_id or "",
            }
            await _finalize_queue_result_payload(error_result)
            raise

        result = dict(result or {})
        result["client_turn_id"] = client_turn_id or ""
        return await _finalize_queue_result_payload(result)

    @router.get("/active")
    async def pma_active_status(
        request: Request, client_turn_id: Optional[str] = None
    ) -> dict[str, Any]:
        async with await _get_pma_lock():
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
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        entries = store.list_recent(limit=limit)
        return {"entries": entries}

    @router.get("/history/{turn_id}")
    def get_pma_history(turn_id: str, request: Request) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        transcript = store.read_transcript(turn_id)
        if not transcript:
            raise HTTPException(status_code=404, detail="Transcript not found")
        return transcript

    @router.post("/threads")
    async def create_managed_thread(
        request: Request, payload: PmaManagedThreadCreateRequest
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        repo_id = _normalize_optional_text(payload.repo_id)
        workspace_root = _normalize_optional_text(payload.workspace_root)
        raw_payload: dict[str, Any] = {}
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                raw_payload = parsed
        except Exception:
            raw_payload = {}
        notify_on = _normalize_notify_on(
            raw_payload.get("notify_on") or raw_payload.get("notifyOn")
        )
        notify_lane = _normalize_optional_text(
            raw_payload.get("notify_lane") or raw_payload.get("notifyLane")
        )
        raw_notify_once = raw_payload.get("notify_once")
        if raw_notify_once is None:
            raw_notify_once = raw_payload.get("notifyOnce")
        notify_once = bool(raw_notify_once) if raw_notify_once is not None else True

        if bool(repo_id) == bool(workspace_root):
            raise HTTPException(
                status_code=400,
                detail="Exactly one of repo_id or workspace_root is required",
            )

        resolved_repo_id: Optional[str] = None
        if repo_id:
            resolved_workspace = _resolve_workspace_from_repo_id(request, repo_id)
            resolved_repo_id = repo_id
            if not _is_within_root(resolved_workspace, hub_root):
                raise HTTPException(
                    status_code=400,
                    detail="Resolved repo path is invalid",
                )
        else:
            if workspace_root is None:
                raise HTTPException(
                    status_code=400,
                    detail="workspace_root is required when repo_id is omitted",
                )
            resolved_workspace = _resolve_workspace_from_input(hub_root, workspace_root)

        store = PmaThreadStore(hub_root)
        thread = store.create_thread(
            payload.agent,
            resolved_workspace,
            repo_id=resolved_repo_id,
            name=_normalize_optional_text(payload.name),
            backend_thread_id=_normalize_optional_text(payload.backend_thread_id),
        )
        notification: Optional[dict[str, Any]] = None
        if notify_on == "terminal":
            notification = await _register_managed_thread_terminal_notify(
                request,
                managed_thread_id=str(thread.get("managed_thread_id") or ""),
                lane_id=notify_lane,
                notify_once=notify_once,
                idempotency_key=(
                    f"managed-thread-notify:{thread.get('managed_thread_id')}"
                    if notify_once
                    else None
                ),
            )
        response: dict[str, Any] = {"thread": thread}
        if notification is not None:
            response["notification"] = notification
        return response

    @router.get("/threads")
    def list_managed_threads(
        request: Request,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        store = PmaThreadStore(request.app.state.config.root)
        threads = store.list_threads(
            agent=_normalize_optional_text(agent),
            status=_normalize_optional_text(status),
            repo_id=_normalize_optional_text(repo_id),
            limit=limit,
        )
        return {"threads": threads}

    @router.post("/threads/{managed_thread_id}/compact")
    def compact_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadCompactRequest,
    ) -> dict[str, Any]:
        summary = (payload.summary or "").strip()
        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")
        pma_config = _get_pma_config(request)
        max_text_chars = int(pma_config.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(summary) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"summary exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = _normalize_optional_text(
            thread.get("backend_thread_id")
        )
        reset_backend = bool(payload.reset_backend)
        store.set_thread_compact_seed(
            managed_thread_id,
            summary,
            reset_backend_id=reset_backend,
        )
        store.append_action(
            "managed_thread_compact",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "old_backend_thread_id": old_backend_thread_id,
                    "summary_length": len(summary),
                    "reset_backend": reset_backend,
                },
                ensure_ascii=True,
            ),
        )
        updated = store.get_thread(managed_thread_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return {"thread": updated}

    @router.post("/threads/{managed_thread_id}/resume")
    def resume_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadResumeRequest,
    ) -> dict[str, Any]:
        backend_thread_id = (payload.backend_thread_id or "").strip()
        if not backend_thread_id:
            raise HTTPException(status_code=400, detail="backend_thread_id is required")

        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = _normalize_optional_text(
            thread.get("backend_thread_id")
        )
        old_status = _normalize_optional_text(thread.get("status"))
        store.set_thread_backend_id(managed_thread_id, backend_thread_id)
        store.activate_thread(managed_thread_id)
        store.append_action(
            "managed_thread_resume",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "old_backend_thread_id": old_backend_thread_id,
                    "backend_thread_id": backend_thread_id,
                    "old_status": old_status,
                },
                ensure_ascii=True,
            ),
        )
        updated = store.get_thread(managed_thread_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return {"thread": updated}

    @router.post("/threads/{managed_thread_id}/archive")
    def archive_managed_thread(
        managed_thread_id: str, request: Request
    ) -> dict[str, Any]:
        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_status = _normalize_optional_text(thread.get("status"))
        store.archive_thread(managed_thread_id)
        store.append_action(
            "managed_thread_archive",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {"old_status": old_status},
                ensure_ascii=True,
            ),
        )
        updated = store.get_thread(managed_thread_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return {"thread": updated}

    async def _build_managed_thread_tail_snapshot(
        *,
        request: Request,
        managed_thread_id: str,
        limit: int,
        level: str,
        since_ms: Optional[int],
        resume_after: Optional[int],
    ) -> dict[str, Any]:
        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        running_turn = store.get_running_turn(managed_thread_id)
        turn = running_turn
        if turn is None:
            turns = store.list_turns(managed_thread_id, limit=1)
            turn = turns[0] if turns else None
        if turn is None:
            return {
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": None,
                "agent": thread.get("agent"),
                "turn_status": None,
                "lifecycle_events": [],
                "events": [],
                "last_event_id": int(resume_after or 0),
                "elapsed_seconds": None,
                "idle_seconds": None,
                "activity": "idle",
                "stream_available": False,
            }

        managed_turn_id = str(turn.get("managed_turn_id") or "")
        turn_status = str(turn.get("status") or "").strip().lower()
        started_at = _normalize_optional_text(turn.get("started_at"))
        finished_at = _normalize_optional_text(turn.get("finished_at"))
        started_dt = _parse_iso_datetime(started_at)
        finished_dt = _parse_iso_datetime(finished_at)
        now_dt = datetime.now(timezone.utc)
        effective_finished = finished_dt or (
            None if turn_status == "running" else now_dt
        )
        elapsed_seconds: Optional[int] = None
        if started_dt is not None:
            end_dt = effective_finished or now_dt
            elapsed_seconds = max(0, int((end_dt - started_dt).total_seconds()))

        lifecycle_events = ["turn_started"]
        if turn_status == "ok":
            lifecycle_events.append("turn_completed")
        elif turn_status == "error":
            lifecycle_events.append("turn_failed")
        elif turn_status == "interrupted":
            lifecycle_events.append("turn_interrupted")

        backend_thread_id = _normalize_optional_text(thread.get("backend_thread_id"))
        backend_turn_id = _normalize_optional_text(turn.get("backend_turn_id"))
        app_server_events = getattr(request.app.state, "app_server_events", None)
        can_stream_codex = (
            str(thread.get("agent") or "").strip().lower() == "codex"
            and app_server_events is not None
            and bool(backend_thread_id)
            and bool(backend_turn_id)
        )
        formatter = AppServerEventFormatter(redact_enabled=True)
        tail_events: list[dict[str, Any]] = []
        if can_stream_codex:
            raw_events = await app_server_events.list_events(
                str(backend_thread_id),
                str(backend_turn_id),
                after_id=int(resume_after or 0),
                limit=limit,
            )
            for event in raw_events:
                serialized = _serialize_tail_event(
                    event, level=level, formatter=formatter, since_ms=since_ms
                )
                if serialized is None:
                    continue
                tail_events.append(serialized)

        last_event_id = int(resume_after or 0)
        if tail_events:
            last_event_id = int(tail_events[-1].get("event_id") or last_event_id)
        last_event_ms = None
        if tail_events:
            last_event_ms = tail_events[-1].get("received_at_ms")
        idle_seconds: Optional[int] = None
        if turn_status == "running":
            if isinstance(last_event_ms, (int, float)) and last_event_ms > 0:
                idle_seconds = max(
                    0, int((now_dt.timestamp() * 1000 - last_event_ms) / 1000)
                )
            elif started_dt is not None:
                idle_seconds = max(0, int((now_dt - started_dt).total_seconds()))

        activity = "idle"
        if turn_status == "running":
            if idle_seconds is None:
                activity = "running"
            elif idle_seconds >= 30:
                activity = "stalled"
            else:
                activity = "running"
        elif turn_status == "ok":
            activity = "completed"
        elif turn_status in {"error", "interrupted"}:
            activity = "failed"

        return {
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "agent": thread.get("agent"),
            "backend_thread_id": backend_thread_id,
            "backend_turn_id": backend_turn_id,
            "turn_status": turn_status,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed_seconds,
            "idle_seconds": idle_seconds,
            "activity": activity,
            "lifecycle_events": lifecycle_events,
            "events": tail_events,
            "last_event_id": last_event_id,
            "last_event_at": (
                tail_events[-1].get("received_at") if tail_events else None
            ),
            "stream_available": bool(can_stream_codex),
        }

    @router.get("/threads/{managed_thread_id}/status")
    async def get_managed_thread_status(
        managed_thread_id: str,
        request: Request,
        limit: int = 20,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_limit = min(limit, 200)
        normalized_level = _normalize_tail_level(level)
        resume_after = _resolve_resume_after(request, since_event_id)
        since_ms = _since_ms_from_duration(since)
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            managed_thread_id=managed_thread_id,
            limit=normalized_limit,
            level=normalized_level,
            since_ms=since_ms,
            resume_after=resume_after,
        )
        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        running_turn = store.get_running_turn(managed_thread_id)
        turn = running_turn
        if turn is None:
            turns = store.list_turns(managed_thread_id, limit=1)
            turn = turns[0] if turns else None

        turn_status = str(snapshot.get("turn_status") or "")
        is_alive = bool(
            (thread.get("status") or "") == "active" and turn_status == "running"
        )
        latest_output_excerpt = ""
        if isinstance(turn, dict):
            latest_output_excerpt = _truncate_text(
                turn.get("assistant_text") or "", 240
            )

        return {
            "managed_thread_id": managed_thread_id,
            "thread": thread,
            "is_alive": is_alive,
            "turn": {
                "managed_turn_id": snapshot.get("managed_turn_id"),
                "status": snapshot.get("turn_status"),
                "activity": snapshot.get("activity"),
                "elapsed_seconds": snapshot.get("elapsed_seconds"),
                "idle_seconds": snapshot.get("idle_seconds"),
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
                "lifecycle_events": snapshot.get("lifecycle_events"),
            },
            "recent_progress": snapshot.get("events") or [],
            "latest_output_excerpt": latest_output_excerpt,
            "stream_available": bool(snapshot.get("stream_available")),
        }

    @router.get("/threads/{managed_thread_id}/tail")
    async def get_managed_thread_tail(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_limit = min(limit, 200)
        normalized_level = _normalize_tail_level(level)
        resume_after = _resolve_resume_after(request, since_event_id)
        since_ms = _since_ms_from_duration(since)
        return await _build_managed_thread_tail_snapshot(
            request=request,
            managed_thread_id=managed_thread_id,
            limit=normalized_limit,
            level=normalized_level,
            since_ms=since_ms,
            resume_after=resume_after,
        )

    @router.get("/threads/{managed_thread_id}/tail/events")
    async def stream_managed_thread_tail(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ):
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_limit = min(limit, 200)
        normalized_level = _normalize_tail_level(level)
        resume_after = _resolve_resume_after(request, since_event_id)
        since_ms = _since_ms_from_duration(since)
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            managed_thread_id=managed_thread_id,
            limit=normalized_limit,
            level=normalized_level,
            since_ms=since_ms,
            resume_after=resume_after,
        )

        async def _stream() -> Any:
            yield f"event: state\ndata: {json.dumps(snapshot, ensure_ascii=True)}\n\n"
            for event in snapshot.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("event_id")
                event_id_line = (
                    f"id: {event_id}\n"
                    if isinstance(event_id, int) and event_id > 0
                    else ""
                )
                yield (
                    f"event: tail\n"
                    f"{event_id_line}"
                    f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
                )

            if snapshot.get("turn_status") != "running":
                return
            if not snapshot.get("stream_available"):
                store = PmaThreadStore(request.app.state.config.root)
                while True:
                    await asyncio.sleep(10.0)
                    turn = store.get_turn(
                        managed_thread_id, str(snapshot.get("managed_turn_id") or "")
                    )
                    status = str((turn or {}).get("status") or "").strip().lower()
                    if status != "running":
                        yield (
                            "event: state\ndata: "
                            f"{json.dumps({'turn_status': status or 'unknown'}, ensure_ascii=True)}\n\n"
                        )
                        return
                    now = datetime.now(timezone.utc)
                    started_dt = _parse_iso_datetime(snapshot.get("started_at"))
                    elapsed = None
                    if started_dt is not None:
                        elapsed = max(0, int((now - started_dt).total_seconds()))
                    progress_payload = {
                        "managed_thread_id": managed_thread_id,
                        "managed_turn_id": snapshot.get("managed_turn_id"),
                        "turn_status": "running",
                        "elapsed_seconds": elapsed,
                    }
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps(progress_payload, ensure_ascii=True)}\n\n"
                    )

            app_server_events = getattr(request.app.state, "app_server_events", None)
            if app_server_events is None:
                return
            backend_thread_id = str(snapshot.get("backend_thread_id") or "")
            backend_turn_id = str(snapshot.get("backend_turn_id") or "")
            if not backend_thread_id or not backend_turn_id:
                return

            formatter = AppServerEventFormatter(redact_enabled=True)
            store = PmaThreadStore(request.app.state.config.root)
            last_event_id = int(snapshot.get("last_event_id") or 0)
            async for entry in app_server_events.stream_entries(
                backend_thread_id,
                backend_turn_id,
                after_id=last_event_id,
                heartbeat_interval=10.0,
            ):
                if entry is None:
                    turn = store.get_turn(
                        managed_thread_id, str(snapshot.get("managed_turn_id") or "")
                    )
                    status = str((turn or {}).get("status") or "").strip().lower()
                    now = datetime.now(timezone.utc)
                    started_dt = _parse_iso_datetime(snapshot.get("started_at"))
                    elapsed = None
                    if started_dt is not None:
                        elapsed = max(0, int((now - started_dt).total_seconds()))
                    idle = None
                    if snapshot.get("last_event_at"):
                        last_event_dt = _parse_iso_datetime(
                            snapshot.get("last_event_at")
                        )
                        if last_event_dt is not None:
                            idle = max(0, int((now - last_event_dt).total_seconds()))
                    progress_payload = {
                        "managed_thread_id": managed_thread_id,
                        "managed_turn_id": snapshot.get("managed_turn_id"),
                        "turn_status": status or "running",
                        "elapsed_seconds": elapsed,
                        "idle_seconds": idle,
                    }
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps(progress_payload, ensure_ascii=True)}\n\n"
                    )
                    if status != "running":
                        return
                    continue

                serialized = _serialize_tail_event(
                    entry,
                    level=normalized_level,
                    formatter=formatter,
                    since_ms=since_ms,
                )
                if serialized is None:
                    continue
                event_id = int(serialized.get("event_id") or 0)
                if event_id > 0:
                    last_event_id = event_id
                snapshot["last_event_at"] = serialized.get("received_at")
                yield (
                    "event: tail\n"
                    f"id: {event_id}\n"
                    f"data: {json.dumps(serialized, ensure_ascii=True)}\n\n"
                )

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.post("/threads/{managed_thread_id}/messages")
    async def send_managed_thread_message(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadMessageRequest,
    ) -> Any:
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        defaults = _get_pma_config(request)
        max_text_chars = int(defaults.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(message) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"message exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        hub_root = request.app.state.config.root
        thread_store = PmaThreadStore(hub_root)
        transcripts = PmaTranscriptStore(hub_root)
        thread = thread_store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        notify_on = _normalize_notify_on(payload.notify_on)
        notify_lane = _normalize_optional_text(payload.notify_lane)
        notify_once = bool(payload.notify_once)
        defer_execution = bool(payload.defer_execution)

        if (thread.get("status") or "") == "archived":
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_archived",
                    "detail": "Managed thread is archived and read-only",
                    "next_step": "Use `car pma thread resume` or spawn a new thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": _normalize_optional_text(
                        thread.get("backend_thread_id")
                    )
                    or "",
                    "assistant_text": "",
                    "error": "Managed thread is archived and read-only",
                },
            )
        model = _normalize_optional_text(payload.model) or defaults.get("model")
        reasoning = _normalize_optional_text(payload.reasoning) or defaults.get(
            "reasoning"
        )
        stored_backend_id = _normalize_optional_text(thread.get("backend_thread_id"))
        known_backend_thread_id = stored_backend_id
        compact_seed = _normalize_optional_text(thread.get("compact_seed"))
        execution_message = message
        if not stored_backend_id and compact_seed:
            execution_message = _compose_compacted_prompt(compact_seed, message)
        execution_prompt = (
            f"{format_pma_discoverability_preamble(hub_root=hub_root)}"
            "<user_message>\n"
            f"{execution_message}\n"
            "</user_message>\n"
        )
        try:
            turn = thread_store.create_turn(
                managed_thread_id,
                prompt=message,
                model=model,
                reasoning=reasoning,
            )
        except ManagedThreadNotActiveError as exc:
            if exc.status == "archived":
                detail = "Managed thread is archived and read-only"
            else:
                detail = "Managed thread is not active"
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_not_active",
                    "detail": detail,
                    "next_step": "Resume the thread or create a new active thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": known_backend_thread_id or "",
                    "assistant_text": "",
                    "error": detail,
                },
            )
        except ManagedThreadAlreadyHasRunningTurnError:
            running_turn = thread_store.get_running_turn(managed_thread_id)
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "already_in_flight",
                    "reason": "running_turn_exists",
                    "detail": (
                        f"Managed thread {managed_thread_id} already has a running turn"
                    ),
                    "next_step": (
                        "Wait for the running turn to finish, use --watch, "
                        "or subscribe with --notify-on terminal."
                    ),
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": str(
                        (running_turn or {}).get("managed_turn_id") or ""
                    )
                    or None,
                    "backend_thread_id": known_backend_thread_id or "",
                    "assistant_text": "",
                    "error": "Managed thread already has a running turn",
                },
            )
        managed_turn_id = str(turn.get("managed_turn_id") or "")
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Failed to create managed turn")

        notification: Optional[dict[str, Any]] = None
        if notify_on == "terminal":
            notification = await _register_managed_thread_terminal_notify(
                request,
                managed_thread_id=managed_thread_id,
                lane_id=notify_lane,
                notify_once=notify_once,
                idempotency_key=(
                    f"managed-thread-send-notify:{managed_turn_id}"
                    if notify_once
                    else None
                ),
            )

        preview = _truncate_text(message, 120)
        workspace_root = _resolve_managed_thread_workspace(
            hub_root, thread.get("workspace_root")
        )
        agent = str(thread.get("agent") or "").strip().lower()
        interrupt_event = asyncio.Event()

        async def _finalize_error(
            detail: str, *, backend_turn_id: Optional[str] = None
        ) -> dict[str, Any]:
            thread_store.mark_turn_finished(
                managed_turn_id,
                status="error",
                assistant_text="",
                error=detail,
                backend_turn_id=backend_turn_id,
                transcript_turn_id=None,
            )
            await _notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="failed",
                reason=detail,
            )
            return {
                "status": "error",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": known_backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
            }

        async def _on_managed_turn_meta(
            backend_thread_id: Optional[str],
            backend_turn_id: Optional[str],
        ) -> None:
            nonlocal known_backend_thread_id
            resolved_backend_turn_id = _normalize_optional_text(backend_turn_id)
            if resolved_backend_turn_id:
                thread_store.set_turn_backend_turn_id(
                    managed_turn_id, resolved_backend_turn_id
                )
            resolved_backend_thread_id = _normalize_optional_text(backend_thread_id)
            if resolved_backend_thread_id != known_backend_thread_id:
                thread_store.set_thread_backend_id(
                    managed_thread_id, resolved_backend_thread_id
                )
                known_backend_thread_id = resolved_backend_thread_id

        async def _run_turn() -> dict[str, Any]:
            nonlocal known_backend_thread_id
            try:
                if agent == "opencode":
                    supervisor = getattr(request.app.state, "opencode_supervisor", None)
                    if supervisor is None:
                        return await _finalize_error("OpenCode unavailable")
                    stall_timeout_seconds = None
                    try:
                        stall_timeout_seconds = (
                            request.app.state.config.opencode.session_stall_timeout_seconds
                        )
                    except Exception:
                        stall_timeout_seconds = None
                    result = await _execute_opencode(
                        supervisor,
                        workspace_root,
                        execution_prompt,
                        interrupt_event,
                        model=model,
                        reasoning=reasoning,
                        backend_session_id=stored_backend_id,
                        stall_timeout_seconds=stall_timeout_seconds,
                        on_meta=_on_managed_turn_meta,
                    )
                elif agent == "codex":
                    supervisor = getattr(
                        request.app.state, "app_server_supervisor", None
                    )
                    events = getattr(request.app.state, "app_server_events", None)
                    if supervisor is None or events is None:
                        return await _finalize_error("App-server unavailable")
                    result = await _execute_app_server(
                        supervisor,
                        events,
                        workspace_root,
                        execution_prompt,
                        interrupt_event,
                        model=model,
                        reasoning=reasoning,
                        backend_thread_id=stored_backend_id,
                        on_meta=_on_managed_turn_meta,
                    )
                else:
                    return await _finalize_error(
                        f"Unknown managed thread agent: {agent}"
                    )
            except HTTPException:
                logger.exception(
                    "Managed thread execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                return await _finalize_error(MANAGED_THREAD_PUBLIC_EXECUTION_ERROR)
            except Exception:
                logger.exception(
                    "Managed thread execution raised unexpected error (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                return await _finalize_error(MANAGED_THREAD_PUBLIC_EXECUTION_ERROR)

            result = dict(result or {})
            if str(result.get("status") or "") != "ok":
                detail = _sanitize_managed_thread_result_error(result.get("detail"))
                backend_turn_id = _normalize_optional_text(result.get("turn_id"))
                return await _finalize_error(detail, backend_turn_id=backend_turn_id)

            assistant_text = str(result.get("message") or "")
            backend_turn_id = _normalize_optional_text(result.get("turn_id"))
            backend_thread_id = _normalize_optional_text(
                result.get("backend_thread_id") or result.get("thread_id")
            )
            if backend_thread_id != known_backend_thread_id:
                thread_store.set_thread_backend_id(managed_thread_id, backend_thread_id)
                known_backend_thread_id = backend_thread_id

            transcript_metadata = {
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "repo_id": thread.get("repo_id"),
                "workspace_root": str(workspace_root),
                "agent": agent,
                "backend_thread_id": backend_thread_id,
                "backend_turn_id": backend_turn_id,
                "model": model,
                "reasoning": reasoning,
                "status": "ok",
            }
            transcript_turn_id: Optional[str] = None
            try:
                transcripts.write_transcript(
                    turn_id=managed_turn_id,
                    metadata=transcript_metadata,
                    assistant_text=assistant_text,
                )
                transcript_turn_id = managed_turn_id
            except Exception:
                logger.exception(
                    "Failed to persist managed-thread transcript (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )

            thread_store.mark_turn_finished(
                managed_turn_id,
                status="ok",
                assistant_text=assistant_text,
                error=None,
                backend_turn_id=backend_turn_id,
                transcript_turn_id=transcript_turn_id,
            )
            finalized_turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
            finalized_status = str((finalized_turn or {}).get("status") or "").strip()
            if finalized_status != "ok":
                detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                response_status = "error"
                if finalized_status == "interrupted":
                    detail = "PMA chat interrupted"
                    response_status = "interrupted"
                elif finalized_status == "error":
                    detail = _sanitize_managed_thread_result_error(
                        (finalized_turn or {}).get("error")
                    )
                await _notify_managed_thread_terminal_transition(
                    request,
                    thread=thread,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    to_state="failed",
                    reason=detail,
                )
                return {
                    "status": response_status,
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": backend_thread_id or "",
                    "assistant_text": "",
                    "error": detail,
                }
            thread_store.update_thread_after_turn(
                managed_thread_id,
                last_turn_id=managed_turn_id,
                last_message_preview=preview,
            )
            await _notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="completed",
                reason="managed_turn_completed",
            )
            return {
                "status": "ok",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": backend_thread_id or "",
                "assistant_text": assistant_text,
                "error": None,
            }

        accepted_payload: dict[str, Any] = {
            "status": "ok",
            "send_state": "accepted",
            "execution_state": "running",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": known_backend_thread_id or "",
            "assistant_text": "",
            "error": None,
        }
        if notification is not None:
            accepted_payload["notification"] = notification

        if defer_execution:
            task_pool = getattr(request.app.state, "pma_managed_thread_tasks", None)
            if not isinstance(task_pool, set):
                task_pool = set()
                request.app.state.pma_managed_thread_tasks = task_pool

            async def _background_run() -> None:
                try:
                    await _run_turn()
                except Exception:
                    logger.exception(
                        "Managed-thread background execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
                    if (
                        str((turn or {}).get("status") or "").strip().lower()
                        == "running"
                    ):
                        detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                        thread_store.mark_turn_finished(
                            managed_turn_id,
                            status="error",
                            assistant_text="",
                            error=detail,
                            backend_turn_id=None,
                            transcript_turn_id=None,
                        )
                        await _notify_managed_thread_terminal_transition(
                            request,
                            thread=thread,
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=managed_turn_id,
                            to_state="failed",
                            reason=detail,
                        )

            task = asyncio.create_task(_background_run())
            task_pool.add(task)
            task.add_done_callback(lambda done: task_pool.discard(done))
            return accepted_payload

        response = await _run_turn()
        response["send_state"] = "accepted"
        response["execution_state"] = "completed"
        if notification is not None:
            response["notification"] = notification
        return response

    @router.post("/threads/{managed_thread_id}/interrupt")
    async def interrupt_managed_thread(
        managed_thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        store = PmaThreadStore(hub_root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        running_turn = store.get_running_turn(managed_thread_id)
        if running_turn is None:
            raise HTTPException(
                status_code=409, detail="Managed thread has no running turn"
            )
        managed_turn_id = str(running_turn.get("managed_turn_id") or "")
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Running turn is missing id")

        agent = str(thread.get("agent") or "").strip().lower()
        backend_thread_id = _normalize_optional_text(thread.get("backend_thread_id"))
        backend_turn_id = _normalize_optional_text(running_turn.get("backend_turn_id"))
        backend_error: Optional[str] = None
        backend_interrupt_attempted = False

        if agent == "codex":
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            if supervisor is None:
                backend_error = "App-server unavailable"
            elif not backend_thread_id or not backend_turn_id:
                backend_error = (
                    "Codex interrupt requires backend_thread_id and backend_turn_id"
                )
            else:
                backend_interrupt_attempted = True
                try:
                    client = await supervisor.get_client(hub_root)
                    await client.turn_interrupt(
                        backend_turn_id, thread_id=backend_thread_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to interrupt Codex managed-thread turn (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
        elif agent == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                backend_error = "OpenCode unavailable"
            elif not backend_thread_id:
                backend_error = "OpenCode interrupt requires backend_thread_id"
            else:
                backend_interrupt_attempted = True
                try:
                    client = await supervisor.get_client(hub_root)
                    await client.abort(backend_thread_id)
                except Exception:
                    logger.exception(
                        "Failed to interrupt OpenCode managed-thread turn (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
        else:
            backend_error = f"Unknown managed thread agent: {agent}"

        store.mark_turn_interrupted(managed_turn_id)
        await _notify_managed_thread_terminal_transition(
            request,
            thread=thread,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="failed",
            reason=backend_error or "managed_turn_interrupted",
        )
        store.append_action(
            "managed_thread_interrupt",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "agent": agent,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": backend_thread_id,
                    "backend_turn_id": backend_turn_id,
                    "backend_interrupt_attempted": backend_interrupt_attempted,
                    "backend_error": backend_error,
                },
                ensure_ascii=True,
            ),
        )
        updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
        return {
            "status": "ok",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "turn": updated_turn,
            "backend_error": backend_error,
        }

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

    @router.get("/audit/recent")
    def get_pma_audit_log(request: Request, limit: int = 100):
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
        safety_checker = _get_safety_checker(request)
        return safety_checker.get_stats()

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
        events: Any,
        hub_root: Path,
        prompt: str,
        interrupt_event: asyncio.Event,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        thread_registry: Optional[Any] = None,
        thread_key: Optional[str] = None,
        on_meta: Optional[Any] = None,
    ) -> dict[str, Any]:
        client = await supervisor.get_client(hub_root)

        if backend_thread_id:
            thread_id = backend_thread_id
        elif thread_registry is not None and thread_key:
            thread_id = thread_registry.get_thread_id(thread_key)
        else:
            thread_id = None
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
                _cancel_background_task(turn_task, name="pma.app_server.turn.wait")
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                try:
                    await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
                _cancel_background_task(turn_task, name="pma.app_server.turn.wait")
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
            turn_result = await turn_task
        finally:
            _cancel_background_task(timeout_task, name="pma.app_server.timeout.wait")
            _cancel_background_task(
                interrupt_task, name="pma.app_server.interrupt.wait"
            )

        if getattr(turn_result, "errors", None):
            errors = turn_result.errors
            raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
        raw_events = getattr(turn_result, "raw_events", []) or []
        return {
            "status": "ok",
            "message": output,
            "thread_id": thread_id,
            "backend_thread_id": thread_id,
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
        backend_session_id: Optional[str] = None,
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
        session_id = backend_session_id
        if session_id is None and thread_registry is not None and thread_key:
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
                _cancel_background_task(output_task, name="pma.opencode.output.collect")
                await opencode_harness.interrupt(hub_root, session_id, None)
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                _cancel_background_task(output_task, name="pma.opencode.output.collect")
                await opencode_harness.interrupt(hub_root, session_id, None)
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                _cancel_background_task(output_task, name="pma.opencode.output.collect")
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
            _cancel_background_task(timeout_task, name="pma.opencode.timeout.wait")
            _cancel_background_task(interrupt_task, name="pma.opencode.interrupt.wait")
            await supervisor.mark_turn_finished(hub_root)

        if output_result.error:
            raise HTTPException(status_code=502, detail=output_result.error)
        return {
            "status": "ok",
            "message": output_result.text,
            "thread_id": session_id,
            "backend_thread_id": session_id,
            "turn_id": build_turn_id(session_id),
        }

    @router.post("/chat")
    async def pma_chat(request: Request):
        pma_config = _get_pma_config(request)
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
        return await _interrupt_active(
            request, reason="PMA chat interrupted", source="user_request"
        )

    @router.post("/stop")
    async def pma_stop(request: Request) -> dict[str, Any]:
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
        turn_id: str,
        request: Request,
        thread_id: str,
        agent: str = "codex",
        since_event_id: Optional[int] = None,
    ):
        agent_id = (agent or "").strip().lower()
        resume_after = _resolve_resume_after(request, since_event_id)
        if agent_id == "codex":
            events = getattr(request.app.state, "app_server_events", None)
            if events is None:
                raise HTTPException(status_code=404, detail="Codex events unavailable")
            if not thread_id:
                raise HTTPException(status_code=400, detail="thread_id is required")
            return StreamingResponse(
                events.stream(thread_id, turn_id, after_id=(resume_after or 0)),
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
        entry: filebox.FileBoxEntry, *, request: Request
    ) -> dict[str, Any]:
        base = request.scope.get("root_path", "") or ""
        box = entry.box
        filename = entry.name
        download = f"{base}/hub/pma/files/{box}/{filename}"
        return {
            "item_type": "pma_file",
            "next_action": "process_uploaded_file",
            "name": filename,
            "box": box,
            "size": entry.size,
            "modified_at": entry.modified_at,
            "source": entry.source,
            "url": download,
        }

    @router.get("/files")
    async def list_pma_files(request: Request) -> dict[str, list[dict[str, Any]]]:
        hub_root = request.app.state.config.root
        result: dict[str, list[dict[str, Any]]] = {"inbox": [], "outbox": []}
        async with await _get_pma_lock():
            listing = await asyncio.to_thread(
                filebox.list_filebox, hub_root, include_legacy=True
            )
        for box in ("inbox", "outbox"):
            entries = listing.get(box, [])
            result[box] = [
                _serialize_pma_entry(entry, request=request)
                for entry in sorted(entries, key=lambda item: item.name)
            ]
        return result

    @router.get("/queue")
    async def pma_queue_status(request: Request) -> dict[str, Any]:
        queue = _get_pma_queue(request)
        summary = await queue.get_queue_summary()
        return summary

    @router.get("/queue/{lane_id:path}")
    async def pma_lane_queue_status(request: Request, lane_id: str) -> dict[str, Any]:
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
                async with await _get_pma_lock():
                    target_path = await asyncio.to_thread(
                        filebox.save_file, hub_root, box, filename, content
                    )
                saved.append(target_path.name)
                _get_safety_checker(request).record_action(
                    action_type=PmaActionType.FILE_UPLOADED,
                    details={
                        "box": box,
                        "filename": target_path.name,
                        "size": len(content),
                    },
                )
            except ValueError as exc:
                logger.warning("Invalid PMA upload target: %s (%s)", filename, exc)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.warning("Failed to write PMA file: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Failed to save file"
                ) from exc
        return {"status": "ok", "saved": saved}

    @router.get("/files/{box}/{filename}")
    def download_pma_file(box: str, filename: str, request: Request):
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        try:
            entry = filebox.resolve_file(hub_root, box, filename)
        except ValueError as exc:
            logger.warning("Invalid filename in PMA download: %s (%s)", filename, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is None:
            logger.warning("File not found in PMA download: %s", filename)
            raise HTTPException(status_code=404, detail="File not found")
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.FILE_DOWNLOADED,
            details={
                "box": box,
                "filename": entry.name,
                "size": entry.size,
            },
        )
        return FileResponse(entry.path, filename=entry.name)

    @router.delete("/files/{box}/{filename}")
    async def delete_pma_file(box: str, filename: str, request: Request):
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        entry: Optional[filebox.FileBoxEntry] = None
        try:
            async with await _get_pma_lock():
                entry = await asyncio.to_thread(
                    filebox.resolve_file, hub_root, box, filename
                )
                if entry is None:
                    logger.warning("File not found in PMA delete: %s", filename)
                    raise HTTPException(status_code=404, detail="File not found")
                await asyncio.to_thread(entry.path.unlink)
        except ValueError as exc:
            logger.warning("Invalid filename in PMA delete: %s (%s)", filename, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except FileNotFoundError:
            logger.warning("File not found in PMA delete: %s", filename)
            raise HTTPException(status_code=404, detail="File not found") from None
        except Exception as exc:
            logger.warning("Failed to delete PMA file: %s", exc)
            raise HTTPException(
                status_code=500, detail="Failed to delete file"
            ) from exc
        if entry is not None:
            _get_safety_checker(request).record_action(
                action_type=PmaActionType.FILE_DELETED,
                details={"box": box, "filename": entry.name, "size": entry.size},
            )
        return {"status": "ok"}

    @router.delete("/files/{box}")
    async def delete_pma_box(box: str, request: Request):
        if box not in ("inbox", "outbox"):
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        deleted_files: list[str] = []
        async with await _get_pma_lock():
            entries = await asyncio.to_thread(
                filebox.list_filebox, hub_root, include_legacy=True
            )
            for entry in entries.get(box, []):
                try:
                    await asyncio.to_thread(entry.path.unlink)
                    deleted_files.append(entry.name)
                except FileNotFoundError:
                    continue
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
    async def snapshot_pma_context(
        request: Request, body: Optional[dict[str, Any]] = None
    ):
        hub_root = request.app.state.config.root
        try:
            await asyncio.to_thread(ensure_pma_docs, hub_root)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to ensure PMA docs: {exc}"
            ) from exc

        reset = False
        if isinstance(body, dict):
            reset = bool(body.get("reset", False))

        docs_dir = _pma_docs_dir(hub_root)
        try:
            await asyncio.to_thread(docs_dir.mkdir, parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to prepare PMA docs directory: {exc}"
            ) from exc
        active_context_path = docs_dir / "active_context.md"
        if not await asyncio.to_thread(active_context_path.exists):
            raise HTTPException(
                status_code=404, detail="Doc not found: active_context.md"
            )
        context_log_path = docs_dir / "context_log.md"

        try:
            active_content = await asyncio.to_thread(
                active_context_path.read_text, encoding="utf-8"
            )
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
            await _append_text_file(context_log_path, snapshot_content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to append context_log.md: {exc}"
            ) from exc

        if reset:
            try:
                await _atomic_write_async(
                    active_context_path, pma_active_context_content()
                )
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
            context_log_bytes = (await asyncio.to_thread(context_log_path.stat)).st_size
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
            return filebox.sanitize_filename(name)
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

    async def _write_doc_history(
        hub_root: Path, doc_name: str, content: str
    ) -> Optional[Path]:
        docs_dir = _pma_docs_dir(hub_root)
        history_root = docs_dir / "_history" / doc_name

        def _write() -> Path:
            history_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history_path = history_root / f"{timestamp}.md"
            atomic_write(history_path, content)
            return history_path

        try:
            return await asyncio.to_thread(_write)
        except Exception:
            logger.exception("Failed to write PMA doc history for %s", doc_name)
            return None

    @router.get("/docs/default/{name}")
    def get_pma_doc_default(name: str, request: Request) -> dict[str, str]:
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        content_fn = PMA_DOC_DEFAULTS.get(name)
        if content_fn is None:
            raise HTTPException(status_code=404, detail=f"Default not found: {name}")
        return {"name": name, "content": content_fn()}

    @router.get("/docs")
    def list_pma_docs(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
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
            "active_context_auto_prune": get_active_context_auto_prune_meta(hub_root),
        }

    @router.get("/docs/{name}")
    def get_pma_doc(name: str, request: Request) -> dict[str, str]:
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
    async def update_pma_doc(
        name: str, request: Request, body: dict[str, str]
    ) -> dict[str, str]:
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
            await _atomic_write_async(doc_path, content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to write doc: {exc}"
            ) from exc
        await _write_doc_history(hub_root, name, content)
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

    router._pma_start_lane_worker = _ensure_lane_worker_for_app  # type: ignore[attr-defined]
    router._pma_stop_lane_worker = _stop_lane_worker_for_app  # type: ignore[attr-defined]
    router._pma_stop_all_lane_workers = _stop_all_lane_workers_for_app  # type: ignore[attr-defined]

    def _get_runtime_state():
        return _pma_runtime_state

    build_automation_routes(router, _get_runtime_state)
    build_managed_thread_crud_routes(router, _get_runtime_state)
    build_managed_thread_tail_routes(router, _get_runtime_state)

    return router


__all__ = ["build_pma_routes"]
