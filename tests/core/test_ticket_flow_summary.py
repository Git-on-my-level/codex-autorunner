from __future__ import annotations

from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_summary import (
    build_ticket_flow_display,
    build_ticket_flow_summary,
)


def test_build_ticket_flow_summary_includes_pr_and_final_review(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-final-review.md").write_text(
        "---\n"
        "agent: codex\n"
        "done: true\n"
        "title: Final review\n"
        "ticket_kind: final_review\n"
        "---\n\n"
        "Reviewed.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002-open-pr.md").write_text(
        "---\n"
        "agent: codex\n"
        "done: false\n"
        "title: Open PR\n"
        "ticket_kind: open_pr\n"
        "pr_url: https://github.com/example/repo/pull/123\n"
        "---\n\n"
        "Open it.\n",
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            "run-1",
            "ticket_flow",
            input_data={},
            state={"ticket_engine": {"total_turns": 7}},
        )

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["pr_url"] == "https://github.com/example/repo/pull/123"
    assert summary["pr_opened"] is True
    assert summary["final_review_status"] == "done"
    assert summary["done_count"] == 1
    assert summary["total_count"] == 2


def test_build_ticket_flow_summary_uses_latest_final_review_ticket(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-final-review.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Final review\n---\n\nold\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002-final-review.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Final review\n---\n\nnew\n",
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run("run-1", "ticket_flow", input_data={}, state={})

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["final_review_status"] == "pending"


def test_build_ticket_flow_summary_pr_url_only_from_open_pr_ticket(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-impl.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Implement\n---\n\n"
        "Related: https://github.com/example/repo/pull/999\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002-final-review.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Final review\n---\n\nok\n",
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run("run-1", "ticket_flow", input_data={}, state={})

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["pr_url"] is None
    assert summary["pr_opened"] is False


def test_build_ticket_flow_summary_without_run_uses_done_status(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-a.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: A\n---\n\nA\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002-b.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: B\n---\n\nB\n",
        encoding="utf-8",
    )

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["status"] == "done"
    assert summary["status_label"] == "Done"
    assert summary["status_icon"] == "🔵"
    assert summary["run_id"] is None
    assert summary["done_count"] == 2
    assert summary["total_count"] == 2


def test_build_ticket_flow_display_maps_runtime_status() -> None:
    display = build_ticket_flow_display(
        status="running",
        done_count=1,
        total_count=4,
        run_id="run-123",
    )
    assert display["status"] == "running"
    assert display["status_label"] == "running"
    assert display["status_icon"] == "🟢"
    assert display["is_active"] is True
    assert display["run_id"] == "run-123"


def test_build_ticket_flow_display_idle_when_no_status_and_incomplete() -> None:
    display = build_ticket_flow_display(
        status=None,
        done_count=1,
        total_count=3,
        run_id=None,
    )
    assert display["status"] == "idle"
    assert display["status_label"] == "Idle"
    assert display["status_icon"] == "⚪"
    assert display["is_active"] is False
    assert display["run_id"] is None


def test_build_ticket_flow_display_done_when_no_status_but_all_done() -> None:
    display = build_ticket_flow_display(
        status=None,
        done_count=3,
        total_count=3,
        run_id=None,
    )
    assert display["status"] == "done"
    assert display["status_label"] == "Done"


def test_build_ticket_flow_summary_returns_none_when_no_tickets(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is None


def test_build_ticket_flow_summary_ticket_counts_match_projection(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Done 1\n---\n\nDone.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Done 2\n---\n\nDone.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Open\n---\n\nOpen.\n",
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            "run-1",
            "ticket_flow",
            input_data={},
            state={"ticket_engine": {"total_turns": 5}},
        )

    from codex_autorunner.core.ticket_flow_projection import build_canonical_state_v1

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["done_count"] == 2
    assert summary["total_count"] == 3

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={},
    )
    assert canonical["frontmatter_done_count"] == 2
    assert canonical["frontmatter_total_count"] == 3


def test_build_ticket_flow_display_paused_status() -> None:
    display = build_ticket_flow_display(
        status="paused",
        done_count=0,
        total_count=5,
        run_id="run-paused",
    )
    assert display["status"] == "paused"
    assert display["status_icon"] == "🔴"
    assert display["is_active"] is True


def test_build_ticket_flow_display_failed_status() -> None:
    display = build_ticket_flow_display(
        status="failed",
        done_count=2,
        total_count=5,
        run_id="run-failed",
    )
    assert display["status"] == "failed"
    assert display["status_icon"] == "⚫"
    assert display["is_active"] is False


def test_build_ticket_flow_summary_extracts_pr_url_from_frontmatter(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001-impl.md").write_text(
        "---\nagent: codex\ndone: true\ntitle: Implement\n---\n\nDone.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002-open-pr.md").write_text(
        "---\n"
        "agent: codex\n"
        "done: false\n"
        "title: Open PR\n"
        "ticket_kind: open_pr\n"
        "pr_url: https://github.com/example/repo/pull/42\n"
        "---\n\nOpen it.\n",
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run("run-1", "ticket_flow", input_data={}, state={})

    summary = build_ticket_flow_summary(repo_root, include_failure=False)
    assert summary is not None
    assert summary["pr_url"] == "https://github.com/example/repo/pull/42"
    assert summary["pr_opened"] is True
