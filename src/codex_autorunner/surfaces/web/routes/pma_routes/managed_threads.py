from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from .....agents.registry import get_registered_agents
from .....core.orchestration import build_harness_backed_orchestration_service
from .....core.orchestration.models import ThreadTarget
from .....core.pma_thread_store import PmaThreadStore
from ...schemas import (
    PmaAutomationSubscriptionCreateRequest,
    PmaAutomationTimerCancelRequest,
    PmaAutomationTimerCreateRequest,
    PmaAutomationTimerTouchRequest,
    PmaManagedThreadCompactRequest,
    PmaManagedThreadCreateRequest,
    PmaManagedThreadResumeRequest,
)
from .automation_adapter import (
    call_store_action_with_id,
    call_store_create_with_payload,
    call_store_list,
    get_automation_store,
    normalize_optional_text,
)

if TYPE_CHECKING:
    pass

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_within_root(path: Path, root: Path) -> bool:
    from .....core.state_roots import is_within_allowed_root

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
    for snapshot in supervisor.list_repos():
        if getattr(snapshot, "id", None) != repo_id:
            continue
        repo_path = getattr(snapshot, "path", None)
        if isinstance(repo_path, str):
            repo_path = Path(repo_path)
        if isinstance(repo_path, Path):
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
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    return workspace


def _normalize_notify_on(value: Any) -> Optional[str]:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return None
    notify_on = normalized.lower()
    if notify_on != "terminal":
        raise HTTPException(
            status_code=400, detail="notify_on must be 'terminal' when provided"
        )
    return notify_on


def _serialize_managed_thread(thread: dict[str, Any]) -> dict[str, Any]:
    payload = dict(thread)
    lifecycle_status = normalize_optional_text(
        thread.get("lifecycle_status") or thread.get("status")
    )
    normalized_status = normalize_optional_text(thread.get("normalized_status"))
    payload["lifecycle_status"] = lifecycle_status
    payload["normalized_status"] = normalized_status or lifecycle_status or ""
    payload["status"] = payload["normalized_status"]
    payload["status_reason"] = normalize_optional_text(
        thread.get("status_reason") or thread.get("status_reason_code")
    )
    payload["status_changed_at"] = normalize_optional_text(
        thread.get("status_changed_at") or thread.get("status_updated_at")
    )
    payload["status_terminal"] = bool(thread.get("status_terminal"))
    payload["status_turn_id"] = normalize_optional_text(thread.get("status_turn_id"))
    payload["accepts_messages"] = lifecycle_status == "active"
    return payload


def _serialize_thread_target(thread: ThreadTarget) -> dict[str, Any]:
    return {
        "managed_thread_id": thread.thread_target_id,
        "agent": thread.agent_id,
        "repo_id": thread.repo_id,
        "workspace_root": thread.workspace_root,
        "name": thread.display_name,
        "backend_thread_id": thread.backend_thread_id,
        "lifecycle_status": thread.lifecycle_status,
        "normalized_status": thread.status,
        "status": thread.status,
        "status_reason": thread.status_reason,
        "status_changed_at": thread.status_changed_at,
        "status_terminal": bool(thread.status_terminal),
        "status_turn_id": thread.status_turn_id,
        "last_turn_id": thread.last_execution_id,
        "last_message_preview": thread.last_message_preview,
        "compact_seed": thread.compact_seed,
        "accepts_messages": thread.lifecycle_status == "active",
    }


def build_managed_thread_orchestration_service(request: Request):
    descriptors = get_registered_agents()

    def _make_harness(agent_id: str):
        descriptor = descriptors.get(agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        return descriptor.make_harness(request.app.state)

    return build_harness_backed_orchestration_service(
        descriptors=descriptors,
        harness_factory=_make_harness,
        pma_thread_store=PmaThreadStore(request.app.state.config.root),
    )


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


async def register_managed_thread_terminal_notify(
    request: Request,
    *,
    managed_thread_id: str,
    lane_id: Optional[str],
    notify_once: bool,
    idempotency_key: Optional[str],
    get_runtime_state,
) -> Optional[dict[str, Any]]:
    store = await get_automation_store(request, get_runtime_state())
    if store is None:
        return None
    created = await call_store_create_with_payload(
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
        store = await get_automation_store(request, get_runtime_state())
        created = await call_store_create_with_payload(
            store,
            (
                "create_subscription",
                "add_subscription",
                "upsert_subscription",
            ),
            payload.model_dump(exclude_none=True),
        )
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
            (
                "delete_subscription",
                "remove_subscription",
                "cancel_subscription",
            ),
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
        created = await call_store_create_with_payload(
            store,
            ("create_timer", "add_timer", "upsert_timer"),
            payload.model_dump(exclude_none=True),
        )
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
        touched = await call_store_action_with_id(
            store,
            ("touch_timer", "refresh_timer", "renew_timer"),
            normalized_id,
            payload=payload.model_dump(exclude_none=True) if payload else {},
            id_aliases=("timer_id", "id"),
        )
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
        cancelled = await call_store_action_with_id(
            store,
            ("cancel_timer", "delete_timer", "remove_timer"),
            normalized_id,
            payload=payload.model_dump(exclude_none=True) if payload else {},
            id_aliases=("timer_id", "id"),
        )
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
    from ...services.pma.common import pma_config_from_raw

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return pma_config_from_raw(raw)

    @router.post("/threads")
    async def create_managed_thread(
        request: Request, payload: PmaManagedThreadCreateRequest
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        repo_id = normalize_optional_text(payload.repo_id)
        workspace_root = normalize_optional_text(payload.workspace_root)
        raw_payload: dict[str, Any] = {}
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                raw_payload = parsed
        except Exception:
            pass
        notify_on = _normalize_notify_on(
            raw_payload.get("notify_on") or raw_payload.get("notifyOn")
        )
        notify_lane = normalize_optional_text(
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
                    status_code=400, detail="Resolved repo path is invalid"
                )
        else:
            if workspace_root is None:
                raise HTTPException(
                    status_code=400,
                    detail="workspace_root is required when repo_id is omitted",
                )
            resolved_workspace = _resolve_workspace_from_input(hub_root, workspace_root)

        service = build_managed_thread_orchestration_service(request)
        thread = service.create_thread_target(
            payload.agent,
            resolved_workspace,
            repo_id=resolved_repo_id,
            display_name=normalize_optional_text(payload.name),
            backend_thread_id=normalize_optional_text(payload.backend_thread_id),
        )
        notification: Optional[dict[str, Any]] = None
        if notify_on == "terminal":
            notification = await register_managed_thread_terminal_notify(
                request,
                managed_thread_id=thread.thread_target_id,
                lane_id=notify_lane,
                notify_once=notify_once,
                idempotency_key=(
                    f"managed-thread-notify:{thread.thread_target_id}"
                    if notify_once
                    else None
                ),
                get_runtime_state=get_runtime_state,
            )
        response: dict[str, Any] = {"thread": _serialize_thread_target(thread)}
        if notification is not None:
            response["notification"] = notification
        return response

    @router.get("/threads")
    def list_managed_threads(
        request: Request,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_status = normalize_optional_text(status)
        normalized_lifecycle_status = normalize_optional_text(lifecycle_status)
        if (
            normalized_status in {"active", "archived"}
            and normalized_lifecycle_status is None
        ):
            normalized_lifecycle_status = normalized_status
            normalized_status = None
        service = build_managed_thread_orchestration_service(request)
        threads = service.list_thread_targets(
            agent_id=normalize_optional_text(agent),
            lifecycle_status=normalized_lifecycle_status,
            runtime_status=normalized_status,
            repo_id=normalize_optional_text(repo_id),
            limit=limit,
        )
        return {"threads": [_serialize_thread_target(thread) for thread in threads]}

    @router.get("/threads/{managed_thread_id}")
    def get_managed_thread(managed_thread_id: str, request: Request) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return {"thread": _serialize_thread_target(thread)}

    @router.post("/threads/{managed_thread_id}/compact")
    def compact_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadCompactRequest,
    ) -> dict[str, Any]:
        summary = (payload.summary or "").strip()
        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")
        max_text_chars = int(_get_pma_config(request).get("max_text_chars", 0) or 0)
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
        return {"thread": _serialize_managed_thread(updated)}

    @router.post("/threads/{managed_thread_id}/resume")
    def resume_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadResumeRequest,
    ) -> dict[str, Any]:
        backend_thread_id = (payload.backend_thread_id or "").strip()
        if not backend_thread_id:
            raise HTTPException(status_code=400, detail="backend_thread_id is required")

        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = normalize_optional_text(thread.backend_thread_id)
        old_status = normalize_optional_text(thread.lifecycle_status)
        updated = service.resume_thread_target(
            managed_thread_id,
            backend_thread_id=backend_thread_id,
        )
        store = PmaThreadStore(request.app.state.config.root)
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
        return {"thread": _serialize_thread_target(updated)}

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
        return {"thread": _serialize_thread_target(updated)}

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
            "turns": [
                {
                    "managed_turn_id": turn.get("managed_turn_id"),
                    "status": turn.get("status"),
                    "prompt_preview": _truncate_text(turn.get("prompt") or "", 120),
                    "assistant_preview": _truncate_text(
                        turn.get("assistant_text") or "", 120
                    ),
                    "started_at": turn.get("started_at"),
                    "finished_at": turn.get("finished_at"),
                    "error": turn.get("error"),
                }
                for turn in turns
            ]
        }

    @router.get("/threads/{managed_thread_id}/turns/{managed_turn_id}")
    def get_managed_thread_turn(
        managed_thread_id: str,
        managed_turn_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        turn = store.get_turn(managed_thread_id, managed_turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Managed turn not found")
        return {"turn": turn}

    @router.get("/bindings")
    def list_bindings(
        request: Request,
        agent: Optional[str] = None,
        repo_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        service = build_managed_thread_orchestration_service(request)
        bindings = service.list_bindings(
            agent_id=normalize_optional_text(agent),
            repo_id=normalize_optional_text(repo_id),
            surface_kind=normalize_optional_text(surface_kind),
            include_disabled=include_disabled,
            limit=limit,
        )
        return {
            "bindings": [
                {
                    "binding_id": b.binding_id,
                    "surface_kind": b.surface_kind,
                    "surface_key": b.surface_key,
                    "thread_target_id": b.thread_target_id,
                    "agent_id": b.agent_id,
                    "repo_id": b.repo_id,
                    "mode": b.mode,
                    "created_at": b.created_at,
                    "updated_at": b.updated_at,
                    "disabled_at": b.disabled_at,
                }
                for b in bindings
            ]
        }

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
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        service = build_managed_thread_orchestration_service(request)
        summaries = service.list_active_work_summaries(
            agent_id=normalize_optional_text(agent),
            repo_id=normalize_optional_text(repo_id),
            limit=limit,
        )
        return {
            "summaries": [
                {
                    "thread_target_id": s.thread_target_id,
                    "agent_id": s.agent_id,
                    "repo_id": s.repo_id,
                    "workspace_root": s.workspace_root,
                    "display_name": s.display_name,
                    "lifecycle_status": s.lifecycle_status,
                    "runtime_status": s.runtime_status,
                    "execution_id": s.execution_id,
                    "execution_status": s.execution_status,
                    "message_preview": s.message_preview,
                    "binding_count": s.binding_count,
                    "surface_kinds": list(s.surface_kinds),
                }
                for s in summaries
            ]
        }


def _truncate_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."
