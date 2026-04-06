from __future__ import annotations

import logging
from typing import Any

from .hub import HubSupervisor

_logger = logging.getLogger(__name__)


def _coerce_automation_items(payload: Any, *, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [entry for entry in candidate if isinstance(entry, dict)]
    return []


def _call_automation_list(
    method: Any, *, key: str, **kwargs: Any
) -> list[dict[str, Any]]:
    if not callable(method):
        return []
    try:
        result = method(**kwargs)
    except TypeError:
        try:
            result = method()
        except (TypeError, RuntimeError, ValueError):
            return []
    except (RuntimeError, ValueError):
        return []
    return _coerce_automation_items(result, key=key)


def snapshot_pma_automation(
    supervisor: HubSupervisor, *, max_items: int = 10
) -> dict[str, Any]:
    out = {
        "subscriptions": {"active_count": 0, "sample": []},
        "timers": {"pending_count": 0, "sample": []},
        "wakeups": {
            "pending_count": 0,
            "dispatched_recent_count": 0,
            "pending_sample": [],
        },
    }
    try:
        store = supervisor.pma_automation_store
    except (AttributeError, RuntimeError, TypeError):
        return out

    subscriptions = _call_automation_list(
        getattr(store, "list_subscriptions", None), key="subscriptions"
    )
    subscriptions_sample = _call_automation_list(
        getattr(store, "list_subscriptions", None),
        key="subscriptions",
        limit=max_items,
    )
    timers = _call_automation_list(getattr(store, "list_timers", None), key="timers")
    timers_sample = _call_automation_list(
        getattr(store, "list_timers", None),
        key="timers",
        limit=max_items,
    )
    pending_wakeups = _call_automation_list(
        getattr(store, "list_wakeups", None), key="wakeups", state_filter="pending"
    )
    pending_wakeups_sample = _call_automation_list(
        getattr(store, "list_pending_wakeups", None),
        key="wakeups",
        limit=max_items,
    )
    if not pending_wakeups:
        pending_wakeups = _call_automation_list(
            getattr(store, "list_pending_wakeups", None), key="wakeups"
        )
    if not pending_wakeups_sample:
        pending_wakeups_sample = _call_automation_list(
            getattr(store, "list_wakeups", None),
            key="wakeups",
            state_filter="pending",
            limit=max_items,
        )
    dispatched_wakeups = _call_automation_list(
        getattr(store, "list_wakeups", None),
        key="wakeups",
        state_filter="dispatched",
    )

    def _pick(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for field in fields:
            value = entry.get(field)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            picked[field] = value
        return picked

    out["subscriptions"] = {
        "active_count": len(subscriptions),
        "sample": [
            _pick(
                entry,
                (
                    "subscription_id",
                    "event_types",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "from_state",
                    "to_state",
                    "reason",
                ),
            )
            for entry in subscriptions_sample[:max_items]
        ],
    }
    out["timers"] = {
        "pending_count": len(timers),
        "sample": [
            _pick(
                entry,
                (
                    "timer_id",
                    "timer_type",
                    "due_at",
                    "idle_seconds",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "reason",
                ),
            )
            for entry in timers_sample[:max_items]
        ],
    }
    out["wakeups"] = {
        "pending_count": len(pending_wakeups),
        "dispatched_recent_count": len(dispatched_wakeups),
        "pending_sample": [
            _pick(
                entry,
                (
                    "wakeup_id",
                    "source",
                    "event_type",
                    "subscription_id",
                    "timer_id",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "from_state",
                    "to_state",
                    "reason",
                    "timestamp",
                ),
            )
            for entry in pending_wakeups_sample[:max_items]
        ],
    }
    return out
