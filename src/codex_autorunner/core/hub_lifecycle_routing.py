from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, List, Optional

from .config import HubConfig
from .hub_topology import RepoSnapshot
from .lifecycle_events import LifecycleEvent, LifecycleEventStore, LifecycleEventType
from .pma_automation_store import PmaAutomationStore
from .pma_dispatch_interceptor import PmaDispatchInterceptor
from .pma_queue import PmaQueue
from .pma_reactive import PmaReactiveStore
from .pma_safety import PmaSafetyChecker
from .state import now_iso


class LifecycleEventRouter:
    """Owns lifecycle event routing policy: dispatch interception, reactive
    gating, automation wakeup enqueueing, and lifecycle-event acknowledgement.

    ``HubSupervisor`` wires this router with injected callbacks so that routing
    decisions are independently testable without a full hub stack.
    """

    def __init__(
        self,
        *,
        hub_config: HubConfig,
        lifecycle_store: LifecycleEventStore,
        list_repos_fn: Callable[[], List[RepoSnapshot]],
        ensure_pma_automation_store_fn: Callable[[], PmaAutomationStore],
        ensure_pma_safety_checker_fn: Callable[[], PmaSafetyChecker],
        run_coroutine_fn: Callable[[Any], Any],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._hub_config = hub_config
        self._lifecycle_store = lifecycle_store
        self._list_repos_fn = list_repos_fn
        self._ensure_pma_automation_store_fn = ensure_pma_automation_store_fn
        self._ensure_pma_safety_checker_fn = ensure_pma_safety_checker_fn
        self._run_coroutine_fn = run_coroutine_fn
        self._logger = logger or logging.getLogger("codex_autorunner.hub")
        self._dispatch_interceptor: Optional[PmaDispatchInterceptor] = None

    def route_event(self, event: LifecycleEvent) -> None:
        if event.processed:
            return
        event_id = event.event_id
        if not event_id:
            return

        decision = "skip"
        processed = False
        automation_wakeups = 0
        try:
            automation_wakeups = self._enqueue_automation_wakeups(event)
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
            self._logger.exception(
                "Failed to enqueue lifecycle automation wake-ups for event %s",
                event.event_id,
            )
            automation_wakeups = 0

        if event.event_type == LifecycleEventType.DISPATCH_CREATED:
            if not self._hub_config.pma.enabled:
                decision = "pma_disabled"
                processed = True
            else:
                interceptor = self._ensure_dispatch_interceptor()
                repo_snapshot = None
                try:
                    snapshots = self._list_repos_fn()
                    for snap in snapshots:
                        if snap.id == event.repo_id:
                            repo_snapshot = snap
                            break
                except (RuntimeError, OSError, ValueError, TypeError):
                    self._logger.exception(
                        "Failed to get repo snapshot for repo_id=%s", event.repo_id
                    )
                    repo_snapshot = None

                if repo_snapshot is None or not repo_snapshot.exists_on_disk:
                    decision = "repo_missing"
                    processed = True
                elif interceptor is not None:
                    result = self._run_coroutine_fn(
                        interceptor.process_dispatch_event(event, repo_snapshot.path)
                    )
                    if result and result.action == "auto_resolved":
                        decision = "dispatch_auto_resolved"
                        processed = True
                    elif result and result.action == "ignore":
                        decision = "dispatch_ignored"
                        processed = True
                    else:
                        allowed, gate_reason = self.check_reactive_gate(event)
                        if not allowed:
                            decision = gate_reason
                            processed = True
                        else:
                            decision = "dispatch_escalated"
                            processed = self._enqueue_pma_for_event(
                                event, reason="dispatch_escalated"
                            )
                else:
                    allowed, gate_reason = self.check_reactive_gate(event)
                    if not allowed:
                        decision = gate_reason
                        processed = True
                    else:
                        decision = "dispatch_enqueued"
                        processed = self._enqueue_pma_for_event(
                            event, reason="dispatch_created"
                        )
        elif event.event_type in (
            LifecycleEventType.FLOW_STARTED,
            LifecycleEventType.FLOW_RESUMED,
            LifecycleEventType.FLOW_PAUSED,
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        ):
            if not self._hub_config.pma.enabled:
                decision = "pma_disabled"
                processed = True
            else:
                allowed, gate_reason = self.check_reactive_gate(event)
                if not allowed:
                    decision = gate_reason
                    processed = True
                else:
                    decision = "flow_enqueued"
                    processed = self._enqueue_pma_for_event(
                        event, reason=event.event_type.value
                    )

        if processed:
            self._lifecycle_store.mark_processed(event_id)
            self._lifecycle_store.prune_processed(keep_last=50)

        self._logger.info(
            "Lifecycle event processed: event_id=%s type=%s repo_id=%s "
            "run_id=%s decision=%s processed=%s automation_wakeups=%s",
            event.event_id,
            event.event_type.value,
            event.repo_id,
            event.run_id,
            decision,
            processed,
            automation_wakeups,
        )

    def check_reactive_gate(self, event: LifecycleEvent) -> tuple[bool, str]:
        pma = self._hub_config.pma
        reactive_enabled = getattr(pma, "reactive_enabled", True)
        if not reactive_enabled:
            return False, "reactive_disabled"

        origin = (event.origin or "").strip().lower()
        blocked_origins = getattr(pma, "reactive_origin_blocklist", [])
        if blocked_origins:
            blocked = {str(value).strip().lower() for value in blocked_origins}
            if origin and origin in blocked:
                self._logger.info(
                    "Skipping PMA reactive trigger for event %s due to origin=%s",
                    event.event_id,
                    origin,
                )
                return False, "reactive_origin_blocked"

        allowlist = getattr(pma, "reactive_event_types", None)
        if allowlist:
            if event.event_type.value not in set(allowlist):
                return False, "reactive_filtered"

        debounce_seconds = int(getattr(pma, "reactive_debounce_seconds", 0) or 0)
        if debounce_seconds > 0:
            key = f"{event.event_type.value}:{event.repo_id}:{event.run_id}"
            store = PmaReactiveStore(self._hub_config.root)
            if not store.check_and_update(key, debounce_seconds):
                return False, "reactive_debounced"

        safety_checker = self._ensure_pma_safety_checker_fn()
        safety_check = safety_checker.check_reactive_turn()
        if not safety_check.allowed:
            self._logger.info(
                "Blocked PMA reactive trigger for event %s: %s",
                event.event_id,
                safety_check.reason,
            )
            return False, safety_check.reason or "reactive_blocked"

        return True, "reactive_allowed"

    def _build_transition_payload(
        self, event: LifecycleEvent
    ) -> dict[str, Optional[str]]:
        data = event.data if isinstance(event.data, dict) else {}
        to_state_fallback = {
            LifecycleEventType.FLOW_STARTED: "running",
            LifecycleEventType.FLOW_RESUMED: "running",
            LifecycleEventType.FLOW_PAUSED: "blocked",
            LifecycleEventType.FLOW_COMPLETED: "completed",
            LifecycleEventType.FLOW_FAILED: "failed",
            LifecycleEventType.FLOW_STOPPED: "stopped",
            LifecycleEventType.DISPATCH_CREATED: "dispatch_created",
        }
        from_state = (
            str(data.get("from_state")).strip()
            if isinstance(data.get("from_state"), str)
            else None
        )
        to_state = (
            str(data.get("to_state")).strip()
            if isinstance(data.get("to_state"), str)
            else to_state_fallback.get(event.event_type)
        )
        if from_state is None:
            if event.event_type == LifecycleEventType.DISPATCH_CREATED:
                from_state = "paused"
            elif event.event_type == LifecycleEventType.FLOW_STARTED:
                from_state = "pending"
            elif event.event_type == LifecycleEventType.FLOW_RESUMED:
                from_state = "paused"
            elif to_state in {"paused", "blocked", "completed", "failed", "stopped"}:
                from_state = "running"

        reason = (
            str(data.get("reason")).strip()
            if isinstance(data.get("reason"), str) and str(data.get("reason")).strip()
            else event.event_type.value
        )
        timestamp = (
            str(event.timestamp).strip()
            if isinstance(event.timestamp, str) and event.timestamp.strip()
            else now_iso()
        )
        thread_id = (
            str(data.get("thread_id")).strip()
            if isinstance(data.get("thread_id"), str)
            and str(data.get("thread_id")).strip()
            else None
        )
        repo_id = (
            event.repo_id.strip()
            if isinstance(event.repo_id, str) and event.repo_id.strip()
            else (
                str(data.get("repo_id")).strip()
                if isinstance(data.get("repo_id"), str)
                and str(data.get("repo_id")).strip()
                else None
            )
        )
        run_id = (
            event.run_id.strip()
            if isinstance(event.run_id, str) and event.run_id.strip()
            else (
                str(data.get("run_id")).strip()
                if isinstance(data.get("run_id"), str)
                and str(data.get("run_id")).strip()
                else None
            )
        )
        return {
            "repo_id": repo_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "timestamp": timestamp,
        }

    def _enqueue_automation_wakeups(self, event: LifecycleEvent) -> int:
        transition = self._build_transition_payload(event)
        try:
            store = self._ensure_pma_automation_store_fn()
            result = store.notify_transition(
                event_type=event.event_type.value,
                repo_id=transition.get("repo_id"),
                run_id=transition.get("run_id"),
                thread_id=transition.get("thread_id"),
                from_state=transition.get("from_state"),
                to_state=transition.get("to_state"),
                reason=transition.get("reason"),
                timestamp=transition.get("timestamp"),
                transition_id=event.event_id,
                event_id=event.event_id,
                origin=event.origin,
                event_data=event.data if isinstance(event.data, dict) else {},
                prefer_subscription_reason=True,
            )
            return int(result.get("created", 0) or 0)
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
            self._logger.exception(
                "Failed to enqueue lifecycle automation wake-ups for event %s",
                event.event_id,
            )
            return 0

    def _enqueue_pma_for_event(self, event: LifecycleEvent, *, reason: str) -> bool:
        if not self._hub_config.pma.enabled:
            return False

        async def _enqueue() -> tuple[object, Optional[str]]:
            queue = PmaQueue(self._hub_config.root)
            message = self._build_lifecycle_message(event, reason=reason)
            payload = {
                "message": message,
                "agent": None,
                "model": None,
                "reasoning": None,
                "client_turn_id": event.event_id,
                "stream": False,
                "hub_root": str(self._hub_config.root),
                "lifecycle_event": {
                    "event_id": event.event_id,
                    "event_type": event.event_type.value,
                    "repo_id": event.repo_id,
                    "run_id": event.run_id,
                    "timestamp": event.timestamp,
                    "data": event.data,
                    "origin": event.origin,
                },
            }
            idempotency_key = f"lifecycle:{event.event_id}"
            return await queue.enqueue("pma:default", idempotency_key, payload)

        _, dupe_reason = self._run_coroutine_fn(_enqueue())
        if dupe_reason:
            self._logger.info(
                "Deduped PMA queue item for lifecycle event %s: %s",
                event.event_id,
                dupe_reason,
            )
        return True

    def _build_lifecycle_message(self, event: LifecycleEvent, *, reason: str) -> str:
        lines = [
            "Lifecycle event received.",
            f"type: {event.event_type.value}",
            f"repo_id: {event.repo_id}",
            f"run_id: {event.run_id}",
            f"event_id: {event.event_id}",
        ]
        if reason:
            lines.append(f"reason: {reason}")
        if event.data:
            try:
                payload = json.dumps(event.data, sort_keys=True, ensure_ascii=True)
            except (TypeError, ValueError):
                payload = str(event.data)
            lines.append(f"data: {payload}")
        if event.event_type == LifecycleEventType.DISPATCH_CREATED:
            lines.append("Dispatch requires attention; check the repo inbox.")
        return "\n".join(lines)

    def _on_dispatch_intercept(self, event_id: str, result: Any) -> None:
        self._logger.info(
            "Dispatch intercepted: event_id=%s action=%s reason=%s",
            event_id,
            (
                result.get("action")
                if isinstance(result, dict)
                else getattr(result, "action", None)
            ),
            (
                result.get("reason")
                if isinstance(result, dict)
                else getattr(result, "reason", None)
            ),
        )

    def _ensure_dispatch_interceptor(
        self,
    ) -> Optional[PmaDispatchInterceptor]:
        if not self._hub_config.pma.enabled:
            return None
        if not self._hub_config.pma.dispatch_interception_enabled:
            return None
        if self._dispatch_interceptor is None:
            self._dispatch_interceptor = PmaDispatchInterceptor(
                hub_root=self._hub_config.root,
                supervisor=None,
                on_intercept=self._on_dispatch_intercept,
            )
        return self._dispatch_interceptor
