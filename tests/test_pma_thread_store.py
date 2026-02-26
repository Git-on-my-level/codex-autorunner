from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from codex_autorunner.core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
    default_pma_threads_db_path,
    pma_threads_db_lock_path,
)


def test_create_list_get_thread(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        repo_id="repo-123",
        name="Primary",
        backend_thread_id="backend-1",
    )

    assert store.path == default_pma_threads_db_path(hub_root)
    assert store.path.exists()
    assert created["status"] == "active"
    assert created["repo_id"] == "repo-123"
    assert created["name"] == "Primary"
    assert created["backend_thread_id"] == "backend-1"

    fetched = store.get_thread(created["managed_thread_id"])
    assert fetched is not None
    assert fetched["managed_thread_id"] == created["managed_thread_id"]

    listed = store.list_threads(agent="codex", status="active", repo_id="repo-123")
    assert len(listed) == 1
    assert listed[0]["managed_thread_id"] == created["managed_thread_id"]


def test_create_finish_turn_and_query(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="hello",
        model="gpt-test",
        reasoning="high",
        client_turn_id="client-1",
    )
    assert turn["status"] == "running"
    assert turn["started_at"]

    store.mark_turn_finished(
        turn["managed_turn_id"],
        status="ok",
        assistant_text="world",
        backend_turn_id="backend-turn-1",
        transcript_turn_id="transcript-1",
    )

    fetched = store.get_turn(thread["managed_thread_id"], turn["managed_turn_id"])
    assert fetched is not None
    assert fetched["status"] == "ok"
    assert fetched["assistant_text"] == "world"
    assert fetched["backend_turn_id"] == "backend-turn-1"
    assert fetched["transcript_turn_id"] == "transcript-1"
    assert fetched["finished_at"]

    listed = store.list_turns(thread["managed_thread_id"])
    assert len(listed) == 1
    assert listed[0]["managed_turn_id"] == turn["managed_turn_id"]


def test_create_turn_rejects_when_running_turn_exists(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    first_turn = store.create_turn(thread["managed_thread_id"], prompt="first")

    with pytest.raises(ManagedThreadAlreadyHasRunningTurnError):
        store.create_turn(thread["managed_thread_id"], prompt="second")

    store.mark_turn_finished(first_turn["managed_turn_id"], status="ok")

    second_turn = store.create_turn(thread["managed_thread_id"], prompt="third")
    assert second_turn["status"] == "running"


def test_mark_turn_finished_does_not_override_interrupted_status(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    turn = store.create_turn(thread["managed_thread_id"], prompt="hello")

    store.mark_turn_interrupted(turn["managed_turn_id"])
    interrupted = store.get_turn(thread["managed_thread_id"], turn["managed_turn_id"])
    assert interrupted is not None
    interrupted_finished_at = interrupted["finished_at"]
    assert interrupted["status"] == "interrupted"
    assert interrupted_finished_at

    store.mark_turn_finished(
        turn["managed_turn_id"],
        status="ok",
        assistant_text="should-not-overwrite",
        backend_turn_id="backend-turn-overwrite",
        transcript_turn_id="transcript-overwrite",
    )

    final = store.get_turn(thread["managed_thread_id"], turn["managed_turn_id"])
    assert final is not None
    assert final["status"] == "interrupted"
    assert final["assistant_text"] is None
    assert final["backend_turn_id"] is None
    assert final["transcript_turn_id"] is None
    assert final["finished_at"] == interrupted_finished_at


def test_concurrent_create_turn_admission_is_atomic(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store_a = PmaThreadStore(hub_root)
    store_b = PmaThreadStore(hub_root)
    thread = store_a.create_thread("codex", tmp_path / "workspace")
    barrier = threading.Barrier(2)

    def _attempt_turn(store: PmaThreadStore, prompt: str) -> tuple[str, str | None]:
        barrier.wait(timeout=5)
        try:
            created = store.create_turn(thread["managed_thread_id"], prompt=prompt)
            return ("created", str(created["managed_turn_id"]))
        except ManagedThreadAlreadyHasRunningTurnError:
            return ("rejected", None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_attempt_turn, store_a, "a"),
            executor.submit(_attempt_turn, store_b, "b"),
        ]
        results = [future.result(timeout=5) for future in futures]

    outcomes = [result[0] for result in results]
    assert outcomes.count("created") == 1
    assert outcomes.count("rejected") == 1

    turns = store_a.list_turns(thread["managed_thread_id"], limit=10)
    running_turn_ids = [
        turn["managed_turn_id"] for turn in turns if turn["status"] == "running"
    ]
    assert len(running_turn_ids) == 1
    created_turn_ids = [result[1] for result in results if result[0] == "created"]
    assert running_turn_ids[0] == created_turn_ids[0]


def test_archive_thread_changes_status(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("opencode", tmp_path / "workspace")

    store.archive_thread(thread["managed_thread_id"])

    archived = store.get_thread(thread["managed_thread_id"])
    assert archived is not None
    assert archived["status"] == "archived"


def test_create_turn_rejects_archived_thread(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    store.archive_thread(thread["managed_thread_id"])

    with pytest.raises(ManagedThreadNotActiveError):
        store.create_turn(thread["managed_thread_id"], prompt="should reject")

    assert store.list_turns(thread["managed_thread_id"]) == []


def test_set_compact_seed_and_reset_backend_id(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread(
        "codex",
        tmp_path / "workspace",
        backend_thread_id="backend-keep-or-clear",
    )

    store.set_thread_compact_seed(
        thread["managed_thread_id"],
        "compact-seed",
        reset_backend_id=True,
    )

    updated = store.get_thread(thread["managed_thread_id"])
    assert updated is not None
    assert updated["compact_seed"] == "compact-seed"
    assert updated["backend_thread_id"] is None


def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    first = PmaThreadStore(hub_root)
    second = PmaThreadStore(hub_root)

    assert first.path == second.path
    assert first.path.exists()

    thread = second.create_thread("codex", tmp_path / "workspace")
    assert thread["managed_thread_id"]


def test_count_threads_by_repo_filters_empty_values(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    store.create_thread("codex", workspace, repo_id="repo-a")
    store.create_thread("codex", workspace, repo_id="repo-a")
    store.create_thread("codex", workspace, repo_id="repo-b")
    store.create_thread("codex", workspace, repo_id="  repo-b ")
    store.create_thread("codex", workspace, repo_id="   ")
    store.create_thread("codex", workspace, repo_id=None)

    counts = store.count_threads_by_repo(status="active")
    assert counts["repo-a"] == 2
    assert counts["repo-b"] == 2
    assert "" not in counts


def test_concurrentish_writes_smoke(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)

    store_a = PmaThreadStore(hub_root)
    store_b = PmaThreadStore(hub_root)

    def _write_one(store: PmaThreadStore, name: str) -> str:
        thread = store.create_thread("codex", workspace_root, name=name)
        action_id = store.append_action(
            "thread_created",
            managed_thread_id=thread["managed_thread_id"],
        )
        assert action_id > 0
        return str(thread["managed_thread_id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_write_one, store_a, "a"),
            executor.submit(_write_one, store_b, "b"),
        ]
        ids = [future.result(timeout=5) for future in futures]

    assert len(ids) == 2
    assert len(store_a.list_threads()) == 2
    assert pma_threads_db_lock_path(store_a.path).exists()
