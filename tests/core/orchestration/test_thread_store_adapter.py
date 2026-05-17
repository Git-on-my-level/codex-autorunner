from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration.thread_store_adapter import (
    ManagedThreadExecutionStore,
)


def test_managed_thread_execution_store_projects_runtime_binding(
    tmp_path: Path,
) -> None:
    store = ManagedThreadExecutionStore(ManagedThreadStore(tmp_path / "hub"))
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    created = store.create_thread_target(
        "codex",
        workspace_root,
        repo_id="repo-1",
        backend_thread_id="backend-thread-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )

    fetched = store.get_thread_target(created.thread_target_id)
    listed = store.list_thread_targets(repo_id="repo-1")

    assert fetched is not None
    assert fetched.backend_thread_id == "backend-thread-1"
    assert fetched.backend_runtime_instance_id == "runtime-1"
    assert [thread.thread_target_id for thread in listed] == [created.thread_target_id]
    assert listed[0].backend_thread_id == "backend-thread-1"
    assert listed[0].backend_runtime_instance_id == "runtime-1"


def test_managed_thread_execution_store_records_thread_activity(
    tmp_path: Path,
) -> None:
    store = ManagedThreadExecutionStore(ManagedThreadStore(tmp_path / "hub"))
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    thread = store.create_thread_target("codex", workspace_root)

    execution = store.create_execution(
        thread.thread_target_id,
        prompt="Summarize the current queue",
    )
    store.record_thread_activity(
        thread.thread_target_id,
        execution_id=execution.execution_id,
        message_preview="Summarize the current queue",
    )

    updated = store.get_thread_target(thread.thread_target_id)

    assert updated is not None
    assert updated.last_execution_id == execution.execution_id
    assert updated.last_message_preview == "Summarize the current queue"
    assert updated.display_name == "Summarize the current queue"
