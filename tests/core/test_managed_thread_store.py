from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.runtime_identity import (
    RUNTIME_STAGE_EFFECTIVE,
    RuntimeIdentityStage,
)


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


def test_mark_turn_finished_persists_provider_reported_effective_runtime(
    tmp_path: Path,
) -> None:
    store = ManagedThreadStore(tmp_path / "hub")
    thread = store.create_thread("opencode", tmp_path)
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="work", model="other/model")
    managed_turn_id = str(turn["managed_turn_id"])
    effective_runtime = RuntimeIdentityStage(
        stage=RUNTIME_STAGE_EFFECTIVE,
        logical_agent="opencode",
        runtime_agent="opencode",
        provider_id="zai-coding-plan",
        provider_model_id="glm-5.1",
        canonical_model_label="zai-coding-plan/glm-5.1",
        backend_runtime_id="session-1",
        source="opencode.session",
        provenance={"session_id": "session-1"},
    )

    assert store.mark_turn_finished(
        managed_turn_id,
        status="ok",
        assistant_text="done",
        backend_turn_id="turn-1",
        effective_runtime=effective_runtime,
    )

    finished = store.get_turn(managed_thread_id, managed_turn_id)
    runtime_identity = finished["runtime_identity"]
    assert runtime_identity["resolved"]["canonical_model_label"] == "other/model"
    assert runtime_identity["effective"]["source"] == "opencode.session"
    assert (
        runtime_identity["effective"]["canonical_model_label"]
        == "zai-coding-plan/glm-5.1"
    )
