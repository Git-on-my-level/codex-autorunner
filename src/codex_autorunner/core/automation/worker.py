from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import (
    JOB_CANCELLED,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SKIPPED,
    JOB_SUCCEEDED,
    AutomationJob,
    AutomationJobAttempt,
    normalize_timestamp,
)
from .store import AutomationStore


@dataclass(frozen=True)
class AutomationExecutorResult:
    status: str = JOB_SUCCEEDED
    summary: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    execution_refs: dict[str, Any] = field(default_factory=dict)


class AutomationExecutor(Protocol):
    def execute(self, job: AutomationJob) -> AutomationExecutorResult: ...


class AutomationExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, AutomationExecutor] = {}

    def register(self, kind: str, executor: AutomationExecutor) -> None:
        text = str(kind or "").strip()
        if not text:
            raise ValueError("executor kind is required")
        self._executors[text] = executor

    def get(self, kind: str) -> AutomationExecutor:
        text = str(kind or "").strip()
        if text not in self._executors:
            raise KeyError(f"automation executor is not registered: {text}")
        return self._executors[text]


@dataclass(frozen=True)
class WorkerProcessResult:
    claimed: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    cancelled: int = 0
    skipped: int = 0
    paused: int = 0
    escalated: int = 0


class AutomationJobWorker:
    def __init__(
        self,
        store: AutomationStore,
        registry: AutomationExecutorRegistry,
        *,
        worker_id: str | None = None,
        claim_lease_seconds: int = 300,
    ) -> None:
        self._store = store
        self._registry = registry
        self._worker_id = worker_id or f"automation-worker:{uuid.uuid4()}"
        self._claim_lease_seconds = max(1, int(claim_lease_seconds))

    def process_once(
        self, *, now: str | None = None, limit: int = 10
    ) -> WorkerProcessResult:
        stamp = normalize_timestamp(now)
        self._store.release_stale_claims(
            stale_before=_add_seconds(stamp, -self._claim_lease_seconds), now=stamp
        )
        claimed = running_count = succeeded = failed = retried = dead = cancelled = 0
        skipped = 0
        paused = escalated = 0
        for _ in range(max(0, int(limit))):
            job = self._store.claim_next_job(lock_key=self._worker_id, now=stamp)
            if job is None:
                break
            claimed += 1
            latest = self._store.get_job(job.job_id)
            if latest is None or latest.state == JOB_CANCELLED:
                cancelled += 1
                continue
            if self._blocked_by_concurrency(latest):
                self._store.retry_job(
                    latest.job_id, available_at=_add_seconds(stamp, 5)
                )
                skipped += 1
                continue
            kind = str(latest.executor.get("kind") or "").strip()
            attempt_started_at = stamp
            running = self._store.start_job(latest.job_id, now=attempt_started_at)
            try:
                executor = self._registry.get(kind)
                result = executor.execute(running)
                status = result.status or JOB_SUCCEEDED
                if status == JOB_CANCELLED:
                    self._store.cancel_job(running.job_id, now=stamp)
                    cancelled += 1
                elif status == JOB_DEAD_LETTERED:
                    self._store.fail_job(
                        running.job_id,
                        error_text=result.summary or "dead_lettered",
                        dead_letter=True,
                        now=stamp,
                    )
                    dead += 1
                elif status == JOB_SKIPPED:
                    self._store.skip_job(
                        running.job_id,
                        result_summary=result.summary or "skipped",
                    )
                    skipped += 1
                elif status == JOB_PAUSED:
                    self._store.pause_job(
                        running.job_id,
                        result_summary=result.summary or "paused",
                        execution_refs=result.execution_refs,
                        now=stamp,
                    )
                    paused += 1
                elif status == JOB_RUNNING:
                    self._store.update_running_job(
                        running.job_id,
                        result_summary=result.summary,
                        execution_refs=result.execution_refs,
                        now=stamp,
                    )
                    running_count += 1
                elif status == JOB_FAILED:
                    did_escalate = self._handle_failure(
                        running, result.summary or "executor_failed", stamp
                    )
                    failed += 1
                    if running.attempt_count >= running.max_attempts:
                        dead += 1
                        escalated += int(did_escalate)
                    else:
                        retried += 1
                else:
                    self._store.complete_job(
                        running.job_id,
                        result_summary=result.summary,
                        execution_refs=result.execution_refs,
                        now=stamp,
                    )
                    succeeded += 1
                self._store.record_attempt(
                    AutomationJobAttempt.create(
                        job_id=running.job_id,
                        attempt_number=running.attempt_count,
                        status=status,
                        started_at=attempt_started_at,
                        finished_at=stamp,
                        error_text=result.summary if status == JOB_FAILED else None,
                        executor_result=result.data,
                        execution_refs=result.execution_refs,
                    )
                )
            except Exception as exc:
                error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                did_escalate = self._handle_failure(running, error, stamp)
                failed += 1
                if running.attempt_count >= running.max_attempts:
                    dead += 1
                    escalated += int(did_escalate)
                    attempt_status = JOB_DEAD_LETTERED
                else:
                    retried += 1
                    attempt_status = JOB_FAILED
                self._store.record_attempt(
                    AutomationJobAttempt.create(
                        job_id=running.job_id,
                        attempt_number=running.attempt_count,
                        status=attempt_status,
                        started_at=attempt_started_at,
                        finished_at=stamp,
                        error_text=error,
                    )
                )
        return WorkerProcessResult(
            claimed=claimed,
            running=running_count,
            succeeded=succeeded,
            failed=failed,
            retried=retried,
            dead_lettered=dead,
            cancelled=cancelled,
            skipped=skipped,
            paused=paused,
            escalated=escalated,
        )

    def _blocked_by_concurrency(self, job: AutomationJob) -> bool:
        per_rule = int(job.policy.get("max_concurrent_per_rule") or 1)
        per_target = int(job.policy.get("max_concurrent_per_target") or 1)
        if (
            per_rule > 0
            and self._store.count_running_jobs(rule_id=job.rule_id) >= per_rule
        ):
            return True
        if (
            per_target > 0
            and self._store.count_running_jobs(target=job.target) >= per_target
        ):
            return True
        return False

    def _handle_failure(self, job: AutomationJob, error: str, now: str) -> bool:
        if job.attempt_count >= job.max_attempts:
            self._store.fail_job(
                job.job_id, error_text=error, dead_letter=True, now=now
            )
            return self._enqueue_failure_escalation(job, error=error, now=now)
        self._store.fail_job(job.job_id, error_text=error, now=now)
        self._store.retry_job(
            job.job_id,
            available_at=_add_seconds(now, self._retry_delay(job)),
            retry_backoff_seconds=self._retry_delay(job),
        )
        return False

    def _enqueue_failure_escalation(
        self, job: AutomationJob, *, error: str, now: str
    ) -> bool:
        config = job.policy.get("on_failure")
        if not isinstance(config, dict) or not config:
            return False
        executor = config.get("executor")
        if not isinstance(executor, dict):
            return False
        target = config.get("target")
        policy = config.get("policy")
        payload = config.get("payload")
        escalation_job = AutomationJob.create(
            rule_id=str(config.get("rule_id") or job.rule_id),
            event_id=str(config.get("event_id") or job.event_id),
            target=target if isinstance(target, dict) else dict(job.target),
            executor=dict(executor),
            policy=policy if isinstance(policy, dict) else {},
            payload={
                **(payload if isinstance(payload, dict) else {}),
                "failed_job": job.to_dict(),
                "failure_error": error,
            },
            dedupe_key=str(
                config.get("dedupe_key") or f"failure-escalation:{job.job_id}"
            ),
            available_at=str(config.get("available_at") or now),
        )
        _, deduped = self._store.enqueue_job(escalation_job)
        return not deduped

    def _retry_delay(self, job: AutomationJob) -> int:
        base = _policy_int(job.policy.get("retry_backoff_seconds"), fallback=30)
        cap = _policy_int(job.policy.get("retry_backoff_max_seconds"), fallback=300)
        delay = min(max(base, 0) * (2 ** max(0, job.attempt_count - 1)), max(cap, 0))
        return int(delay)


def _add_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(normalize_timestamp(value).replace("Z", "+00:00"))
    return (
        (parsed + timedelta(seconds=seconds))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _policy_int(value: Any, *, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "AutomationExecutor",
    "AutomationExecutorRegistry",
    "AutomationExecutorResult",
    "AutomationJobWorker",
    "WorkerProcessResult",
]
