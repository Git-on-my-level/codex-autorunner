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

    codex_work = bindings.list_active_work_summaries(agent_id="codex")
    opencode_work = bindings.list_active_work_summaries(agent_id="opencode")

    assert len(codex_work) == 1
    assert codex_work[0].agent_id == "codex"
    assert codex_work[0].thread_target_id == codex_thread_id

    assert len(opencode_work) == 1
    assert opencode_work[0].agent_id == "opencode"
    assert opencode_work[0].thread_target_id == opencode_thread_id


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
