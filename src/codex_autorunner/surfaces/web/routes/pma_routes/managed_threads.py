from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional, cast

from fastapi import APIRouter, Body, HTTPException, Request

from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.chat_bindings import active_chat_binding_metadata_by_thread
from .....core.orchestration import build_harness_backed_orchestration_service
from .....core.orchestration.catalog import RuntimeAgentDescriptor
from .....core.orchestration.cold_trace_store import ColdTraceStore
from .....core.orchestration.turn_timeline import list_turn_timeline
from .....core.pma_automation_store import PmaAutomationThreadNotFoundError
from .....core.pma_thread_store import PmaThreadStore
from ...schemas import (
    PmaAutomationSubscriptionCreateRequest,
    PmaAutomationTimerCancelRequest,
    PmaAutomationTimerCreateRequest,
    PmaAutomationTimerTouchRequest,
    PmaManagedThreadBulkArchiveRequest,
    PmaManagedThreadCompactRequest,
    PmaManagedThreadCreateRequest,
    PmaManagedThreadForkRequest,
    PmaManagedThreadResumeRequest,
)
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
    resolve_origin_followup_context,
)
from .automation_adapter import (
    call_store_action_with_id,
    call_store_create_with_payload,
    call_store_list,
    get_automation_store,
    normalize_optional_text,
)
from .hermes_supervisors import resolve_cached_hermes_supervisor
from .managed_thread_route_helpers import (
    _apply_chat_binding_fields,
    _attach_latest_execution_fields,
    _serialize_managed_thread,
    _serialize_thread_target,
    resolve_managed_thread_create_resolution,
    resolve_managed_thread_list_query,
    resolve_owner_scoped_query,
    serialize_active_work_summary,
    serialize_binding_record,
    serialize_managed_thread_turn_summary,
)

if TYPE_CHECKING:
    pass


_logger = logging.getLogger(__name__)


def _load_chat_binding_metadata_by_thread(hub_root):
    try:
        return active_chat_binding_metadata_by_thread(hub_root=hub_root)
    except Exception as exc:  # intentional: non-critical metadata load
        _logger.warning(
            "Could not load PMA chat-binding metadata for thread response: %s", exc
        )
        return {}


def _subscription_request_has_explicit_routing(payload: dict[str, Any]) -> bool:
    return any(
        normalize_optional_text(payload.get(field)) is not None
        for field in ("thread_id", "lane_id")
    )


def build_managed_thread_orchestration_service(request: Request):
    try:
        descriptors = get_registered_agents(request.app.state)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()

    def _make_harness(agent_id: str, profile: Optional[str] = None):
        cache = getattr(request.app.state, "_managed_thread_harness_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            request.app.state._managed_thread_harness_cache = cache
        resolution = resolve_agent_runtime(agent_id, profile, context=request.app.state)
        runtime_agent_id = resolution.runtime_agent_id
        runtime_profile = resolution.runtime_profile
        cache_key = (runtime_agent_id, runtime_profile or "")
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        descriptor = descriptors.get(runtime_agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{runtime_agent_id}'")
        harness = descriptor.make_harness(
            wrap_requested_agent_context(
                request.app.state,
                agent_id=runtime_agent_id,
                profile=runtime_profile,
            )
        )
        cache[cache_key] = harness
        return harness

    return build_harness_backed_orchestration_service(
        descriptors=cast(dict[str, RuntimeAgentDescriptor], descriptors),
        harness_factory=_make_harness,
        pma_thread_store=PmaThreadStore(request.app.state.config.root),
    )


def _resolve_hermes_supervisor(request: Request, *, profile: Optional[str]):
    return resolve_cached_hermes_supervisor(request, profile=profile)


def _resolve_fork_supervisor(request: Request, *, profile: Optional[str]):
    return _resolve_hermes_supervisor(request, profile=profile)


def build_automation_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build automation subscription and timer routes."""

    @router.post("/automation/subscriptions")
    @router.post("/subscriptions")
    async def create_automation_subscription(
        request: Request, payload: PmaAutomationSubscriptionCreateRequest
    ) -> dict[str, Any]:
        runtime_state = get_runtime_state()
        store = await get_automation_store(request, runtime_state)
        try:
            normalized_payload = payload.normalized_payload()
            if not _subscription_request_has_explicit_routing(normalized_payload):
                origin_thread_id, origin_lane_id = resolve_origin_followup_context(
                    runtime_state
                )
                if origin_thread_id:
                    normalized_payload.setdefault("origin_thread_id", origin_thread_id)
                if origin_lane_id:
                    normalized_payload.setdefault("origin_lane_id", origin_lane_id)
            created = await call_store_create_with_payload(
                store,
                (
                    "create_subscription",
                    "upsert_subscription",
                ),
                normalized_payload,
            )
        except PmaAutomationThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(created, dict) and "subscription" in created:
            return created
        return {"subscription": created}

    @router.get("/automation/subscriptions")
    @router.get("/subscriptions")
    async def list_automation_subscriptions(
        request: Request,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        store = await get_automation_store(request, get_runtime_state())
        subscriptions = await call_store_list(
            store,
            ("list_subscriptions", "get_subscriptions"),
            {
                k: v
                for k, v in {
                    "repo_id": normalize_optional_text(repo_id),
                    "run_id": normalize_optional_text(run_id),
                    "thread_id": normalize_optional_text(thread_id),
                    "lane_id": normalize_optional_text(lane_id),
                    "limit": limit,
                }.items()
                if v is not None
            },
        )
        if isinstance(subscriptions, dict) and "subscriptions" in subscriptions:
            return subscriptions
        return {"subscriptions": list(subscriptions or [])}

    @router.delete("/automation/subscriptions/{subscription_id}")
    @router.delete("/subscriptions/{subscription_id}")
    async def delete_automation_subscription(
        subscription_id: str, request: Request
    ) -> dict[str, Any]:
        normalized_id = (subscription_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        store = await get_automation_store(request, get_runtime_state())
        deleted = await call_store_action_with_id(
            store,
            ("cancel_subscription",),
            normalized_id,
            payload={},
            id_aliases=("subscription_id", "id"),
        )
        if isinstance(deleted, dict):
            payload = dict(deleted)
            payload.setdefault("status", "ok")
            payload.setdefault("subscription_id", normalized_id)
            return payload
        return {
            "status": "ok",
            "subscription_id": normalized_id,
            "deleted": True if deleted is None else bool(deleted),
        }

    @router.post("/automation/timers")
    @router.post("/timers")
    async def create_automation_timer(
        request: Request, payload: PmaAutomationTimerCreateRequest
    ) -> dict[str, Any]:
        store = await get_automation_store(request, get_runtime_state())
        try:
            normalized_payload = payload.normalized_payload()
            created = await call_store_create_with_payload(
                store,
                ("create_timer", "upsert_timer"),
                normalized_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(created, dict) and "timer" in created:
            return created
        return {"timer": created}

    @router.get("/automation/timers")
    @router.get("/timers")
    async def list_automation_timers(
        request: Request,
        timer_type: Optional[str] = None,
        subscription_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        store = await get_automation_store(request, get_runtime_state())
        timers = await call_store_list(
            store,
            ("list_timers", "get_timers"),
            {
                k: v
                for k, v in {
                    "timer_type": normalize_optional_text(timer_type),
                    "subscription_id": normalize_optional_text(subscription_id),
                    "repo_id": normalize_optional_text(repo_id),
                    "run_id": normalize_optional_text(run_id),
                    "thread_id": normalize_optional_text(thread_id),
                    "lane_id": normalize_optional_text(lane_id),
                    "limit": limit,
                }.items()
                if v is not None
            },
        )
        if isinstance(timers, dict) and "timers" in timers:
            return timers
        return {"timers": list(timers or [])}

    @router.post("/automation/timers/{timer_id}/touch")
    @router.post("/timers/{timer_id}/touch")
    async def touch_automation_timer(
        timer_id: str,
        request: Request,
        payload: Annotated[Optional[PmaAutomationTimerTouchRequest], Body()] = None,
    ) -> dict[str, Any]:
        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        store = await get_automation_store(request, get_runtime_state())
        try:
            normalized_payload = payload.normalized_payload() if payload else {}
            touched = await call_store_action_with_id(
                store,
                ("touch_timer", "refresh_timer", "renew_timer"),
                normalized_id,
                payload=normalized_payload,
                id_aliases=("timer_id", "id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(touched, dict):
            out = dict(touched)
            out.setdefault("status", "ok")
            out.setdefault("timer_id", normalized_id)
            return out
        return {"status": "ok", "timer_id": normalized_id}

    @router.post("/automation/timers/{timer_id}/cancel")
    @router.post("/timers/{timer_id}/cancel")
    @router.delete("/automation/timers/{timer_id}")
    @router.delete("/timers/{timer_id}")
    async def cancel_automation_timer(
        timer_id: str,
        request: Request,
        payload: Annotated[Optional[PmaAutomationTimerCancelRequest], Body()] = None,
    ) -> dict[str, Any]:
        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        store = await get_automation_store(request, get_runtime_state())
        try:
            normalized_payload = payload.normalized_payload() if payload else {}
            cancelled = await call_store_action_with_id(
                store,
                ("cancel_timer",),
                normalized_id,
                payload=normalized_payload,
                id_aliases=("timer_id", "id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(cancelled, dict):
            out = dict(cancelled)
            out.setdefault("status", "ok")
            out.setdefault("timer_id", normalized_id)
            return out
        return {"status": "ok", "timer_id": normalized_id}


def build_managed_thread_crud_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread CRUD routes (create, list, get, compact, resume, archive)."""

    @router.post("/threads")
    async def create_managed_thread(
        request: Request, payload: PmaManagedThreadCreateRequest
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        resolved = resolve_managed_thread_create_resolution(request, payload)

        service = build_managed_thread_orchestration_service(request)
        try:
            thread = service.create_thread_target(
                resolved.agent_id,
                resolved.workspace_root,
                repo_id=resolved.repo_id,
                resource_kind=resolved.resource_kind,
                resource_id=resolved.resource_id,
                display_name=normalize_optional_text(payload.name),
                metadata=resolved.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notification: Optional[dict[str, Any]] = None
        if resolved.followup_policy.event_mode == "terminal":
            automation_client = ManagedThreadAutomationClient(
                request,
                get_runtime_state,
            )
            try:
                notification = await automation_client.create_terminal_followup(
                    managed_thread_id=thread.thread_target_id,
                    lane_id=resolved.followup_policy.lane_id,
                    notify_once=resolved.followup_policy.notify_once,
                    idempotency_key=(
                        f"managed-thread-notify:{thread.thread_target_id}"
                        if resolved.followup_policy.notify_once
                        else None
                    ),
                    required=resolved.followup_policy.required,
                )
            except ManagedThreadAutomationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Automation action unavailable"
                ) from exc
        binding_metadata = _load_chat_binding_metadata_by_thread(hub_root)
        response: dict[str, Any] = {
            "thread": _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
            )
        }
        if notification is not None:
            response["notification"] = notification
        return response

    @router.get("/threads")
    def list_managed_threads(
        request: Request,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        query = resolve_managed_thread_list_query(
            agent=agent,
            status=status,
            lifecycle_status=lifecycle_status,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        threads = service.list_thread_targets(
            agent_id=query.agent_id,
            lifecycle_status=query.lifecycle_status,
            runtime_status=query.runtime_status,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            limit=query.limit,
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in threads
            ]
        }

    @router.get("/threads/{managed_thread_id}")
    def get_managed_thread(managed_thread_id: str, request: Request) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        serialized_thread = _attach_latest_execution_fields(
            _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
            ),
            service=service,
            managed_thread_id=managed_thread_id,
        )
        return {
            "thread": serialized_thread,
        }

    @router.post("/threads/{managed_thread_id}/compact")
    def compact_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadCompactRequest,
    ) -> dict[str, Any]:
        summary = (payload.summary or "").strip()
        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")
        max_text_chars = int(request.app.state.config.pma.max_text_chars or 0)
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

        old_backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
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
        thread_payload = _serialize_managed_thread(updated)
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "thread": _apply_chat_binding_fields(
                thread_payload,
                managed_thread_id=managed_thread_id,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/{managed_thread_id}/fork")
    async def fork_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadForkRequest,
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        source_thread = service.get_thread_target(managed_thread_id)
        if source_thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        if not source_thread.workspace_root:
            raise HTTPException(
                status_code=500,
                detail="Managed thread is missing workspace_root",
            )
        runtime_resolution = resolve_agent_runtime(
            source_thread.agent_id,
            source_thread.agent_profile,
            context=request.app.state,
        )
        if runtime_resolution.logical_agent_id != "hermes":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Managed thread agent '{source_thread.agent_id}' does not "
                    "support session fork"
                ),
            )
        source_session_id = normalize_optional_text(source_thread.backend_thread_id)
        if source_session_id is None:
            raise HTTPException(
                status_code=409,
                detail="Managed thread has no backend session to fork",
            )
        supervisor = _resolve_fork_supervisor(
            request,
            profile=runtime_resolution.logical_profile,
        )
        if supervisor is None:
            raise HTTPException(status_code=503, detail="Hermes runtime unavailable")
        try:
            forked_session = await supervisor.fork_session(
                Path(source_thread.workspace_root),
                source_session_id,
                title=normalize_optional_text(payload.name)
                or source_thread.display_name
                or source_thread.thread_target_id,
                metadata={
                    "flow_type": "pma_managed_thread_fork",
                    "managed_thread_id": managed_thread_id,
                    "source_backend_thread_id": source_session_id,
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if forked_session is None or not forked_session.session_id:
            raise HTTPException(
                status_code=409,
                detail="Hermes runtime does not support session fork",
            )

        store = PmaThreadStore(request.app.state.config.root)
        stored_source = store.get_thread(managed_thread_id)
        metadata = dict((stored_source or {}).get("metadata") or {})
        if source_thread.backend_runtime_instance_id:
            metadata["backend_runtime_instance_id"] = (
                source_thread.backend_runtime_instance_id
            )
        try:
            forked_thread = service.create_thread_target(
                source_thread.agent_id,
                Path(source_thread.workspace_root),
                repo_id=source_thread.repo_id,
                resource_kind=source_thread.resource_kind,
                resource_id=source_thread.resource_id,
                display_name=normalize_optional_text(payload.name)
                or source_thread.display_name,
                backend_thread_id=forked_session.session_id,
                metadata=metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refreshed_forked_thread = service.get_thread_target(
            forked_thread.thread_target_id
        )
        if refreshed_forked_thread is not None:
            forked_thread = refreshed_forked_thread
        store.append_action(
            "managed_thread_fork",
            managed_thread_id=forked_thread.thread_target_id,
            payload_json=json.dumps(
                {
                    "source_managed_thread_id": managed_thread_id,
                    "source_backend_thread_id": source_session_id,
                    "forked_backend_thread_id": forked_session.session_id,
                },
                ensure_ascii=True,
            ),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "thread": _serialize_thread_target(
                forked_thread,
                binding_metadata_by_thread=binding_metadata,
            ),
            "forked_from_managed_thread_id": managed_thread_id,
            "source_backend_thread_id": source_session_id,
        }

    @router.post("/threads/{managed_thread_id}/resume")
    async def resume_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadResumeRequest,
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        if not thread.workspace_root:
            raise HTTPException(
                status_code=500,
                detail="Managed thread is missing workspace_root",
            )

        old_backend_thread_id = normalize_optional_text(thread.backend_thread_id)
        old_status = normalize_optional_text(thread.lifecycle_status)
        updated = service.resume_thread_target(managed_thread_id)
        store = PmaThreadStore(request.app.state.config.root)
        store.append_action(
            "managed_thread_resume",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "old_backend_thread_id": old_backend_thread_id,
                    "old_status": old_status,
                },
                ensure_ascii=True,
            ),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "thread": _serialize_thread_target(
                updated,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/{managed_thread_id}/archive")
    def archive_managed_thread(
        managed_thread_id: str, request: Request
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_status = normalize_optional_text(thread.lifecycle_status)
        updated = service.archive_thread_target(managed_thread_id)
        store = PmaThreadStore(request.app.state.config.root)
        store.append_action(
            "managed_thread_archive",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps({"old_status": old_status}, ensure_ascii=True),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "thread": _serialize_thread_target(
                updated,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/archive")
    def archive_managed_threads(
        payload: PmaManagedThreadBulkArchiveRequest, request: Request
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        store = PmaThreadStore(request.app.state.config.root)
        archived_threads: list[Any] = []
        errors: list[dict[str, str]] = []

        for managed_thread_id in payload.thread_ids:
            thread = service.get_thread_target(managed_thread_id)
            if thread is None:
                errors.append(
                    {
                        "thread_id": managed_thread_id,
                        "detail": "Managed thread not found",
                    }
                )
                continue

            old_status = normalize_optional_text(thread.lifecycle_status)
            updated = service.archive_thread_target(managed_thread_id)
            store.append_action(
                "managed_thread_archive",
                managed_thread_id=managed_thread_id,
                payload_json=json.dumps({"old_status": old_status}, ensure_ascii=True),
            )
            archived_threads.append(updated)

        binding_metadata = _load_chat_binding_metadata_by_thread(
            request.app.state.config.root
        )
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in archived_threads
            ],
            "archived_count": len(archived_threads),
            "requested_count": len(payload.thread_ids),
            "errors": errors,
            "error_count": len(errors),
        }

    @router.get("/threads/{managed_thread_id}/turns")
    def list_managed_thread_turns(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        limit = min(limit, 200)

        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        turns = store.list_turns(managed_thread_id, limit=limit)
        return {
            "turns": [serialize_managed_thread_turn_summary(turn) for turn in turns]
        }

    @router.get("/threads/{managed_thread_id}/turns/{managed_turn_id}")
    def get_managed_thread_turn(
        managed_thread_id: str,
        managed_turn_id: str,
        request: Request,
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        store = PmaThreadStore(hub_root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        turn = store.get_turn(managed_thread_id, managed_turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Managed turn not found")

        timeline = list_turn_timeline(
            hub_root,
            execution_id=managed_turn_id,
        )

        cold_store = ColdTraceStore(hub_root)
        manifest = cold_store.get_manifest(managed_turn_id)
        checkpoint = cold_store.load_checkpoint(managed_turn_id)

        hot_truncated = False
        hot_preview_only = False
        for entry in timeline:
            event = entry.get("event")
            if not isinstance(event, dict):
                continue
            if event.get("details_in_cold_trace"):
                hot_truncated = True
                hot_preview_only = True
            for flag in (
                "tool_input_truncated",
                "result_truncated",
                "error_truncated",
                "message_truncated",
                "data_truncated",
                "content_truncated",
                "final_message_truncated",
                "error_message_truncated",
            ):
                if event.get(flag):
                    hot_truncated = True
                    break

        response: dict[str, Any] = {
            "turn": turn,
            "timeline": timeline,
            "trace_metadata": {
                "hot_timeline_entries": len(timeline),
                "hot_preview_truncated": hot_truncated,
                "hot_preview_only": hot_preview_only,
                "cold_trace_available": manifest is not None,
                "checkpoint_available": checkpoint is not None,
            },
        }
        if manifest is not None:
            response["trace_metadata"]["cold_trace"] = {
                "trace_id": manifest.trace_id,
                "status": manifest.status,
                "event_count": manifest.event_count,
                "byte_count": manifest.byte_count,
                "includes_families": list(manifest.includes_families),
            }
        return response

    @router.get("/bindings")
    def list_bindings(
        request: Request,
        agent: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        query = resolve_owner_scoped_query(
            agent=agent,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        bindings = service.list_bindings(
            agent_id=query.agent_id,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            surface_kind=normalize_optional_text(surface_kind),
            include_disabled=include_disabled,
            limit=query.limit,
        )
        return {"bindings": [serialize_binding_record(binding) for binding in bindings]}

    @router.get("/bindings/active")
    def get_active_thread_for_binding(
        request: Request,
        surface_kind: str,
        surface_key: str,
    ) -> dict[str, Any]:
        if not surface_kind or not surface_key:
            raise HTTPException(
                status_code=400, detail="surface_kind and surface_key are required"
            )
        service = build_managed_thread_orchestration_service(request)
        thread_target_id = service.get_active_thread_for_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        return {"thread_target_id": thread_target_id}

    @router.get("/bindings/work")
    def list_active_work_summaries(
        request: Request,
        agent: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List busy thread summaries for running or queued work only."""
        query = resolve_owner_scoped_query(
            agent=agent,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        summaries = service.list_active_work_summaries(
            agent_id=query.agent_id,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            limit=query.limit,
        )
        return {"summaries": [serialize_active_work_summary(s) for s in summaries]}
