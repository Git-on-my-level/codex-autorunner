from __future__ import annotations

from codex_autorunner.core.automation.execution_graph import (
    automation_execution_snapshot,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AutomationChildExecutionEdge,
    AutomationJob,
    AutomationRuntimeContract,
)


def test_automation_execution_snapshot_uses_latest_agent_task_child_edge() -> None:
    job = AutomationJob.create(
        job_id="job-retry",
        rule_id="rule-1",
        event_id="event-1",
        target={"repo_id": "repo-1"},
        executor={"kind": "agent_task_turn"},
        available_at="2026-01-01T00:00:00Z",
    )
    edges = [
        AutomationChildExecutionEdge.create(
            parent_job_id="job-retry",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="turn-older",
            requested_runtime=AutomationRuntimeContract(
                agent="codex",
                workspace_scope={"thread_target_id": "thread-old"},
            ),
            created_at="2026-01-01T00:00:00Z",
        ),
        AutomationChildExecutionEdge.create(
            parent_job_id="job-retry",
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id="turn-newer",
            requested_runtime=AutomationRuntimeContract(
                agent="codex",
                workspace_scope={"thread_target_id": "thread-new"},
            ),
            created_at="2026-01-01T00:05:00Z",
        ),
    ]
    snapshot = automation_execution_snapshot(job, hub_root=None, child_edges=edges)
    assert snapshot.managed_thread is not None
    assert snapshot.managed_thread["execution_id"] == "turn-newer"
    assert snapshot.managed_thread["thread_target_id"] == "thread-new"
