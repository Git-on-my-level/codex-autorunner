from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pr_bindings import PrBindingStore


def _insert_thread_target(hub_root: Path, thread_target_id: str) -> None:
    with open_orchestration_sqlite(hub_root) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                created_at,
                updated_at
            ) VALUES (?, 'codex', '2026-03-25T00:00:00Z', '2026-03-25T00:00:00Z')
            """,
            (thread_target_id,),
        )


def test_upsert_binding_updates_existing_pr_binding_without_duplication(
    tmp_path: Path,
) -> None:
    store = PrBindingStore(tmp_path)

    created = store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="draft",
        head_branch="feature/login",
        base_branch="main",
    )
    updated = store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/login-v2",
        base_branch="main",
    )

    assert updated.binding_id == created.binding_id
    assert updated.pr_state == "open"
    assert updated.head_branch == "feature/login-v2"
    assert updated.base_branch == "main"
    assert updated.thread_target_id is None

    fetched = store.get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )
    assert fetched is not None
    assert fetched.binding_id == created.binding_id
    assert fetched.head_branch == "feature/login-v2"

    listed = store.list_bindings(repo_id="repo-1")
    assert [binding.binding_id for binding in listed] == [created.binding_id]

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM orch_pr_bindings
             WHERE provider = ?
               AND repo_slug = ?
               AND pr_number = ?
            """,
            ("github", "acme/widgets", 17),
        ).fetchone()
    assert row is not None
    assert int(row["count"] or 0) == 1


def test_attach_thread_target_and_find_active_binding_for_branch(
    tmp_path: Path,
) -> None:
    store = PrBindingStore(tmp_path)
    _insert_thread_target(tmp_path, "thread-123")

    store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/alpha",
        base_branch="main",
    )
    store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=18,
        pr_state="closed",
        head_branch="feature/alpha",
        base_branch="main",
    )

    attached = store.attach_thread_target(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        thread_target_id="thread-123",
    )
    assert attached is not None
    assert attached.thread_target_id == "thread-123"

    retained = store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/alpha",
        base_branch="main",
    )
    assert retained.thread_target_id == "thread-123"

    active = store.find_active_binding_for_branch(
        provider="github",
        repo_slug="acme/widgets",
        branch_name="feature/alpha",
    )
    assert active is not None
    assert active.pr_number == 17
    assert active.thread_target_id == "thread-123"


def test_close_binding_marks_terminal_state_without_deleting_row(
    tmp_path: Path,
) -> None:
    store = PrBindingStore(tmp_path)
    _insert_thread_target(tmp_path, "thread-999")

    created = store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=21,
        pr_state="open",
        head_branch="feature/close-me",
        base_branch="main",
        thread_target_id="thread-999",
    )

    closed = store.close_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=21,
        pr_state="merged",
    )
    assert closed is not None
    assert closed.binding_id == created.binding_id
    assert closed.pr_state == "merged"
    assert closed.closed_at is not None
    assert closed.thread_target_id == "thread-999"

    fetched = store.get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=21,
    )
    assert fetched is not None
    assert fetched.binding_id == created.binding_id
    assert fetched.pr_state == "merged"

    assert (
        store.find_active_binding_for_branch(
            provider="github",
            repo_slug="acme/widgets",
            branch_name="feature/close-me",
        )
        is None
    )

    merged = store.list_bindings(pr_state="merged")
    assert [binding.binding_id for binding in merged] == [created.binding_id]

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM orch_pr_bindings
             WHERE binding_id = ?
            """,
            (created.binding_id,),
        ).fetchone()
    assert row is not None
    assert int(row["count"] or 0) == 1
