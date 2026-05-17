from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, List, Optional

from .automation import AutomationEvent, AutomationRuleEngine, RuleEvaluationResult
from .config import HubConfig
from .hub_topology import RepoSnapshot
from .lifecycle_events import LifecycleEvent, LifecycleEventStore, LifecycleEventType
from .pma_dispatch_interceptor import PmaDispatchInterceptor
from .state import now_iso


class LifecycleEventRouter:
    """Owns lifecycle event routing policy: dispatch interception, normalized
    automation event recording, and lifecycle-event acknowledgement.

    ``HubSupervisor`` wires this router with injected callbacks so that routing
    decisions are independently testable without a full hub stack.
    """

    def __init__(
        self,
        *,
        hub_config: HubConfig,
        lifecycle_store: LifecycleEventStore,
        list_repos_fn: Callable[[], List[RepoSnapshot]],
        run_coroutine_fn: Callable[[Any], Any],
        automation_rule_engine: Optional[AutomationRuleEngine] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._hub_config = hub_config
        self._lifecycle_store = lifecycle_store
        self._list_repos_fn = list_repos_fn
        self._run_coroutine_fn = run_coroutine_fn
        self._automation_rule_engine = automation_rule_engine
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
        if event.event_type == LifecycleEventType.DISPATCH_CREATED:
            interceptor = self._ensure_dispatch_interceptor()
            if interceptor is not None:
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
                    evaluation = self._record_automation_event(event)
                    decision = self._automation_decision(
                        evaluation, fallback="dispatch_enqueued"
                    )
                    if not (
                        evaluation is not None
                        and (
                            evaluation.jobs_created
                            or evaluation.jobs_deduped
                            or evaluation.jobs_skipped
                            or evaluation.matched_rules
                        )
                    ):
                        decision = "repo_missing"
                    processed = True
                elif interceptor is not None:
                    result = self._run_coroutine_fn(
                        interceptor.process_dispatch_event(event, repo_snapshot.path)
                    )
                    if result and result.action == "auto_resolved":
                        self._record_automation_event(
                            event,
                            payload_overrides={"pma_dispatch_action": result.action},
                        )
                        decision = "dispatch_auto_resolved"
                        processed = True
                    elif result and result.action == "ignore":
                        self._record_automation_event(
                            event,
                            payload_overrides={"pma_dispatch_action": result.action},
                        )
                        decision = "dispatch_ignored"
                        processed = True
                    else:
                        evaluation = self._record_automation_event(
                            event,
                            payload_overrides={
                                "pma_dispatch_action": (
                                    result.action if result is not None else "escalate"
                                )
                            },
                        )
                        decision = self._automation_decision(
                            evaluation, fallback="dispatch_escalated"
                        )
                        processed = True
            else:
                evaluation = self._record_automation_event(event)
                decision = self._automation_decision(
                    evaluation, fallback="dispatch_enqueued"
                )
                processed = True
        elif event.event_type in (
            LifecycleEventType.FLOW_STARTED,
            LifecycleEventType.FLOW_RESUMED,
            LifecycleEventType.FLOW_PAUSED,
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        ):
            evaluation = self._record_automation_event(event)
            decision = self._automation_decision(evaluation, fallback="flow_enqueued")
            processed = True

        if processed:
            self._lifecycle_store.mark_processed(event_id)
            self._lifecycle_store.prune_processed(keep_last=50)

        self._logger.info(
            "Lifecycle event processed: event_id=%s type=%s repo_id=%s "
            "run_id=%s decision=%s processed=%s",
            event.event_id,
            event.event_type.value,
            event.repo_id,
            event.run_id,
            decision,
            processed,
        )

    def _record_automation_event(
        self,
        event: LifecycleEvent,
        *,
        payload_overrides: Optional[dict[str, Any]] = None,
    ) -> Optional[RuleEvaluationResult]:
        if self._automation_rule_engine is None:
            return None
        event_type = _automation_lifecycle_event_type(event.event_type)
        if event_type is None:
            return None
        data = event.data if isinstance(event.data, dict) else {}
        transition = self._build_transition_payload(event)
        legacy_event_type = data.get("event_type")
        if not isinstance(legacy_event_type, str) or not legacy_event_type.strip():
            legacy_event_type = event.event_type.value
        automation_event = AutomationEvent.create(
            event_id=f"lifecycle:{event.event_id}",
            event_type=event_type,
            source="lifecycle",
            observed_at=event.timestamp,
            repo_id=event.repo_id,
            target={"repo_id": event.repo_id, "run_id": event.run_id},
            payload={
                **dict(data),
                **{k: v for k, v in transition.items() if v is not None},
                **dict(payload_overrides or {}),
                "event_type": legacy_event_type,
                "origin": event.origin,
            },
            raw_payload={
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "repo_id": event.repo_id,
                "run_id": event.run_id,
                "timestamp": event.timestamp,
                "data": dict(data),
                "origin": event.origin,
            },
            metadata={"lifecycle_event_id": event.event_id},
        )
        try:
            return self._automation_rule_engine.record_event_and_enqueue_jobs(
                automation_event
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
            self._logger.exception(
                "Failed to record lifecycle automation event %s",
                event.event_id,
            )
            return None

    def _automation_decision(
        self, evaluation: Optional[RuleEvaluationResult], *, fallback: str
    ) -> str:
        if evaluation is None:
            return "automation_unavailable"
        if evaluation.jobs_created:
            return fallback
        if evaluation.jobs_deduped:
            return "automation_deduped"
        if evaluation.jobs_skipped:
            return "automation_policy_skipped"
        if evaluation.matched_rules:
            return "automation_matched_no_job"
        return "automation_no_matching_rule"

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


def _automation_lifecycle_event_type(
    event_type: LifecycleEventType,
) -> Optional[str]:
    mapping = {
        LifecycleEventType.DISPATCH_CREATED: "lifecycle.dispatch_created",
        LifecycleEventType.FLOW_STARTED: "lifecycle.flow_started",
        LifecycleEventType.FLOW_RESUMED: "lifecycle.flow_resumed",
        LifecycleEventType.FLOW_PAUSED: "lifecycle.flow_paused",
        LifecycleEventType.FLOW_COMPLETED: "lifecycle.flow_completed",
        LifecycleEventType.FLOW_FAILED: "lifecycle.flow_failed",
        LifecycleEventType.FLOW_STOPPED: "lifecycle.flow_stopped",
    }
    return mapping.get(event_type)
