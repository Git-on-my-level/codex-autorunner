from __future__ import annotations

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationExecutorRegistry,
    AutomationJob,
    AutomationJobWorker,
    AutomationRule,
    AutomationStore,
    PublishOperationAutomationExecutor,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_DEAD_LETTERED,
    JOB_PENDING,
    JOB_SUCCEEDED,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.publish_executor import (
    PublishExecutorRegistry,
    RetryablePublishError,
    TerminalPublishError,
)
from codex_autorunner.core.publish_journal import PublishJournalStore


def _store(tmp_path) -> AutomationStore:
    store = AutomationStore(tmp_path)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Publish",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_PUBLISH_OPERATION,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-1", event_type="manual.run")
    )
    return store


def _job(**kwargs) -> AutomationJob:
    args = {
        "job_id": "job-1",
        "rule_id": "rule-1",
        "event_id": "event-1",
        "target": {"repo_id": "repo-1"},
        "executor": {
            "kind": EXECUTOR_PUBLISH_OPERATION,
            "operation_kind": "notify_chat",
            "operation_key": "notify:1",
            "payload": {"message": "hello", "delivery": "none"},
        },
        "available_at": "2026-01-01T00:00:00Z",
    }
    args.update(kwargs)
    return AutomationJob.create(**args)


def test_publish_operation_job_uses_journal_and_refs(tmp_path) -> None:
    store = _store(tmp_path)
    journal = PublishJournalStore(tmp_path)
    calls = []

    def publish(operation):
        calls.append(operation.operation_id)
        return {"remote_id": "msg-1"}

    store.enqueue_job(_job())
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PUBLISH_OPERATION,
        PublishOperationAutomationExecutor(
            hub_root=tmp_path,
            journal_store=journal,
            automation_store=store,
            executor_registry=PublishExecutorRegistry({"notify_chat": publish}),
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    edge = store.list_child_execution_edges("job-1")[0]
    assert result.succeeded == 1
    assert saved.state == JOB_SUCCEEDED
    assert edge.child_kind == AUTOMATION_CHILD_KIND_PUBLISH_OPERATION
    assert journal.get_operation(edge.child_id).state == "succeeded"
    assert len(calls) == 1

    duplicate, deduped = journal.create_operation(
        operation_key="notify:1",
        operation_kind="notify_chat",
        payload={"message": "changed"},
    )
    assert deduped is True
    assert duplicate.operation_id == edge.child_id


def test_publish_operation_retry_then_dead_letter(tmp_path) -> None:
    store = _store(tmp_path)
    journal = PublishJournalStore(tmp_path)
    calls = 0

    def publish(operation):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryablePublishError("try later", retry_after_seconds=0)
        raise TerminalPublishError("nope")

    store.enqueue_job(_job(policy={"max_attempts": 2}, max_attempts=2))
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PUBLISH_OPERATION,
        PublishOperationAutomationExecutor(
            hub_root=tmp_path,
            journal_store=journal,
            automation_store=store,
            executor_registry=PublishExecutorRegistry({"notify_chat": publish}),
        ),
    )
    worker = AutomationJobWorker(store, registry)

    first = worker.process_once(now="2026-01-01T00:00:00Z")
    assert first.retried == 1
    assert store.get_job("job-1").state == JOB_PENDING

    second = worker.process_once(now=store.get_job("job-1").available_at)
    assert second.dead_lettered == 1
    assert store.get_job("job-1").state == JOB_DEAD_LETTERED


def test_publish_chat_notification_maps_to_notify_chat(tmp_path) -> None:
    store = _store(tmp_path)
    journal = PublishJournalStore(tmp_path)
    store.enqueue_job(
        _job(
            executor={
                "kind": EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
                "operation_key": "chat:1",
                "message": "done",
                "delivery": "none",
            }
        )
    )
    registry = AutomationExecutorRegistry()
    registry.register(
        EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
        PublishOperationAutomationExecutor(
            hub_root=tmp_path,
            journal_store=journal,
            automation_store=store,
            executor_registry=PublishExecutorRegistry(
                {"notify_chat": lambda _operation: {"published": 0}}
            ),
        ),
    )

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    edge = store.list_child_execution_edges("job-1")[0]
    operation = journal.get_operation(edge.child_id)
    assert result.succeeded == 1
    assert operation.operation_kind == "notify_chat"
