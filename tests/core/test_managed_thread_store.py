from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.managed_thread_store import ManagedThreadStore


def test_archive_thread_terminalizes_running_turn(tmp_path: Path) -> None:
    store = ManagedThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path)
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="work")
    managed_turn_id = str(turn["managed_turn_id"])

    store.archive_thread(managed_thread_id)

    archived = store.get_thread(managed_thread_id)
    finished_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert archived is not None
    assert archived["lifecycle_status"] == "archived"
    assert archived["normalized_status"] == "archived"
    assert finished_turn is not None
    assert finished_turn["status"] == "interrupted"
    assert finished_turn["finished_at"]
    assert finished_turn["error"] == "thread_archived"
    assert store.get_running_turn(managed_thread_id) is None
    assert managed_thread_id not in store.list_thread_ids_with_running_executions(
        limit=None
    )
