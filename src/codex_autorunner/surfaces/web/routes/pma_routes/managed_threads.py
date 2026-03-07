from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ...schemas import (
    PmaAutomationSubscriptionCreateRequest,
    PmaAutomationTimerCancelRequest,
    PmaAutomationTimerCreateRequest,
    PmaAutomationTimerTouchRequest,
)

if TYPE_CHECKING:
    pass


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
        from .automation_adapter import (
            call_store_create_with_payload,
            get_automation_store,
        )

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
        from .automation_adapter import (
            call_store_list,
            get_automation_store,
            normalize_optional_text,
        )

        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        store = await get_automation_store(request, get_runtime_state())
        filters = {
            "repo_id": normalize_optional_text(repo_id),
            "run_id": normalize_optional_text(run_id),
            "thread_id": normalize_optional_text(thread_id),
            "lane_id": normalize_optional_text(lane_id),
            "limit": limit,
        }
        subscriptions = await call_store_list(
            store,
            (
                "list_subscriptions",
                "get_subscriptions",
            ),
            {k: v for k, v in filters.items() if v is not None},
        )
        if isinstance(subscriptions, dict) and "subscriptions" in subscriptions:
            return subscriptions
        if subscriptions is None:
            subscriptions = []
        return {"subscriptions": list(subscriptions)}

    @router.delete("/automation/subscriptions/{subscription_id}")
    @router.delete("/subscriptions/{subscription_id}")
    async def delete_automation_subscription(
        subscription_id: str, request: Request
    ) -> dict[str, Any]:
        from .automation_adapter import (
            call_store_action_with_id,
            get_automation_store,
        )

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
        request: Request, payload: "PmaAutomationTimerCreateRequest"
    ) -> dict[str, Any]:
        from .automation_adapter import (
            call_store_create_with_payload,
            get_automation_store,
        )

        store = await get_automation_store(request, get_runtime_state())
        created = await call_store_create_with_payload(
            store,
            (
                "create_timer",
                "add_timer",
                "upsert_timer",
            ),
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
        from .automation_adapter import (
            call_store_list,
            get_automation_store,
            normalize_optional_text,
        )

        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        store = await get_automation_store(request, get_runtime_state())
        filters = {
            "timer_type": normalize_optional_text(timer_type),
            "subscription_id": normalize_optional_text(subscription_id),
            "repo_id": normalize_optional_text(repo_id),
            "run_id": normalize_optional_text(run_id),
            "thread_id": normalize_optional_text(thread_id),
            "lane_id": normalize_optional_text(lane_id),
            "limit": limit,
        }
        timers = await call_store_list(
            store,
            (
                "list_timers",
                "get_timers",
            ),
            {k: v for k, v in filters.items() if v is not None},
        )
        if isinstance(timers, dict) and "timers" in timers:
            return timers
        if timers is None:
            timers = []
        return {"timers": list(timers)}

    @router.post("/automation/timers/{timer_id}/touch")
    @router.post("/timers/{timer_id}/touch")
    async def touch_automation_timer(
        timer_id: str,
        request: Request,
        payload: Optional["PmaAutomationTimerTouchRequest"] = None,
    ) -> dict[str, Any]:
        from .automation_adapter import (
            call_store_action_with_id,
            get_automation_store,
        )

        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        store = await get_automation_store(request, get_runtime_state())
        resolved_payload = (
            payload.model_dump(exclude_none=True) if payload is not None else {}
        )
        touched = await call_store_action_with_id(
            store,
            (
                "touch_timer",
                "refresh_timer",
                "renew_timer",
            ),
            normalized_id,
            payload=resolved_payload,
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
        payload: Optional["PmaAutomationTimerCancelRequest"] = None,
    ) -> dict[str, Any]:
        from .automation_adapter import (
            call_store_action_with_id,
            get_automation_store,
        )

        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        store = await get_automation_store(request, get_runtime_state())
        resolved_payload = (
            payload.model_dump(exclude_none=True) if payload is not None else {}
        )
        cancelled = await call_store_action_with_id(
            store,
            (
                "cancel_timer",
                "delete_timer",
                "remove_timer",
            ),
            normalized_id,
            payload=resolved_payload,
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
    import json
    import re
    from pathlib import Path

    from .....core.pma_thread_store import PmaThreadStore
    from .automation_adapter import normalize_optional_text

    _DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")

    @router.post("/threads")
    async def create_managed_thread(request: Request, payload: Any) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        raw_payload = (
            payload.model_dump(exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload
        )
        if isinstance(raw_payload, dict):
            repo_id = normalize_optional_text(raw_payload.get("repo_id"))
            workspace_root = normalize_optional_text(raw_payload.get("workspace_root"))
            agent = raw_payload.get("agent")
            name = normalize_optional_text(raw_payload.get("name"))
            backend_thread_id = normalize_optional_text(
                raw_payload.get("backend_thread_id")
            )
        else:
            repo_id = None
            workspace_root = None
            agent = None
            name = None
            backend_thread_id = None

        if bool(repo_id) == bool(workspace_root):
            raise HTTPException(
                status_code=400,
                detail="Exactly one of repo_id or workspace_root is required",
            )

        resolved_repo_id: Optional[str] = None
        resolved_workspace: Optional[Path] = None

        if repo_id:
            from .....core.state_roots import is_within_allowed_root
            from .....manifest import load_manifest

            manifest = load_manifest(hub_root)
            workspace_root_from_manifest = manifest.workspace_for_repo_id(repo_id)
            if workspace_root_from_manifest:
                resolved_workspace = workspace_root_from_manifest
                resolved_repo_id = repo_id
            if not resolved_workspace or not is_within_allowed_root(
                resolved_workspace, hub_root
            ):
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
            from .....core.state_roots import is_within_allowed_root

            workspace_root_clean = workspace_root.replace("\\", "/")
            if _DRIVE_PREFIX_RE.match(workspace_root_clean):
                workspace_root_clean = "/" + workspace_root_clean
            resolved_workspace = Path(workspace_root_clean).resolve()
            if not is_within_allowed_root(resolved_workspace, hub_root):
                raise HTTPException(
                    status_code=400,
                    detail="workspace_root must be within allowed roots",
                )

        store = PmaThreadStore(hub_root)
        thread = store.create_thread(
            agent,
            resolved_workspace,
            repo_id=resolved_repo_id,
            name=name,
            backend_thread_id=backend_thread_id,
        )
        return {"thread": thread}

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
            agent=normalize_optional_text(agent),
            status=normalize_optional_text(status),
            repo_id=normalize_optional_text(repo_id),
            limit=limit,
        )
        return {"threads": threads}

    @router.get("/threads/{managed_thread_id}")
    def get_managed_thread(managed_thread_id: str, request: Request) -> dict[str, Any]:
        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return {"thread": thread}

    @router.post("/threads/{managed_thread_id}/compact")
    def compact_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: Any,
    ) -> dict[str, Any]:
        summary = ""
        reset_backend = False
        if hasattr(payload, "model_dump"):
            summary = (payload.summary or "").strip()
            reset_backend = bool(payload.reset_backend)
        elif isinstance(payload, dict):
            summary = (payload.get("summary") or "").strip()
            reset_backend = bool(payload.get("reset_backend"))

        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")

        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
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
        payload: Any,
    ) -> dict[str, Any]:
        backend_thread_id = ""
        if hasattr(payload, "model_dump"):
            backend_thread_id = (payload.backend_thread_id or "").strip()
        elif isinstance(payload, dict):
            backend_thread_id = (payload.get("backend_thread_id") or "").strip()

        if not backend_thread_id:
            raise HTTPException(status_code=400, detail="backend_thread_id is required")

        store = PmaThreadStore(request.app.state.config.root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
        old_status = normalize_optional_text(thread.get("status"))
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

        old_status = normalize_optional_text(thread.get("status"))
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
