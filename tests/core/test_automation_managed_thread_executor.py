from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationExecutorRegistry,
    AutomationJob,
    AutomationJobWorker,
    AutomationRule,
    AutomationStore,
    ManagedThreadTurnAutomationExecutor,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_MANAGED_THREAD_TURN,
    JOB_DEAD_LETTERED,
    JOB_PAUSED,
    JOB_RUNNING,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.managed_thread_store import ManagedThreadStore


def _store_rule_event(
    tmp_path: Path,
) -> tuple[AutomationStore, ManagedThreadStore, str]:
    hub_root = tmp_path / "hub"
    store = AutomationStore(hub_root)
    threads = ManagedThreadStore(hub_root)
    thread = threads.create_thread("codex", tmp_path)
    thread_id = str(thread["managed_thread_id"])
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Managed turn",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(
            event_id="event-1",
            event_type="manual.run",
            payload={"name": "Ada"},
        )
    )
    return store, threads, thread_id


def _job(thread_id: str, **kwargs) -> AutomationJob:
    args = {
        "job_id": "job-1",
        "rule_id": "rule-1",
        "event_id": "event-1",
        "target": {"thread_target_id": thread_id},
        "executor": {
            "kind": EXECUTOR_MANAGED_THREAD_TURN,
            "prompt": "Say hi to {{ event.payload.name }}",
            "client_turn_id": "client-1",
            "model": "gpt-test",
            "reasoning": "low",
        },
        "available_at": "2026-01-01T00:00:00Z",
    }
    args.update(kwargs)
    return AutomationJob.create(**args)


def test_managed_thread_turn_creates_turn_and_refs(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    started_workers: list[str] = []
    store.enqueue_job(_job(thread_id, policy={"approval_mode": "inherit_profile"}))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
            queue_worker_starter_fn=started_workers.append,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert result.succeeded == 0
    assert saved.state == JOB_RUNNING
    assert saved.managed_thread_target_id == thread_id
    turn = threads.get_turn(thread_id, str(saved.managed_thread_execution_id))
    assert turn["prompt"] == "Say hi to Ada"
    assert turn["client_turn_id"] == "client-1"
    assert turn["status"] == "queued"
    assert [
        entry["managed_turn_id"] for entry in threads.list_queued_turns(thread_id)
    ] == [str(saved.managed_thread_execution_id)]
    assert started_workers == [thread_id]


def test_managed_thread_turn_creates_automation_thread_when_target_is_repo(
    tmp_path: Path,
) -> None:
    store, threads, _thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(
        _job(
            "",
            target={"repo_id": "repo-1"},
            executor={
                "kind": EXECUTOR_MANAGED_THREAD_TURN,
                "message_text": "Inspect {{ target.repo_id }}",
                "agent": "codex",
                "profile": "automation",
                "client_turn_id": "client-1",
            },
            policy={"approval_mode": "inherit_profile"},
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    assert result.running == 1
    assert saved.managed_thread_target_id
    thread = threads.get_thread(str(saved.managed_thread_target_id))
    assert thread["repo_id"] == "repo-1"
    assert thread["metadata"]["automation_job_id"] == "job-1"
    turn = threads.get_turn(
        str(saved.managed_thread_target_id), str(saved.managed_thread_execution_id)
    )
    assert turn["prompt"] == "Inspect repo-1"


def test_managed_thread_default_approval_pauses_unattended_job(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(_job(thread_id))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.paused == 1
    assert store.get_job("job-1").state == JOB_PAUSED
    assert threads.list_turns(thread_id) == []


def test_managed_thread_auto_decline_dead_letters_and_escalates(tmp_path: Path) -> None:
    store, threads, thread_id = _store_rule_event(tmp_path)
    store.enqueue_job(
        _job(
            thread_id,
            policy={
                "max_attempts": 1,
                "approval_mode": "auto_decline",
                "on_failure": {
                    "executor": {
                        "kind": EXECUTOR_MANAGED_THREAD_TURN,
                        "message_text": "Escalate failed automation",
                    },
                    "target": {"thread_target_id": thread_id},
                },
            },
            max_attempts=1,
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_MANAGED_THREAD_TURN,
        ManagedThreadTurnAutomationExecutor(
            hub_root=tmp_path / "hub",
            automation_store=store,
            thread_store=threads,
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    assert result.dead_lettered == 1
    assert result.escalated == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED
    escalations = [job for job in store.list_jobs() if job.job_id != "job-1"]
    assert len(escalations) == 1
    assert escalations[0].executor["kind"] == EXECUTOR_MANAGED_THREAD_TURN
