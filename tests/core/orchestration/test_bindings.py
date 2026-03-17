from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.orchestration import (
    OrchestrationBindingStore,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore


def _create_thread(
    hub_root: Path,
    *,
    agent: str = "codex",
    repo_id: str = "repo-1",
    workspace_name: str = "workspace",
    name: str = "Thread",
) -> str:
    workspace_root = hub_root / workspace_name
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        agent,
        workspace_root,
        repo_id=repo_id,
        name=name,
    )
    return str(created["managed_thread_id"])


def test_binding_store_replaces_active_binding_for_same_surface(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    first_thread_id = _create_thread(hub_root, workspace_name="repo-a-1")
    second_thread_id = _create_thread(hub_root, workspace_name="repo-a-2")

    first = bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=first_thread_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="reuse",
    )
    second = bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=second_thread_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="reuse",
    )

    active = bindings.get_binding(surface_kind="telegram", surface_key="123:root")
    previous = bindings.get_binding(
        surface_kind="telegram",
        surface_key="123:root",
        include_disabled=True,
    )

    assert first.binding_id != second.binding_id
    assert active is not None
    assert active.binding_id == second.binding_id
    assert active.thread_target_id == second_thread_id
    assert previous is not None
    assert previous.thread_target_id == second_thread_id
    all_rows = bindings.list_bindings(
        repo_id="repo-1", surface_kind="telegram", include_disabled=True
    )
    assert [row.thread_target_id for row in all_rows] == [
        second_thread_id,
        first_thread_id,
    ]
    assert all_rows[1].disabled_at is not None


def test_binding_store_disable_and_active_thread_lookup(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    thread_id = _create_thread(hub_root)
    binding = bindings.upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )

    assert (
        bindings.get_active_thread_for_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        == thread_id
    )

    disabled = bindings.disable_binding(binding_id=binding.binding_id)

    assert disabled is not None
    assert disabled.disabled_at is not None
    assert (
        bindings.get_active_thread_for_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        is None
    )


def test_binding_store_lists_bindings_and_active_work_by_agent_and_repo(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    codex_thread_id = _create_thread(
        hub_root,
        agent="codex",
        repo_id="repo-1",
        workspace_name="repo-1",
        name="Codex Thread",
    )
    opencode_thread_id = _create_thread(
        hub_root,
        agent="opencode",
        repo_id="repo-2",
        workspace_name="repo-2",
        name="OpenCode Thread",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=codex_thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-1",
        thread_target_id=codex_thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-2",
        thread_target_id=opencode_thread_id,
        agent_id="opencode",
        repo_id="repo-2",
    )
    store = PmaThreadStore(hub_root)
    running_turn = store.create_turn(codex_thread_id, prompt="busy")

    repo_bindings = bindings.list_bindings(repo_id="repo-1")
    agent_bindings = bindings.list_bindings(agent_id="codex")
    active_work = bindings.list_active_work_summaries(repo_id="repo-1")

    assert {binding.surface_kind for binding in repo_bindings} == {
        "telegram",
        "discord",
    }
    assert len(agent_bindings) == 2
    assert len(active_work) == 1
    assert active_work[0].thread_target_id == codex_thread_id
    assert active_work[0].execution_id == running_turn["managed_turn_id"]
    assert active_work[0].execution_status == "running"
    assert active_work[0].binding_count == 2
    assert set(active_work[0].surface_kinds) == {"discord", "telegram"}


def test_binding_store_active_work_by_agent(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    codex_thread_id = _create_thread(
        hub_root,
        agent="codex",
        repo_id="repo-1",
        workspace_name="repo-1",
        name="Codex Thread",
    )
    opencode_thread_id = _create_thread(
        hub_root,
        agent="opencode",
        repo_id="repo-1",
        workspace_name="repo-2",
        name="OpenCode Thread",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=codex_thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-2",
        thread_target_id=opencode_thread_id,
        agent_id="opencode",
        repo_id="repo-1",
    )
    store = PmaThreadStore(hub_root)
    store.create_turn(codex_thread_id, prompt="codex busy")
    store.create_turn(opencode_thread_id, prompt="opencode busy")

    codex_work = bindings.list_active_work_summaries(agent_id="codex")
    opencode_work = bindings.list_active_work_summaries(agent_id="opencode")

    assert len(codex_work) == 1
    assert codex_work[0].agent_id == "codex"
    assert codex_work[0].thread_target_id == codex_thread_id

    assert len(opencode_work) == 1
    assert opencode_work[0].agent_id == "opencode"
    assert opencode_work[0].thread_target_id == opencode_thread_id


def test_binding_store_filters_by_thread_target_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    first_thread_id = _create_thread(hub_root, workspace_name="repo-a-1")
    second_thread_id = _create_thread(hub_root, workspace_name="repo-a-2")

    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=first_thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=second_thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )

    listed = bindings.list_bindings(thread_target_id=first_thread_id)

    assert len(listed) == 1
    assert listed[0].thread_target_id == first_thread_id
    assert listed[0].surface_kind == "discord"


def test_binding_store_active_work_by_repo(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    thread1_id = _create_thread(
        hub_root,
        agent="codex",
        repo_id="repo-a",
        workspace_name="repo-a",
        name="Repo A Thread",
    )
    thread2_id = _create_thread(
        hub_root,
        agent="codex",
        repo_id="repo-b",
        workspace_name="repo-b",
        name="Repo B Thread",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-a",
        thread_target_id=thread1_id,
        agent_id="codex",
        repo_id="repo-a",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-b",
        thread_target_id=thread2_id,
        agent_id="codex",
        repo_id="repo-b",
    )
    store = PmaThreadStore(hub_root)
    store.create_turn(thread1_id, prompt="repo a busy")
    store.create_turn(thread2_id, prompt="repo b busy")

    repo_a_work = bindings.list_active_work_summaries(repo_id="repo-a")
    repo_b_work = bindings.list_active_work_summaries(repo_id="repo-b")

    assert len(repo_a_work) == 1
    assert repo_a_work[0].repo_id == "repo-a"
    assert repo_a_work[0].thread_target_id == thread1_id

    assert len(repo_b_work) == 1
    assert repo_b_work[0].repo_id == "repo-b"
    assert repo_b_work[0].thread_target_id == thread2_id


def test_binding_store_active_work_includes_queue_depth(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    thread_id = _create_thread(hub_root, repo_id="repo-1", workspace_name="repo-1")
    store = PmaThreadStore(hub_root)
    running_turn = store.create_turn(thread_id, prompt="first")
    queued_turn = store.create_turn(
        thread_id,
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-1",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )

    active_work = bindings.list_active_work_summaries(repo_id="repo-1")

    assert len(active_work) == 1
    assert active_work[0].execution_id == running_turn["managed_turn_id"]
    assert active_work[0].queued_count == 1
    assert queued_turn["status"] == "queued"


def test_binding_store_active_work_excludes_idle_completed_and_archived_threads(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    store = PmaThreadStore(hub_root)
    idle_thread_id = _create_thread(
        hub_root, repo_id="repo-1", workspace_name="idle", name="Idle Thread"
    )
    completed_thread_id = _create_thread(
        hub_root, repo_id="repo-1", workspace_name="completed", name="Completed Thread"
    )
    running_thread_id = _create_thread(
        hub_root, repo_id="repo-1", workspace_name="running", name="Running Thread"
    )
    queued_thread_id = _create_thread(
        hub_root, repo_id="repo-1", workspace_name="queued", name="Queued Thread"
    )
    running_and_queued_thread_id = _create_thread(
        hub_root,
        repo_id="repo-1",
        workspace_name="running-and-queued",
        name="Running and Queued Thread",
    )
    archived_thread_id = _create_thread(
        hub_root, repo_id="repo-1", workspace_name="archived", name="Archived Thread"
    )

    for surface_key, thread_id in (
        ("idle", idle_thread_id),
        ("completed", completed_thread_id),
        ("running", running_thread_id),
        ("queued", queued_thread_id),
        ("running-and-queued", running_and_queued_thread_id),
        ("archived", archived_thread_id),
    ):
        bindings.upsert_binding(
            surface_kind="discord",
            surface_key=f"chan-{surface_key}",
            thread_target_id=thread_id,
            agent_id="codex",
            repo_id="repo-1",
        )

    completed_turn = store.create_turn(completed_thread_id, prompt="completed")
    assert store.mark_turn_finished(completed_turn["managed_turn_id"], status="ok")

    running_turn = store.create_turn(running_thread_id, prompt="running")

    queued_running_turn = store.create_turn(queued_thread_id, prompt="queued parent")
    queued_turn = store.create_turn(
        queued_thread_id,
        prompt="queued child",
        busy_policy="queue",
    )
    assert store.mark_turn_finished(queued_running_turn["managed_turn_id"], status="ok")

    running_with_queue_turn = store.create_turn(
        running_and_queued_thread_id,
        prompt="running parent",
    )
    store.create_turn(
        running_and_queued_thread_id,
        prompt="running child",
        busy_policy="queue",
    )

    archived_running_turn = store.create_turn(archived_thread_id, prompt="archived")
    store.create_turn(
        archived_thread_id,
        prompt="archived child",
        busy_policy="queue",
    )
    store.archive_thread(archived_thread_id)

    active_work = bindings.list_active_work_summaries(repo_id="repo-1")
    summaries = {summary.thread_target_id: summary for summary in active_work}

    assert set(summaries) == {
        running_thread_id,
        queued_thread_id,
        running_and_queued_thread_id,
    }
    assert summaries[running_thread_id].execution_id == running_turn["managed_turn_id"]
    assert summaries[running_thread_id].execution_status == "running"
    assert summaries[running_thread_id].queued_count == 0

    assert summaries[queued_thread_id].execution_id == queued_turn["managed_turn_id"]
    assert summaries[queued_thread_id].execution_status == "queued"
    assert summaries[queued_thread_id].queued_count == 1
    assert summaries[queued_thread_id].runtime_status == "completed"

    assert (
        summaries[running_and_queued_thread_id].execution_id
        == running_with_queue_turn["managed_turn_id"]
    )
    assert summaries[running_and_queued_thread_id].execution_status == "running"
    assert summaries[running_and_queued_thread_id].queued_count == 1

    assert idle_thread_id not in summaries
    assert completed_thread_id not in summaries
    assert archived_thread_id not in summaries
    assert archived_running_turn["status"] == "running"


def test_binding_store_list_bindings_by_surface_kind(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    thread_id = _create_thread(hub_root)

    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-1",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:root",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="123:chat",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="repo-1",
    )

    discord_bindings = bindings.list_bindings(surface_kind="discord")
    telegram_bindings = bindings.list_bindings(surface_kind="telegram")

    assert len(discord_bindings) == 1
    assert discord_bindings[0].surface_kind == "discord"

    assert len(telegram_bindings) == 2
    assert {b.surface_key for b in telegram_bindings} == {"123:root", "123:chat"}


def test_binding_store_supports_agent_workspace_owners(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root)
    bindings = OrchestrationBindingStore(hub_root)
    workspace_root = hub_root / "runtimes" / "zeroclaw" / "zc-main"
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        resource_kind="agent_workspace",
        resource_id="zc-main",
        name="Workspace thread",
    )
    thread_id = str(created["managed_thread_id"])

    binding = bindings.upsert_binding(
        surface_kind="discord",
        surface_key="chan-zc",
        thread_target_id=thread_id,
        agent_id="codex",
        resource_kind="agent_workspace",
        resource_id="zc-main",
    )
    store.create_turn(thread_id, prompt="busy")

    listed = bindings.list_bindings(
        resource_kind="agent_workspace",
        resource_id="zc-main",
    )
    active = bindings.list_active_work_summaries(
        resource_kind="agent_workspace",
        resource_id="zc-main",
    )

    assert binding.resource_kind == "agent_workspace"
    assert binding.resource_id == "zc-main"
    assert binding.repo_id is None
    assert len(listed) == 1
    assert listed[0].resource_kind == "agent_workspace"
    assert listed[0].resource_id == "zc-main"
    assert len(active) == 1
    assert active[0].resource_kind == "agent_workspace"
    assert active[0].resource_id == "zc-main"
    assert active[0].repo_id is None
