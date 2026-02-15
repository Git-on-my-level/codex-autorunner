from __future__ import annotations

from pathlib import Path

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_summary import (
    build_ticket_flow_display,
    build_ticket_flow_summary,
)


def test_build_ticket_flow_summary_includes_pr_and_final_review(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
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
