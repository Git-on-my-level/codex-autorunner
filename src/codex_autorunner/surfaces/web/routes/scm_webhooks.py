from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ....adapters.github import GitHubWebhookConfig, normalize_github_webhook
from ....core.scm_webhook_config import (
    drain_inline_enabled,
    github_automation_enabled,
    github_webhook_ingress_enabled,
    resolve_github_webhook_config,
    resolve_payload_limits,
)
from ..services.scm_webhooks import (
    ScmDrainCallback,
    ScmWebhookIngestRequest,
    ScmWebhookInspectService,
    ingest_scm_webhook_event,
)

_DEFAULT_INSPECT_LIMIT = 50


def _to_webhook_config(resolved) -> GitHubWebhookConfig:
    return GitHubWebhookConfig(
        secret=resolved.secret,
        verify_signatures=resolved.verify_signatures,
        allow_unsigned=resolved.allow_unsigned,
    )


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _rejection_status_code(reason: Optional[str]) -> int:
    if reason in {"missing_signature", "invalid_signature"}:
        return 401
    if reason == "payload_too_large":
        return 413
    return 400


def _require_hub_root(request: Request) -> Path:
    config = getattr(request.app.state, "config", None)
    hub_root = getattr(config, "root", None)
    if hub_root is None:
        raise HTTPException(status_code=503, detail="Hub config unavailable")
    return Path(hub_root)


def _request_raw_config(request: Request) -> object:
    config = getattr(request.app.state, "config", None)
    return getattr(config, "raw", {})


def _require_scm_automation_enabled(raw_config: object) -> None:
    if not github_automation_enabled(raw_config):
        raise HTTPException(status_code=404, detail="SCM automation disabled")


def _inspect_service(request: Request) -> ScmWebhookInspectService:
    _require_scm_automation_enabled(_request_raw_config(request))
    return ScmWebhookInspectService(_require_hub_root(request))


def build_scm_webhook_routes(
    *, drain_callback: Optional[ScmDrainCallback] = None
) -> APIRouter:
    router = APIRouter(prefix="/hub/scm", tags=["scm"])

    @router.get("/inspect/events")
    def list_scm_events(
        request: Request,
        provider: Optional[str] = None,
        event_type: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_number: Optional[int] = None,
        delivery_id: Optional[str] = None,
        occurred_after: Optional[str] = None,
        occurred_before: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        try:
            return _inspect_service(request).list_events(
                provider=provider,
                event_type=event_type,
                repo_slug=repo_slug,
                repo_id=repo_id,
                pr_number=pr_number,
                delivery_id=delivery_id,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/inspect/bindings")
    def list_pr_bindings(
        request: Request,
        provider: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_state: Optional[str] = None,
        head_branch: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        try:
            return _inspect_service(request).list_pr_bindings(
                provider=provider,
                repo_slug=repo_slug,
                repo_id=repo_id,
                pr_state=pr_state,
                head_branch=head_branch,
                thread_target_id=thread_target_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/inspect/reactions")
    def list_scm_reaction_states(
        request: Request,
        binding_id: Optional[str] = None,
        reaction_kind: Optional[str] = None,
        state: Optional[str] = None,
        last_event_id: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        return _inspect_service(request).list_reaction_states(
            binding_id=binding_id,
            reaction_kind=reaction_kind,
            state=state,
            last_event_id=last_event_id,
            limit=limit,
        )

    @router.get("/inspect/publish-operations")
    def list_publish_operations(
        request: Request,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        return _inspect_service(request).list_publish_operations(
            state=state,
            operation_kind=operation_kind,
            limit=limit,
        )

    @router.post("/webhooks/github")
    async def ingest_github_webhook(request: Request):
        raw_config = _request_raw_config(request)
        if not github_webhook_ingress_enabled(raw_config):
            raise HTTPException(
                status_code=404, detail="GitHub webhook ingress disabled"
            )
        hub_root = _require_hub_root(request)
        max_payload_bytes, max_raw_payload_bytes, store_raw_payload = (
            resolve_payload_limits(raw_config)
        )

        body = await request.body()
        if len(body) > max_payload_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "status": "rejected",
                    "reason": "payload_too_large",
                    "detail": f"Webhook body exceeds max_payload_bytes={max_payload_bytes}",
                },
            )

        result = normalize_github_webhook(
            headers=request.headers,
            body=body,
            config=_to_webhook_config(resolve_github_webhook_config(raw_config)),
        )
        if result.status == "ignored":
            return _compact(
                {
                    "status": "ignored",
                    "github_event": result.github_event,
                    "delivery_id": result.delivery_id,
                    "reason": result.reason,
                    "detail": result.detail,
                }
            )
        if result.status == "rejected":
            return JSONResponse(
                status_code=_rejection_status_code(result.reason),
                content=_compact(
                    {
                        "status": "rejected",
                        "github_event": result.github_event,
                        "delivery_id": result.delivery_id,
                        "reason": result.reason,
                        "detail": result.detail,
                    }
                ),
            )

        event = result.event
        if event is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Normalized SCM event missing")

        outcome = await ingest_scm_webhook_event(
            ScmWebhookIngestRequest(
                hub_root=hub_root,
                raw_config=raw_config,
                event=event,
                headers=request.headers,
                store_raw_payload=store_raw_payload,
                max_raw_payload_bytes=max_raw_payload_bytes,
                drain_inline=drain_inline_enabled(raw_config),
                request_context=request,
                app=request.app,
                app_drain_callback=getattr(
                    request.app.state, "scm_webhook_drain_callback", None
                ),
                route_drain_callback=drain_callback,
                logger=getattr(request.app.state, "logger", None),
            )
        )
        if outcome.status == "rejected":
            return JSONResponse(
                status_code=outcome.status_code,
                content=outcome.to_response_payload(),
            )
        return outcome.to_response_payload()

    return router


__all__ = [
    "build_scm_webhook_routes",
    "github_automation_enabled",
    "github_webhook_ingress_enabled",
]
