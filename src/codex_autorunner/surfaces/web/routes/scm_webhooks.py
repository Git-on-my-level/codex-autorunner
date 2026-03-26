from __future__ import annotations

import inspect
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ....core.pr_bindings import PrBindingStore
from ....core.publish_journal import PublishJournalStore
from ....core.scm_automation_service import ScmAutomationService
from ....core.scm_events import ScmEvent, ScmEventStore
from ....core.scm_observability import (
    SCM_AUDIT_INGEST,
    ScmAuditRecorder,
    create_or_preserve_correlation_id,
)
from ....core.scm_reaction_state import ScmReactionStateStore
from ....integrations.github import GitHubWebhookConfig, normalize_github_webhook

ScmDrainCallback = Callable[[Request, ScmEvent], object]
_DEFAULT_INSPECT_LIMIT = 50
_MAX_INSPECT_LIMIT = 200
_DEFAULT_MAX_PAYLOAD_BYTES = 262_144
_DEFAULT_MAX_RAW_PAYLOAD_BYTES = 65_536


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _github_automation_config(raw_config: object) -> Mapping[str, Any]:
    github = _mapping(raw_config).get("github")
    automation = _mapping(github).get("automation")
    return _mapping(automation)


def _github_webhook_ingress_config(raw_config: object) -> Mapping[str, Any]:
    ingress = _github_automation_config(raw_config).get("webhook_ingress")
    return _mapping(ingress)


def github_automation_enabled(raw_config: object) -> bool:
    return bool(_github_automation_config(raw_config).get("enabled"))


def github_webhook_ingress_enabled(raw_config: object) -> bool:
    ingress = _github_webhook_ingress_config(raw_config)
    return github_automation_enabled(raw_config) and bool(ingress.get("enabled"))


def _resolve_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _resolve_limit(value: object, *, default: int) -> int:
    resolved = _resolve_int(value, default=default)
    return min(resolved, _MAX_INSPECT_LIMIT)


def _resolve_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _resolve_secret(*configs: Mapping[str, Any]) -> Optional[str]:
    secret: Optional[str] = None
    secret_env: Optional[str] = None
    for config in configs:
        raw_secret = config.get("secret")
        if isinstance(raw_secret, str) and raw_secret.strip():
            secret = raw_secret.strip()
        raw_secret_env = config.get("secret_env")
        if isinstance(raw_secret_env, str) and raw_secret_env.strip():
            secret_env = raw_secret_env.strip()
    if secret_env:
        env_value = os.getenv(secret_env)
        if isinstance(env_value, str) and env_value.strip():
            return env_value.strip()
    return secret


def _resolve_github_webhook_config(raw_config: object) -> GitHubWebhookConfig:
    automation = _github_automation_config(raw_config)
    ingress = _github_webhook_ingress_config(raw_config)
    return GitHubWebhookConfig(
        secret=_resolve_secret(automation, ingress),
        verify_signatures=_resolve_bool(
            ingress.get("verify_signatures", automation.get("verify_signatures")),
            default=True,
        ),
        allow_unsigned=_resolve_bool(
            ingress.get("allow_unsigned", automation.get("allow_unsigned")),
            default=False,
        ),
    )


def _drain_inline_enabled(raw_config: object) -> bool:
    return _resolve_bool(
        _github_automation_config(raw_config).get("drain_inline"),
        default=False,
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


def _serialize_items(items: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in items:
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            serialized.append(to_dict())
    return serialized


async def _run_drain_callback(
    *,
    request: Request,
    event: ScmEvent,
    route_callback: Optional[ScmDrainCallback],
) -> None:
    callback = getattr(request.app.state, "scm_webhook_drain_callback", None)
    if not callable(callback):
        callback = route_callback
    if not callable(callback):
        config = getattr(request.app.state, "config", None)
        hub_root = getattr(config, "root", None)
        if hub_root is None:
            return
        raw_config = getattr(config, "raw", {})
        callback = _default_drain_callback_factory(
            hub_root=hub_root,
            raw_config=raw_config,
        )
    result = callback(request, event)
    if inspect.isawaitable(result):
        await result


def _default_drain_callback_factory(
    *,
    hub_root: Path,
    raw_config: object,
) -> ScmDrainCallback:
    service = ScmAutomationService(
        hub_root,
        reaction_config=_github_automation_config(raw_config),
    )

    def callback(_request: Request, event: ScmEvent) -> None:
        service.ingest_event(event)
        service.process_now()

    return callback


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
        raw_config = _request_raw_config(request)
        _require_scm_automation_enabled(raw_config)
        hub_root = _require_hub_root(request)
        resolved_limit = _resolve_limit(limit, default=_DEFAULT_INSPECT_LIMIT)
        try:
            events = ScmEventStore(hub_root).list_events(
                provider=provider,
                event_type=event_type,
                repo_slug=repo_slug,
                repo_id=repo_id,
                pr_number=pr_number,
                delivery_id=delivery_id,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                limit=resolved_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"events": _serialize_items(events), "limit": resolved_limit}

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
        raw_config = _request_raw_config(request)
        _require_scm_automation_enabled(raw_config)
        hub_root = _require_hub_root(request)
        resolved_limit = _resolve_limit(limit, default=_DEFAULT_INSPECT_LIMIT)
        try:
            bindings = PrBindingStore(hub_root).list_bindings(
                provider=provider,
                repo_slug=repo_slug,
                repo_id=repo_id,
                pr_state=pr_state,
                head_branch=head_branch,
                thread_target_id=thread_target_id,
                limit=resolved_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"bindings": _serialize_items(bindings), "limit": resolved_limit}

    @router.get("/inspect/reactions")
    def list_scm_reaction_states(
        request: Request,
        binding_id: Optional[str] = None,
        reaction_kind: Optional[str] = None,
        state: Optional[str] = None,
        last_event_id: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        raw_config = _request_raw_config(request)
        _require_scm_automation_enabled(raw_config)
        hub_root = _require_hub_root(request)
        resolved_limit = _resolve_limit(limit, default=_DEFAULT_INSPECT_LIMIT)
        reactions = ScmReactionStateStore(hub_root).list_reaction_states(
            binding_id=binding_id,
            reaction_kind=reaction_kind,
            state=state,
            last_event_id=last_event_id,
            limit=resolved_limit,
        )
        return {"reactions": _serialize_items(reactions), "limit": resolved_limit}

    @router.get("/inspect/publish-operations")
    def list_publish_operations(
        request: Request,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: int = _DEFAULT_INSPECT_LIMIT,
    ) -> dict[str, Any]:
        raw_config = _request_raw_config(request)
        _require_scm_automation_enabled(raw_config)
        hub_root = _require_hub_root(request)
        resolved_limit = _resolve_limit(limit, default=_DEFAULT_INSPECT_LIMIT)
        operations = PublishJournalStore(hub_root).list_operations(
            state=state,
            operation_kind=operation_kind,
            limit=resolved_limit,
            newest_first=True,
        )
        return {"operations": _serialize_items(operations), "limit": resolved_limit}

    @router.post("/webhooks/github")
    async def ingest_github_webhook(request: Request):
        raw_config = _request_raw_config(request)
        if not github_webhook_ingress_enabled(raw_config):
            raise HTTPException(
                status_code=404, detail="GitHub webhook ingress disabled"
            )
        hub_root = _require_hub_root(request)

        ingress = _github_webhook_ingress_config(raw_config)
        max_payload_bytes = _resolve_int(
            ingress.get("max_payload_bytes"),
            default=_DEFAULT_MAX_PAYLOAD_BYTES,
        )
        max_raw_payload_bytes = _resolve_int(
            ingress.get("max_raw_payload_bytes"),
            default=_DEFAULT_MAX_RAW_PAYLOAD_BYTES,
        )
        store_raw_payload = _resolve_bool(
            ingress.get("store_raw_payload"),
            default=False,
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
            config=_resolve_github_webhook_config(raw_config),
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

        store = ScmEventStore(hub_root)
        correlation_id = create_or_preserve_correlation_id(
            provider=event.provider,
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            headers=request.headers,
            payload=event.payload,
        )
        try:
            persisted = store.record_event(
                event_id=event.event_id,
                provider=event.provider,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                received_at=event.received_at,
                repo_slug=event.repo_slug,
                repo_id=event.repo_id,
                pr_number=event.pr_number,
                delivery_id=event.delivery_id,
                correlation_id=correlation_id,
                payload=event.payload,
                raw_payload=event.raw_payload if store_raw_payload else None,
                max_raw_payload_bytes=max_raw_payload_bytes,
            )
        except sqlite3.IntegrityError:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "reason": "duplicate_event",
                    "detail": "SCM event already exists",
                    "event_id": event.event_id,
                },
            )
        except ValueError as exc:
            reason = (
                "raw_payload_too_large"
                if "raw_payload exceeds" in str(exc)
                else "invalid_event"
            )
            detail = (
                "SCM event payload exceeds configured storage limits"
                if reason == "raw_payload_too_large"
                else "SCM event payload could not be processed"
            )
            return JSONResponse(
                status_code=413 if reason == "raw_payload_too_large" else 400,
                content={
                    "status": "rejected",
                    "reason": reason,
                    "detail": detail,
                },
            )

        drained_inline = False
        audit_error: Optional[str] = None
        drain_error: Optional[str] = None
        try:
            ScmAuditRecorder(hub_root).record(
                action_type=SCM_AUDIT_INGEST,
                correlation_id=correlation_id,
                event=persisted,
            )
        except Exception:  # pragma: no cover - defensive logging
            logger = getattr(request.app.state, "logger", None)
            if isinstance(logger, logging.Logger):
                logger.warning(
                    "SCM ingest audit recording failed for %s",
                    persisted.event_id,
                    exc_info=True,
                )
            audit_error = "ingest_audit_failed"
        if _drain_inline_enabled(raw_config):
            try:
                await _run_drain_callback(
                    request=request,
                    event=persisted,
                    route_callback=drain_callback,
                )
                drained_inline = True
            except Exception as exc:  # pragma: no cover - defensive logging
                logger = getattr(request.app.state, "logger", None)
                if isinstance(logger, logging.Logger):
                    logger.warning(
                        "SCM inline drain failed for %s: %s",
                        persisted.event_id,
                        exc,
                        exc_info=True,
                    )
                drain_error = "inline_drain_failed"

        return _compact(
            {
                "status": "accepted",
                "event_id": persisted.event_id,
                "provider": persisted.provider,
                "event_type": persisted.event_type,
                "repo_slug": persisted.repo_slug,
                "repo_id": persisted.repo_id,
                "pr_number": persisted.pr_number,
                "delivery_id": persisted.delivery_id,
                "correlation_id": persisted.correlation_id,
                "drained_inline": drained_inline,
                "audit_error": audit_error,
                "drain_error": drain_error,
            }
        )

    return router


__all__ = [
    "build_scm_webhook_routes",
    "github_automation_enabled",
    "github_webhook_ingress_enabled",
]
