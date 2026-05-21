from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import HTTPException, Request

from .....core.automation import (
    EXECUTOR_MANAGED_THREAD_TURN,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    AutomationRule,
    AutomationStore,
)
from .....core.automation.builtins import _normalize_reactive_event_types
from .....core.automation.models import TARGET_POLICY_HUB, TRIGGER_KIND_EVENT
from .....core.pma_origin import (
    PmaOriginContext,
    merge_pma_origin_metadata,
    resolve_runtime_pma_origin,
)
from .....core.time_utils import now_iso
from ...schemas import ManagedThreadCreateRequest, ManagedThreadMessageRequest
from ...services.pma import get_pma_request_context
from ...services.pma.common import normalize_optional_text


@dataclass(frozen=True)
class ManagedThreadFollowupPolicy:
    enabled: bool
    required: bool
    event_mode: Literal["terminal"] | None
    lane_id: Optional[str]
    notify_once: bool


class ManagedThreadAutomationUnavailable(RuntimeError):
    pass


def build_managed_thread_terminal_notify_payload(
    *,
    managed_thread_id: str,
    lane_id: Optional[str],
    notify_once: bool,
    idempotency_key: Optional[str],
    origin: Optional[PmaOriginContext],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_types": [
            "managed_thread_completed",
            "managed_thread_failed",
            "managed_thread_interrupted",
        ],
        "thread_id": managed_thread_id,
        "lane_id": lane_id,
        "notify_once": notify_once,
        "metadata": merge_pma_origin_metadata(
            {"notify_once": notify_once},
            origin=origin,
        ),
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    if origin and origin.thread_id:
        payload["origin_thread_id"] = origin.thread_id
    if origin and origin.lane_id:
        payload["origin_lane_id"] = origin.lane_id
    if origin and origin.surface_kind:
        payload["origin_surface_kind"] = origin.surface_kind
    if origin and origin.surface_key:
        payload["origin_surface_key"] = origin.surface_key
    return payload


def resolve_managed_thread_followup_policy(
    payload: ManagedThreadCreateRequest | ManagedThreadMessageRequest,
    *,
    default_terminal_followup: bool,
) -> ManagedThreadFollowupPolicy:
    notify_on = payload.notify_on
    terminal_followup = getattr(payload, "terminal_followup", None)
    if terminal_followup is False and notify_on == "terminal":
        raise HTTPException(
            status_code=400,
            detail=(
                "terminal_followup=false cannot be combined with notify_on='terminal'"
            ),
        )

    enabled = False
    if notify_on == "terminal":
        enabled = True
    elif terminal_followup is True:
        enabled = True
    elif (
        getattr(payload, "notify_lane_explicit", False)
        and terminal_followup is not False
    ):
        enabled = True
    elif terminal_followup is not False and default_terminal_followup:
        enabled = True

    return ManagedThreadFollowupPolicy(
        enabled=enabled,
        required=enabled
        and (
            getattr(payload, "notify_on_explicit", False)
            or getattr(payload, "notify_lane_explicit", False)
            or terminal_followup is True
        ),
        event_mode="terminal" if enabled else None,
        lane_id=normalize_optional_text(payload.notify_lane),
        notify_once=bool(payload.notify_once),
    )


class ManagedThreadAutomationClient:
    def __init__(self, request: Request, get_runtime_state) -> None:
        self._request = request
        self._get_runtime_state = get_runtime_state

    async def create_terminal_followup(
        self,
        *,
        managed_thread_id: str,
        lane_id: Optional[str],
        notify_once: bool,
        idempotency_key: Optional[str],
        required: bool,
    ) -> Optional[dict[str, Any]]:
        runtime_state = self._get_runtime_state() if self._get_runtime_state else None
        origin = resolve_runtime_pma_origin(runtime_state)
        if not required and origin is None:
            return None
        try:
            context = get_pma_request_context(self._request)
            created = _create_terminal_followup_rule(
                AutomationStore(context.hub_root),
                build_managed_thread_terminal_notify_payload(
                    managed_thread_id=managed_thread_id,
                    lane_id=lane_id,
                    notify_once=notify_once,
                    idempotency_key=idempotency_key,
                    origin=origin,
                ),
            )
        except HTTPException as exc:
            if not required:
                return None
            if exc.status_code in {503}:
                raise ManagedThreadAutomationUnavailable(
                    "Automation action unavailable"
                ) from exc
            raise
        except TypeError as exc:
            if not required:
                return None
            raise ManagedThreadAutomationUnavailable(
                "Automation action unavailable"
            ) from exc
        return {"mode": "terminal", "subscription": created, "deduped": False}


def apply_origin_followup_context(
    payload: dict[str, Any],
    runtime_state: Any,
) -> dict[str, Any]:
    resolved_payload = dict(payload)
    origin = resolve_runtime_pma_origin(runtime_state)
    if origin is None:
        return resolved_payload
    if origin.thread_id:
        resolved_payload.setdefault("origin_thread_id", origin.thread_id)
    if origin.lane_id:
        resolved_payload.setdefault("origin_lane_id", origin.lane_id)
    if origin.surface_kind:
        resolved_payload.setdefault("origin_surface_kind", origin.surface_kind)
    if origin.surface_key:
        resolved_payload.setdefault("origin_surface_key", origin.surface_key)
    resolved_payload["metadata"] = merge_pma_origin_metadata(
        (
            resolved_payload.get("metadata")
            if isinstance(resolved_payload.get("metadata"), dict)
            else None
        ),
        origin=origin,
    )
    return resolved_payload


def _create_terminal_followup_rule(
    store: AutomationStore, payload: dict[str, Any]
) -> dict[str, Any]:
    idempotency_key = normalize_optional_text(payload.get("idempotency_key"))
    if idempotency_key is not None:
        for rule in store.list_rules():
            if rule.metadata.get("purpose") not in {
                "managed_thread_lifecycle_subscription",
                "pma_lifecycle_subscription",
            }:
                continue
            if rule.metadata.get("legacy_idempotency_key") == idempotency_key:
                return _subscription_row_from_rule(rule)

    stamp = now_iso()
    subscription_id = normalize_optional_text(payload.get("subscription_id")) or str(
        uuid.uuid4()
    )
    thread_id = normalize_optional_text(payload.get("thread_id"))
    lane_id = normalize_optional_text(payload.get("lane_id")) or "pma:default"
    event_types = _normalize_reactive_event_types(payload.get("event_types") or [])
    metadata_raw = payload.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    rule = AutomationRule.create(
        rule_id=f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}",
        name=f"Managed-thread subscription {subscription_id}",
        enabled=True,
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"kind": "lifecycle_event", "event_types": event_types},
        filters={"event.payload.thread_id": thread_id} if thread_id else {},
        target_policy=TARGET_POLICY_HUB,
        target={"thread_id": thread_id},
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
        executor={
            "wake_up_kind": "managed_thread_subscription",
            "source": "transition",
            "subscription_id": subscription_id,
            "lane_id": lane_id,
            "thread_id": "{{ event.payload.thread_id }}",
            "message_text": (
                "Automation wake-up received.\n"
                "source: transition\n"
                f"subscription_id: {subscription_id}\n"
                "thread_id: {{ event.payload.thread_id }}\n"
                "suggested_next_action: inspect the terminal managed-thread transition."
            ),
        },
        policy={
            "dedupe_key": (
                f"managed-thread-subscription:{subscription_id}:" "{{ event.event_id }}"
            ),
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "builtin": True,
            "purpose": "managed_thread_lifecycle_subscription",
            "legacy_subscription_id": subscription_id,
            "legacy_idempotency_key": idempotency_key,
            "legacy_max_matches": 1 if bool(payload.get("notify_once")) else None,
            "legacy_match_count": 0,
            "legacy_metadata": metadata,
        },
        created_at=stamp,
        updated_at=stamp,
    )
    store.upsert_rule(rule)
    return _subscription_row_from_rule(rule)


def _subscription_row_from_rule(rule: AutomationRule) -> dict[str, Any]:
    executor = rule.executor if isinstance(rule.executor, dict) else {}
    target = rule.target if isinstance(rule.target, dict) else {}
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    return {
        "subscription_id": rule.metadata.get("legacy_subscription_id")
        or rule.rule_id.removeprefix(PMA_SUBSCRIPTION_RULE_PREFIX),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
        "state": "active" if rule.enabled else "cancelled",
        "event_types": list(rule.trigger.get("event_types") or []),
        "repo_id": target.get("repo_id") or filters.get("event.repo_id"),
        "run_id": target.get("run_id") or filters.get("event.payload.run_id"),
        "thread_id": target.get("thread_id") or filters.get("event.payload.thread_id"),
        "lane_id": executor.get("lane_id") or "pma:default",
        "from_state": filters.get("event.payload.from_state"),
        "to_state": filters.get("event.payload.to_state"),
        "reason": rule.metadata.get("legacy_reason"),
        "idempotency_key": rule.metadata.get("legacy_idempotency_key"),
        "max_matches": rule.metadata.get("legacy_max_matches"),
        "match_count": rule.metadata.get("legacy_match_count") or 0,
        "metadata": dict(rule.metadata.get("legacy_metadata") or {}),
    }
