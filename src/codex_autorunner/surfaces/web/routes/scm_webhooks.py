from __future__ import annotations

import inspect
import os
import sqlite3
from typing import Any, Callable, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ....core.scm_events import ScmEvent, ScmEventStore
from ....integrations.github import GitHubWebhookConfig, normalize_github_webhook

ScmDrainCallback = Callable[[Request, ScmEvent], object]
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


def github_webhook_ingress_enabled(raw_config: object) -> bool:
    automation = _github_automation_config(raw_config)
    ingress = _github_webhook_ingress_config(raw_config)
    return bool(automation.get("enabled")) and bool(ingress.get("enabled"))


def _resolve_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


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
        return
    result = callback(request, event)
    if inspect.isawaitable(result):
        await result


def build_scm_webhook_routes(
    *, drain_callback: Optional[ScmDrainCallback] = None
) -> APIRouter:
    router = APIRouter(prefix="/hub/scm/webhooks", tags=["scm"])

    @router.post("/github")
    async def ingest_github_webhook(request: Request):
        config = getattr(request.app.state, "config", None)
        raw_config = getattr(config, "raw", {})
        if not github_webhook_ingress_enabled(raw_config):
            raise HTTPException(
                status_code=404, detail="GitHub webhook ingress disabled"
            )
        hub_root = getattr(config, "root", None)
        if hub_root is None:
            raise HTTPException(status_code=503, detail="Hub config unavailable")

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
            detail = str(exc)
            reason = (
                "raw_payload_too_large"
                if "raw_payload exceeds" in detail
                else "invalid_event"
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
        if _drain_inline_enabled(raw_config):
            await _run_drain_callback(
                request=request,
                event=persisted,
                route_callback=drain_callback,
            )
            drained_inline = True

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
                "drained_inline": drained_inline,
            }
        )

    return router


__all__ = ["build_scm_webhook_routes", "github_webhook_ingress_enabled"]
