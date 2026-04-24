from __future__ import annotations

import os
from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_thread_mirror import sync_legacy_mirror
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    _ensure_schema,
    _execution_row_to_record,
    _thread_row_to_record,
)
from codex_autorunner.core.pr_binding_resolver import resolve_binding_for_scm_event
from codex_autorunner.core.scm_events import ScmEvent
from codex_autorunner.core.sqlite_utils import open_sqlite


def _make_event(
    *,
    repo_slug: str = "acme/widgets",
    repo_id: str | None = "repo-1",
    pr_number: int | None = 17,
    action: str = "opened",
    state: str = "open",
    merged: bool = False,
    draft: bool = False,
    head_ref: str | None = "feature/login",
    base_ref: str | None = "main",
) -> ScmEvent:
    payload: dict[str, object] = {
        "action": action,
        "state": state,
        "merged": merged,
        "draft": draft,
    }
    if head_ref is not None:
        payload["head_ref"] = head_ref
    if base_ref is not None:
        payload["base_ref"] = base_ref
    return ScmEvent(
        event_id=f"github:delivery:{pr_number}:{action}",
        provider="github",
        event_type="pull_request",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:01Z",
        repo_slug=repo_slug,
        repo_id=repo_id,
        pr_number=pr_number,
        delivery_id=None,
        payload=payload,
        raw_payload=None,
    )


def _create_repo_thread(
    hub_root: Path,
    *,
    repo_id: str = "repo-1",
    head_branch: str = "feature/login",
    thread_store: PmaThreadStore | None = None,
) -> str:
    workspace_root = hub_root / "worktrees" / head_branch.replace("/", "-")
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = thread_store or PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        repo_id=repo_id,
        metadata={"head_branch": head_branch},
    )
    return str(created["managed_thread_id"])


def _create_terminal_repo_thread(
    hub_root: Path,
    *,
    repo_id: str = "repo-1",
    head_branch: str = "feature/login",
    archive: bool = False,
    thread_store: PmaThreadStore | None = None,
) -> str:
    workspace_root = hub_root / "worktrees" / head_branch.replace("/", "-")
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = thread_store or PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        workspace_root,
        repo_id=repo_id,
        metadata={"head_branch": head_branch},
    )
    managed_thread_id = str(created["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="create pr")
    assert store.mark_turn_finished(turn["managed_turn_id"], status="ok") is True
    if archive:
        store.archive_thread(managed_thread_id)
    return managed_thread_id


def test_resolve_binding_for_pr_event_creates_binding_and_attaches_matching_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    thread_store = PmaThreadStore(hub_root)
    thread_target_id = _create_repo_thread(hub_root, thread_store=thread_store)

    binding = resolve_binding_for_scm_event(hub_root, _make_event())

    assert binding is not None
    assert binding.provider == "github"
    assert binding.repo_slug == "acme/widgets"
    assert binding.repo_id == "repo-1"
    assert binding.pr_number == 17
    assert binding.pr_state == "open"
    assert binding.head_branch == "feature/login"
    assert binding.base_branch == "main"
    assert binding.thread_target_id == thread_target_id


def test_resolve_binding_for_repeated_pr_event_updates_existing_binding_without_duplication(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    thread_store = PmaThreadStore(hub_root)

    created = resolve_binding_for_scm_event(hub_root, _make_event())
    assert created is not None
    assert created.thread_target_id is None

    thread_target_id = _create_repo_thread(hub_root, thread_store=thread_store)
    updated = resolve_binding_for_scm_event(
        hub_root,
        _make_event(action="synchronize", state="open"),
    )

    assert updated is not None
    assert updated.binding_id == created.binding_id
    assert updated.thread_target_id == thread_target_id

    with open_orchestration_sqlite(hub_root) as conn:
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


def test_resolve_binding_for_pr_event_creates_repo_scoped_binding_when_no_thread_exists(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"

    binding = resolve_binding_for_scm_event(
        hub_root,
        _make_event(pr_number=29, head_ref="feature/repo-scoped"),
    )

    assert binding is not None
    assert binding.pr_number == 29
    assert binding.thread_target_id is None
    assert binding.head_branch == "feature/repo-scoped"


def test_resolve_binding_for_closed_or_merged_pr_updates_existing_binding_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    thread_store = PmaThreadStore(hub_root)
    thread_target_id = _create_repo_thread(hub_root, thread_store=thread_store)

    created = resolve_binding_for_scm_event(hub_root, _make_event())
    assert created is not None
    assert created.thread_target_id == thread_target_id

    merged = resolve_binding_for_scm_event(
        hub_root,
        _make_event(action="closed", state="closed", merged=True),
    )

    assert merged is not None
    assert merged.binding_id == created.binding_id
    assert merged.pr_state == "merged"
    assert merged.closed_at is not None
    assert merged.thread_target_id == thread_target_id


def test_resolve_binding_for_pr_event_attaches_recent_archived_matching_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    thread_store = PmaThreadStore(hub_root)
    thread_target_id = _create_terminal_repo_thread(
        hub_root, archive=True, thread_store=thread_store
    )

    binding = resolve_binding_for_scm_event(hub_root, _make_event())

    assert binding is not None
    assert binding.thread_target_id == thread_target_id


def test_resolve_binding_for_pr_event_ignores_stale_archived_matching_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    os.environ["CAR_LEGACY_MIRROR_ENABLED"] = "true"
    try:
        thread_store = PmaThreadStore(hub_root)
        thread_target_id = _create_terminal_repo_thread(
            hub_root, archive=True, thread_store=thread_store
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sync_legacy_mirror(
                hub_root=hub_root,
                legacy_db_path=thread_store.path,
                durable=False,
                orchestration_conn=conn,
                thread_row_to_record=_thread_row_to_record,
                execution_row_to_record=_execution_row_to_record,
                ensure_legacy_schema=_ensure_schema,
            )

        with open_sqlite(thread_store.path) as legacy_conn:
            with legacy_conn:
                legacy_conn.execute(
                    """
                    UPDATE pma_managed_threads
                       SET updated_at = ?,
                           status_updated_at = ?
                     WHERE managed_thread_id = ?
                    """,
                    (
                        "2025-01-01T00:00:00Z",
                        "2025-01-01T00:00:00Z",
                        thread_target_id,
                    ),
                )

        with open_orchestration_sqlite(hub_root) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET updated_at = ?,
                           status_updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        "2025-01-01T00:00:00Z",
                        "2025-01-01T00:00:00Z",
                        thread_target_id,
                    ),
                )

        binding = resolve_binding_for_scm_event(hub_root, _make_event())

        assert binding is not None
        assert binding.thread_target_id is None
    finally:
        os.environ.pop("CAR_LEGACY_MIRROR_ENABLED", None)
