from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import ColdTraceStore
from codex_autorunner.core.orchestration.execution_history import ExecutionCheckpoint
from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
    default_pma_threads_db_path,
    pma_threads_db_lock_path,
)
from codex_autorunner.core.pma_thread_store_rows import PmaThreadRecord


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
    assert created["lifecycle_status"] == "active"
    assert created["normalized_status"] == "idle"
    assert created["status_reason_code"] == "thread_created"
    assert "status" not in created
    assert created["status_terminal"] is False
    assert created["repo_id"] == "repo-123"
    assert created["name"] == "Primary"
    assert "backend_thread_id" not in created
    assert "backend_runtime_instance_id" not in created

    fetched = store.get_thread(created["managed_thread_id"])
    assert fetched is not None
    assert fetched["managed_thread_id"] == created["managed_thread_id"]

    listed = store.list_threads(agent="codex", status="active", repo_id="repo-123")
    assert len(listed) == 1
    assert listed[0]["managed_thread_id"] == created["managed_thread_id"]

    normalized_listed = store.list_threads(
        agent="codex",
        normalized_status="idle",
        repo_id="repo-123",
    )
    assert len(normalized_listed) == 1
    assert normalized_listed[0]["managed_thread_id"] == created["managed_thread_id"]


def test_create_thread_mirrors_backend_thread_id_into_orchestration_row(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        backend_thread_id="backend-1",
    )

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            """
            SELECT backend_thread_id
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (created["managed_thread_id"],),
        ).fetchone()

    assert row is not None
    assert row["backend_thread_id"] == "backend-1"


def test_create_thread_enriches_head_branch_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(
        "codex_autorunner.core.pma_thread_store_rows.workspace_head_branch",
        lambda repo_root: "feature/pr-binding",
    )

    created = PmaThreadStore(hub_root).create_thread("codex", workspace_root)

    assert created["metadata"]["head_branch"] == "feature/pr-binding"


def test_backend_thread_binding_is_shared_across_store_instances(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        backend_thread_id="backend-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )
    managed_thread_id = created["managed_thread_id"]

    restarted = PmaThreadStore(hub_root)
    fetched = restarted.get_thread(managed_thread_id)
    assert fetched is not None
    binding = restarted.get_thread_runtime_binding(managed_thread_id)
    assert binding is not None
    assert binding.backend_thread_id == "backend-1"
    assert binding.backend_runtime_instance_id == "runtime-1"
    assert "backend_thread_id" not in fetched
    assert "backend_runtime_instance_id" not in fetched


def test_backend_thread_binding_can_be_cleared_across_restart(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        backend_thread_id="backend-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )
    managed_thread_id = created["managed_thread_id"]

    clear_runtime_thread_binding(hub_root, managed_thread_id)

    restarted = PmaThreadStore(hub_root)
    fetched = restarted.get_thread(managed_thread_id)
    assert fetched is not None
    binding = restarted.get_thread_runtime_binding(managed_thread_id)
    assert binding is None

    listed = restarted.list_threads(agent="codex", status="active")
    assert len(listed) == 1
    assert listed[0]["managed_thread_id"] == managed_thread_id
    assert "backend_thread_id" not in listed[0]
    assert "backend_runtime_instance_id" not in listed[0]


def test_connect_readonly_skips_bootstrap_initialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    created = PmaThreadStore(hub_root).create_thread("codex", workspace_root)
    initialize_calls: list[str] = []

    def _record_initialize(self) -> None:  # type: ignore[no-untyped-def]
        if Path(self.hub_root) == hub_root.resolve():
            initialize_calls.append("called")

    monkeypatch.setattr(
        "codex_autorunner.core.pma_thread_store.PmaThreadStoreBootstrap.initialize",
        _record_initialize,
    )

    store = PmaThreadStore.connect_readonly(hub_root)
    listed = store.list_threads(agent="codex")

    assert initialize_calls == []
    assert [thread["managed_thread_id"] for thread in listed] == [
        created["managed_thread_id"]
    ]


def test_connect_readonly_does_not_prepare_shared_state_on_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    created = PmaThreadStore(hub_root).create_thread("codex", workspace_root)

    def _fail_prepare(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("readonly access must not prepare shared state")

    monkeypatch.setattr(
        "codex_autorunner.core.pma_thread_store_bootstrap.ensure_legacy_orchestration_backfill",
        _fail_prepare,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_thread_store_bootstrap.prepare_orchestration_sqlite",
        _fail_prepare,
    )

    store = PmaThreadStore.connect_readonly(hub_root)
    fetched = store.get_thread(created["managed_thread_id"])

    assert fetched is not None
    assert fetched["managed_thread_id"] == created["managed_thread_id"]


def test_create_finish_turn_and_query(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="hello",
        request_kind="review",
        model="gpt-test",
        reasoning="high",
        client_turn_id="client-1",
        metadata={"bound_chat_execution": {"origin": {"kind": "pma_web"}}},
    )
    assert turn["status"] == "running"
    assert turn["request_kind"] == "review"
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
    assert fetched["request_kind"] == "review"
    assert fetched["status"] == "ok"
    assert fetched["assistant_text"] == "world"
    assert fetched["backend_turn_id"] == "backend-turn-1"
    assert fetched["transcript_turn_id"] == "transcript-1"
    assert fetched["finished_at"]
    assert fetched["metadata"] == {
        "bound_chat_execution": {"origin": {"kind": "pma_web"}}
    }

    listed = store.list_turns(thread["managed_thread_id"])
    assert len(listed) == 1
    assert listed[0]["managed_turn_id"] == turn["managed_turn_id"]
    assert listed[0]["request_kind"] == "review"

    thread_after = store.get_thread(thread["managed_thread_id"])
    assert thread_after is not None
    assert thread_after["normalized_status"] == "completed"
    assert thread_after["status_reason_code"] == "managed_turn_completed"
    assert thread_after["status_terminal"] is True


def test_set_thread_backend_id_preserves_runtime_tag_when_omitted(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread(
        "codex",
        tmp_path / "workspace",
        backend_thread_id="backend-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )

    store.set_thread_backend_id(thread["managed_thread_id"], "backend-2")

    updated = store.get_thread(thread["managed_thread_id"])
    assert updated is not None
    binding = store.get_thread_runtime_binding(thread["managed_thread_id"])
    assert binding is not None
    assert binding.backend_thread_id == "backend-2"
    assert binding.backend_runtime_instance_id == "runtime-1"
    assert "backend_thread_id" not in updated
    assert "backend_runtime_instance_id" not in updated
    with open_orchestration_sqlite(tmp_path / "hub", durable=False) as conn:
        row = conn.execute(
            """
            SELECT backend_thread_id
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (thread["managed_thread_id"],),
        ).fetchone()
    assert row is not None
    assert row["backend_thread_id"] == "backend-2"


def test_set_thread_backend_id_skips_noop_write_when_binding_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread(
        "codex",
        tmp_path / "workspace",
        backend_thread_id="backend-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )

    def _fail_write_conn():  # type: ignore[no-untyped-def]
        raise AssertionError("no-op backend binding update should not write")

    monkeypatch.setattr(store, "_write_conn", _fail_write_conn)

    store.set_thread_backend_id(
        thread["managed_thread_id"],
        "backend-1",
        backend_runtime_instance_id="runtime-1",
    )

    binding = store.get_thread_runtime_binding(thread["managed_thread_id"])
    assert binding is not None
    assert binding.backend_thread_id == "backend-1"
    assert binding.backend_runtime_instance_id == "runtime-1"


def test_set_thread_backend_id_skips_noop_write_when_runtime_tag_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread(
        "codex",
        tmp_path / "workspace",
        backend_thread_id="backend-1",
        metadata={"backend_runtime_instance_id": "runtime-1"},
    )

    def _fail_write_conn():  # type: ignore[no-untyped-def]
        raise AssertionError("no-op backend binding update should not write")

    monkeypatch.setattr(store, "_write_conn", _fail_write_conn)

    store.set_thread_backend_id(thread["managed_thread_id"], "backend-1")

    binding = store.get_thread_runtime_binding(thread["managed_thread_id"])
    assert binding is not None
    assert binding.backend_thread_id == "backend-1"
    assert binding.backend_runtime_instance_id == "runtime-1"


def test_create_turn_rejects_when_running_turn_exists(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    first_turn = store.create_turn(thread["managed_thread_id"], prompt="first")

    with pytest.raises(ManagedThreadAlreadyHasRunningTurnError):
        store.create_turn(thread["managed_thread_id"], prompt="second")

    store.mark_turn_finished(first_turn["managed_turn_id"], status="ok")

    second_turn = store.create_turn(thread["managed_thread_id"], prompt="third")
    assert second_turn["status"] == "running"


def test_create_turn_can_queue_behind_running_turn(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second",
        request_kind="review",
        busy_policy="queue",
        client_turn_id="client-2",
        queue_payload={"request": {"message_text": "second", "kind": "review"}},
    )

    assert running_turn["status"] == "running"
    assert queued_turn["status"] == "queued"
    assert store.get_queue_depth(thread["managed_thread_id"]) == 1
    queued_items = store.list_pending_turn_queue_items(thread["managed_thread_id"])
    assert len(queued_items) == 1
    assert queued_items[0]["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert queued_items[0]["request_kind"] == "review"


def test_get_turn_by_client_turn_id_prefers_current_execution(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="first",
        client_turn_id="publish-turn-1",
    )
    store.mark_turn_finished(running_turn["managed_turn_id"], status="ok")

    newer_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second",
        client_turn_id="publish-turn-1",
    )

    fetched = store.get_turn_by_client_turn_id(
        thread["managed_thread_id"], "publish-turn-1"
    )
    assert fetched is not None
    assert fetched["managed_turn_id"] == newer_turn["managed_turn_id"]
    assert fetched["status"] == "running"


def test_claim_next_queued_turn_promotes_queued_execution(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second",
        request_kind="review",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second", "kind": "review"}},
    )

    assert store.claim_next_queued_turn(thread["managed_thread_id"]) is None
    assert (
        store.mark_turn_finished(running_turn["managed_turn_id"], status="ok") is True
    )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    execution, payload = claimed
    assert execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert execution["request_kind"] == "review"
    assert execution["status"] == "running"
    assert payload["request"]["kind"] == "review"
    assert payload["request"]["message_text"] == "second"
    assert store.get_queue_depth(thread["managed_thread_id"]) == 0


def test_promote_queued_turn_moves_selected_execution_to_front(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    first_queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )
    promoted_queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="third",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "third"}},
    )

    assert (
        store.promote_queued_turn(
            thread["managed_thread_id"],
            promoted_queued_turn["managed_turn_id"],
        )
        is True
    )
    assert (
        store.mark_turn_finished(running_turn["managed_turn_id"], status="ok") is True
    )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    execution, _payload = claimed
    assert execution["managed_turn_id"] == promoted_queued_turn["managed_turn_id"]
    remaining = store.list_queued_turns(thread["managed_thread_id"])
    assert remaining[0]["managed_turn_id"] == first_queued_turn["managed_turn_id"]


def test_cancel_queued_turn_marks_selected_execution_interrupted(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )

    assert (
        store.cancel_queued_turn(
            thread["managed_thread_id"],
            queued_turn["managed_turn_id"],
        )
        is True
    )
    cancelled_turn = store.get_turn(
        thread["managed_thread_id"],
        queued_turn["managed_turn_id"],
    )
    assert cancelled_turn is not None
    assert cancelled_turn["status"] == "interrupted"
    assert store.get_queue_depth(thread["managed_thread_id"]) == 0


def test_cancel_queued_turns_marks_only_pending_queue_items_interrupted(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second queued",
        request_kind="review",
        busy_policy="queue",
        client_turn_id="client-queued",
        queue_payload={"request": {"message_text": "second queued", "kind": "review"}},
    )

    cancelled = store.cancel_queued_turns(thread["managed_thread_id"])
    assert cancelled == [queued_turn["managed_turn_id"]]

    running_after = store.get_turn(
        thread["managed_thread_id"], running_turn["managed_turn_id"]
    )
    assert running_after is not None
    assert running_after["status"] == "running"
    assert running_after["error"] is None

    queued_after = store.get_turn(
        thread["managed_thread_id"], queued_turn["managed_turn_id"]
    )
    assert queued_after is not None
    assert queued_after["status"] == "interrupted"
    assert queued_after["error"] == "interrupted"
    assert queued_after["finished_at"]

    assert store.get_queue_depth(thread["managed_thread_id"]) == 0
    assert store.list_pending_turn_queue_items(thread["managed_thread_id"]) == []


def test_create_turn_recovers_stale_running_execution_before_admission(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    stale_running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    assert store.mark_turn_finished(stale_running_turn["managed_turn_id"], status="ok")
    active_turn = store.create_turn(thread["managed_thread_id"], prompt="second")
    assert store.mark_turn_interrupted(active_turn["managed_turn_id"])

    # Simulate corrupted state: an older turn is incorrectly left as running.
    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET status = 'running',
                       error_text = NULL,
                       finished_at = NULL
                 WHERE execution_id = ?
                """,
                (stale_running_turn["managed_turn_id"],),
            )

    recovered_running_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="fresh execution",
    )
    assert recovered_running_turn["status"] == "running"

    stale_after = store.get_turn(
        thread["managed_thread_id"], stale_running_turn["managed_turn_id"]
    )
    assert stale_after is not None
    assert stale_after["status"] == "interrupted"
    assert stale_after["error"] == "stale_running_execution_recovered"
    assert stale_after["finished_at"]


def test_create_turn_hides_new_running_execution_until_status_transition_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    first_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    assert store.mark_turn_finished(first_turn["managed_turn_id"], status="ok")

    observed_running_counts: list[int] = []
    original = store._transition_thread_status_in_transaction

    def _observing_transition(*args, **kwargs):  # type: ignore[no-untyped-def]
        with open_orchestration_sqlite(store.hub_root) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                """,
                (thread["managed_thread_id"],),
            ).fetchone()
        observed_running_counts.append(int((count or {})["c"] or 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "_transition_thread_status_in_transaction",
        _observing_transition,
    )

    created = store.create_turn(thread["managed_thread_id"], prompt="second")

    assert created["status"] == "running"
    assert observed_running_counts == [0]


def test_claim_next_queued_turn_recovers_stale_running_execution(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    stale_running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    assert store.mark_turn_finished(stale_running_turn["managed_turn_id"], status="ok")
    active_turn = store.create_turn(thread["managed_thread_id"], prompt="second")

    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="third queued",
        request_kind="review",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "third queued", "kind": "review"}},
    )
    assert queued_turn["status"] == "queued"
    assert store.mark_turn_interrupted(active_turn["managed_turn_id"])

    # Simulate corrupted state: an older turn is incorrectly left as running.
    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET status = 'running',
                       error_text = NULL,
                       finished_at = NULL
                 WHERE execution_id = ?
                """,
                (stale_running_turn["managed_turn_id"],),
            )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    execution, payload = claimed
    assert execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert execution["status"] == "running"
    assert payload["request"]["kind"] == "review"
    assert payload["request"]["message_text"] == "third queued"

    stale_after = store.get_turn(
        thread["managed_thread_id"], stale_running_turn["managed_turn_id"]
    )
    assert stale_after is not None
    assert stale_after["status"] == "interrupted"
    assert stale_after["error"] == "stale_running_execution_recovered"
    assert stale_after["finished_at"]


def test_claim_next_queued_turn_hides_promoted_execution_until_status_transition_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    first_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="queued",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "queued"}},
    )
    assert queued_turn["status"] == "queued"
    assert store.mark_turn_finished(first_turn["managed_turn_id"], status="ok")

    observed_running_counts: list[int] = []
    original = store._lifecycle._transition_thread_status_in_transaction
    assert original is not None

    def _observing_transition(*args, **kwargs):  # type: ignore[no-untyped-def]
        with open_orchestration_sqlite(store.hub_root) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                """,
                (thread["managed_thread_id"],),
            ).fetchone()
        observed_running_counts.append(int((count or {})["c"] or 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        store._lifecycle,
        "_transition_thread_status_in_transaction",
        _observing_transition,
    )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])

    assert claimed is not None
    execution, _payload = claimed
    assert execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert execution["status"] == "running"
    assert observed_running_counts == [0]


def test_create_turn_recovers_old_running_execution_when_status_turn_matches(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(
        tmp_path / "hub",
        stale_running_threshold_seconds=60,
    )
    thread = store.create_thread("codex", tmp_path / "workspace")

    stale_running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    thread_before = store.get_thread(thread["managed_thread_id"])
    assert thread_before is not None
    assert thread_before["status_turn_id"] == stale_running_turn["managed_turn_id"]

    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET started_at = '2000-01-01T00:00:00Z',
                       created_at = '2000-01-01T00:00:00Z'
                 WHERE execution_id = ?
                """,
                (stale_running_turn["managed_turn_id"],),
            )

    # Passive status reads should not age-interrupt the active status turn.
    running_snapshot = store.get_running_turn(thread["managed_thread_id"])
    assert running_snapshot is not None
    assert running_snapshot["managed_turn_id"] == stale_running_turn["managed_turn_id"]

    recovered_running_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="fresh execution",
    )
    assert recovered_running_turn["status"] == "running"

    stale_after = store.get_turn(
        thread["managed_thread_id"], stale_running_turn["managed_turn_id"]
    )
    assert stale_after is not None
    assert stale_after["status"] == "interrupted"
    assert stale_after["error"] == "stale_running_execution_recovered"
    assert stale_after["finished_at"]


def test_get_running_turn_uses_read_only_status_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")

    def _fail_write_conn():  # type: ignore[no-untyped-def]
        raise AssertionError("passive running-turn lookup should not write")

    monkeypatch.setattr(store, "_write_conn", _fail_write_conn)

    fetched = store.get_running_turn(thread["managed_thread_id"])

    assert fetched is not None
    assert fetched["managed_turn_id"] == running_turn["managed_turn_id"]
    assert fetched["status"] == "running"


def test_get_running_turn_hides_logically_stale_running_rows_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")

    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET status_turn_id = ?,
                       updated_at = ?
                 WHERE thread_target_id = ?
                """,
                (
                    "different-execution",
                    running_turn["started_at"],
                    thread["managed_thread_id"],
                ),
            )

    def _fail_write_conn():  # type: ignore[no-untyped-def]
        raise AssertionError("passive running-turn lookup should not write")

    monkeypatch.setattr(store, "_write_conn", _fail_write_conn)

    assert store.get_running_turn(thread["managed_thread_id"]) is None

    stale_row = store.get_turn(
        thread["managed_thread_id"], running_turn["managed_turn_id"]
    )
    assert stale_row is not None
    assert stale_row["status"] == "running"


def test_claim_next_queued_turn_recovers_old_running_execution_when_status_turn_matches(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(
        tmp_path / "hub",
        stale_running_threshold_seconds=60,
    )
    thread = store.create_thread("codex", tmp_path / "workspace")

    stale_running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second queued",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second queued"}},
    )
    assert queued_turn["status"] == "queued"

    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET started_at = '2000-01-01T00:00:00Z',
                       created_at = '2000-01-01T00:00:00Z'
                 WHERE execution_id = ?
                """,
                (stale_running_turn["managed_turn_id"],),
            )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    execution, payload = claimed
    assert execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert execution["status"] == "running"
    assert payload["request"]["message_text"] == "second queued"

    stale_after = store.get_turn(
        thread["managed_thread_id"], stale_running_turn["managed_turn_id"]
    )
    assert stale_after is not None
    assert stale_after["status"] == "interrupted"
    assert stale_after["error"] == "stale_running_execution_recovered"
    assert stale_after["finished_at"]


def test_claim_next_queued_turn_recovers_running_turn_when_thread_status_is_terminal(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    stale_running_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="second queued",
        request_kind="review",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second queued", "kind": "review"}},
    )
    assert queued_turn["status"] == "queued"

    # Simulate corrupted state: thread reports terminal while a running turn remains.
    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET runtime_status = 'completed',
                       status_reason = 'managed_turn_completed',
                       status_turn_id = ?,
                       status_terminal = 1,
                       status_updated_at = ?,
                       updated_at = ?
                 WHERE thread_target_id = ?
                """,
                (
                    stale_running_turn["managed_turn_id"],
                    stale_running_turn["started_at"],
                    stale_running_turn["started_at"],
                    thread["managed_thread_id"],
                ),
            )

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    execution, payload = claimed
    assert execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert execution["status"] == "running"
    assert payload["request"]["kind"] == "review"
    assert payload["request"]["message_text"] == "second queued"

    stale_after = store.get_turn(
        thread["managed_thread_id"], stale_running_turn["managed_turn_id"]
    )
    assert stale_after is not None
    assert stale_after["status"] == "interrupted"
    assert stale_after["error"] == "stale_running_execution_recovered"
    assert stale_after["finished_at"]


def test_create_turn_keeps_old_running_execution_with_recent_activity(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(
        tmp_path / "hub",
        stale_running_threshold_seconds=60,
    )
    thread = store.create_thread("codex", tmp_path / "workspace")

    active_turn = store.create_turn(thread["managed_thread_id"], prompt="first")

    with store._write_conn() as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET started_at = '2000-01-01T00:00:00Z',
                       created_at = '2000-01-01T00:00:00Z'
                 WHERE execution_id = ?
                """,
                (active_turn["managed_turn_id"],),
            )
    ColdTraceStore(store.hub_root).save_checkpoint(
        ExecutionCheckpoint(
            execution_id=active_turn["managed_turn_id"],
            thread_target_id=thread["managed_thread_id"],
            status="running",
            last_progress_at="2999-01-01T00:00:00Z",
        )
    )

    with pytest.raises(ManagedThreadAlreadyHasRunningTurnError):
        store.create_turn(thread["managed_thread_id"], prompt="second")

    turn_after = store.get_turn(
        thread["managed_thread_id"], active_turn["managed_turn_id"]
    )
    assert turn_after is not None
    assert turn_after["status"] == "running"


def test_recovery_does_not_interrupt_legit_queued_turn_promoted_after_newer_terminal(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    first_running = store.create_turn(thread["managed_thread_id"], prompt="A")
    queued_turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="B",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "B"}},
    )
    assert queued_turn["status"] == "queued"
    assert store.mark_turn_finished(first_running["managed_turn_id"], status="ok")

    newer_running = store.create_turn(thread["managed_thread_id"], prompt="C")
    assert newer_running["status"] == "running"
    assert store.mark_turn_finished(newer_running["managed_turn_id"], status="ok")

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is not None
    claimed_execution, _ = claimed
    assert claimed_execution["managed_turn_id"] == queued_turn["managed_turn_id"]
    assert claimed_execution["status"] == "running"

    running_after = store.get_running_turn(thread["managed_thread_id"])
    assert running_after is not None
    assert running_after["managed_turn_id"] == queued_turn["managed_turn_id"]

    queued_after = store.get_turn(
        thread["managed_thread_id"], queued_turn["managed_turn_id"]
    )
    assert queued_after is not None
    assert queued_after["status"] == "running"
    assert queued_after["error"] is None


def test_mark_turn_finished_does_not_override_interrupted_status(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    turn = store.create_turn(thread["managed_thread_id"], prompt="hello")

    assert store.mark_turn_interrupted(turn["managed_turn_id"]) is True
    interrupted = store.get_turn(thread["managed_thread_id"], turn["managed_turn_id"])
    assert interrupted is not None
    interrupted_finished_at = interrupted["finished_at"]
    assert interrupted["status"] == "interrupted"
    assert interrupted_finished_at

    assert (
        store.mark_turn_finished(
            turn["managed_turn_id"],
            status="ok",
            assistant_text="should-not-overwrite",
            backend_turn_id="backend-turn-overwrite",
            transcript_turn_id="transcript-overwrite",
        )
        is False
    )

    final = store.get_turn(thread["managed_thread_id"], turn["managed_turn_id"])
    assert final is not None
    assert final["status"] == "interrupted"
    assert final["assistant_text"] is None
    assert final["backend_turn_id"] is None
    assert final["transcript_turn_id"] is None
    assert final["finished_at"] == interrupted_finished_at

    thread_after = store.get_thread(thread["managed_thread_id"])
    assert thread_after is not None
    assert thread_after["normalized_status"] == "interrupted"
    assert thread_after["status_reason_code"] == "managed_turn_interrupted"
    assert thread_after["status_terminal"] is True


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
    assert archived["lifecycle_status"] == "archived"
    assert archived["normalized_status"] == "archived"
    assert archived["status_reason_code"] == "thread_archived"
    assert archived["status_terminal"] is True


def test_create_turn_rejects_archived_thread(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    store.archive_thread(thread["managed_thread_id"])

    with pytest.raises(ManagedThreadNotActiveError):
        store.create_turn(thread["managed_thread_id"], prompt="should reject")

    assert store.list_turns(thread["managed_thread_id"]) == []


def test_archived_thread_does_not_recover_live_running_turn(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    running = store.create_turn(thread["managed_thread_id"], prompt="first")
    queued = store.create_turn(
        thread["managed_thread_id"],
        prompt="second queued",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second queued"}},
    )
    assert queued["status"] == "queued"

    store.archive_thread(thread["managed_thread_id"])

    running_view = store.get_running_turn(thread["managed_thread_id"])
    assert running_view is not None
    assert running_view["managed_turn_id"] == running["managed_turn_id"]

    claimed = store.claim_next_queued_turn(thread["managed_thread_id"])
    assert claimed is None

    running_after = store.get_turn(
        thread["managed_thread_id"], running["managed_turn_id"]
    )
    assert running_after is not None
    assert running_after["status"] == "running"
    assert running_after["error"] is None
    assert running_after["finished_at"] is None

    queued_after = store.get_turn(
        thread["managed_thread_id"], queued["managed_turn_id"]
    )
    assert queued_after is not None
    assert queued_after["status"] == "queued"


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
    assert "backend_thread_id" not in updated
    assert "backend_runtime_instance_id" not in updated
    assert store.get_thread_runtime_binding(thread["managed_thread_id"]) is None


def test_transient_failure_recovery_promotes_status_back_to_completed(
    tmp_path: Path,
) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    first_turn = store.create_turn(thread["managed_thread_id"], prompt="first")
    assert (
        store.mark_turn_finished(first_turn["managed_turn_id"], status="error") is True
    )

    failed_thread = store.get_thread(thread["managed_thread_id"])
    assert failed_thread is not None
    assert failed_thread["normalized_status"] == "failed"
    assert failed_thread["status_reason_code"] == "managed_turn_failed"

    second_turn = store.create_turn(thread["managed_thread_id"], prompt="retry")
    running_thread = store.get_thread(thread["managed_thread_id"])
    assert running_thread is not None
    assert running_thread["normalized_status"] == "running"
    assert running_thread["status_reason_code"] == "turn_started"
    assert running_thread["status_terminal"] is False

    assert store.mark_turn_finished(second_turn["managed_turn_id"], status="ok") is True
    recovered_thread = store.get_thread(thread["managed_thread_id"])
    assert recovered_thread is not None
    assert recovered_thread["normalized_status"] == "completed"
    assert recovered_thread["status_reason_code"] == "managed_turn_completed"
    assert recovered_thread["status_terminal"] is True


def test_archive_then_activate_restores_ready_status(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")

    store.archive_thread(thread["managed_thread_id"])
    store.activate_thread(thread["managed_thread_id"])

    resumed = store.get_thread(thread["managed_thread_id"])
    assert resumed is not None
    assert resumed["lifecycle_status"] == "active"
    assert resumed["normalized_status"] == "idle"
    assert resumed["status_reason_code"] == "thread_resumed"
    assert resumed["status_terminal"] is False


def test_duplicate_completion_event_is_idempotent(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    thread = store.create_thread("codex", tmp_path / "workspace")
    turn = store.create_turn(thread["managed_thread_id"], prompt="hello")

    assert store.mark_turn_finished(turn["managed_turn_id"], status="ok") is True
    first_thread = store.get_thread(thread["managed_thread_id"])
    assert first_thread is not None
    first_changed_at = first_thread["status_updated_at"]

    assert store.mark_turn_finished(turn["managed_turn_id"], status="ok") is False
    second_thread = store.get_thread(thread["managed_thread_id"])
    assert second_thread is not None
    assert second_thread["normalized_status"] == "completed"
    assert second_thread["status_reason_code"] == "managed_turn_completed"
    assert second_thread["status_updated_at"] == first_changed_at


def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    first = PmaThreadStore(hub_root)
    second = PmaThreadStore(hub_root)

    assert first.path == second.path

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


def test_thread_record_normalizes_legacy_status_aliases(tmp_path: Path) -> None:
    store = PmaThreadStore(tmp_path / "hub")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = store.create_thread(
        "codex",
        workspace,
        metadata={"head_branch": "feature/demo"},
    )

    with store._read_conn() as conn:
        row = conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (created["managed_thread_id"],),
        ).fetchone()

    assert row is not None
    record = PmaThreadRecord.from_orchestration_row(row).to_dict()

    assert record["lifecycle_status"] == "active"
    assert record["normalized_status"] == "idle"
    assert record["status_reason_code"] == "thread_created"
    assert record["status_updated_at"] == created["status_updated_at"]
    assert "status" not in record
    assert "status_reason" not in record
    assert "status_changed_at" not in record
    assert record["status_terminal"] is False
    assert record["metadata"]["head_branch"] == "feature/demo"


def test_thread_record_migrates_alias_only_status_fields_to_canonical_schema() -> None:
    record = PmaThreadRecord.from_store_mapping(
        {
            "managed_thread_id": "thread-1",
            "agent": "codex",
            "workspace_root": "/tmp/workspace",
            "status": "active",
            "normalized_status": "completed",
            "status_reason": "managed_turn_completed",
            "status_changed_at": "2026-03-08T00:00:20Z",
            "status_terminal": True,
            "status_turn_id": "turn-1",
        }
    ).to_dict()

    assert record["lifecycle_status"] == "active"
    assert record["normalized_status"] == "completed"
    assert record["status_reason_code"] == "managed_turn_completed"
    assert record["status_updated_at"] == "2026-03-08T00:00:20Z"
    assert record["status_turn_id"] == "turn-1"
    assert record["status_terminal"] is True
    assert "status" not in record
    assert "status_reason" not in record
    assert "status_changed_at" not in record


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
