from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import HTTPException, Request

from .....core.pma_automation_store import PmaAutomationThreadNotFoundError
from ...routes.pma_routes.automation_adapter import (
    call_store_create_with_payload,
    get_automation_store,
)
from ...schemas import PmaManagedThreadCreateRequest, PmaManagedThreadMessageRequest
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
        "metadata": {"notify_once": notify_once},
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return payload


def resolve_managed_thread_followup_policy(
    payload: PmaManagedThreadCreateRequest | PmaManagedThreadMessageRequest,
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
        try:
            store = await get_automation_store(
                self._request,
                runtime_state,
                required=required,
            )
            if store is None:
                return None
            created = await call_store_create_with_payload(
                store,
                (
                    "create_subscription",
                    "upsert_subscription",
                ),
                build_managed_thread_terminal_notify_payload(
                    managed_thread_id=managed_thread_id,
                    lane_id=lane_id,
                    notify_once=notify_once,
                    idempotency_key=idempotency_key,
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
        except PmaAutomationThreadNotFoundError:
            if not required:
                return None
            raise

        if isinstance(created, dict) and "subscription" in created:
            return {"mode": "terminal", **created}
        return {"mode": "terminal", "subscription": created}
