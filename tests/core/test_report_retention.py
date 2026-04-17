from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.report_retention import prune_report_directory


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size, encoding="utf-8")


def test_prune_report_directory_enforces_count_and_bytes(tmp_path: Path) -> None:
    reports = tmp_path / ".codex-autorunner" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    _write(reports / "latest-selfdescribe-smoke.md", 200)
    for i in range(10):
        _write(reports / f"history-{i:02d}.md", 200)

    summary = prune_report_directory(
        reports,
        max_history_files=3,
        max_total_bytes=1000,
    )

    names = sorted(p.name for p in reports.iterdir() if p.is_file())
    assert "latest-selfdescribe-smoke.md" in names
    history = [n for n in names if n.startswith("history-")]
    assert len(history) <= 3
    total_bytes = sum(p.stat().st_size for p in reports.iterdir() if p.is_file())
    assert total_bytes <= 1000
    assert summary.pruned >= 1


def test_prune_report_directory_keeps_stable_latest_even_when_budget_tight(
    tmp_path: Path,
) -> None:
    reports = tmp_path / ".codex-autorunner" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    _write(reports / "latest-selfdescribe-smoke.md", 400)
    _write(reports / "history-a.md", 300)
    _write(reports / "history-b.md", 300)

    prune_report_directory(
        reports,
        max_history_files=1,
        max_total_bytes=450,
    )

    names = sorted(p.name for p in reports.iterdir() if p.is_file())
    assert "latest-selfdescribe-smoke.md" in names
    assert len([n for n in names if n.startswith("history-")]) <= 1


def test_prune_report_directory_dry_run_preserves_files(tmp_path: Path) -> None:
    reports = tmp_path / ".codex-autorunner" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    _write(reports / "latest-selfdescribe-smoke.md", 200)
    _write(reports / "history-a.md", 200)
    _write(reports / "history-b.md", 200)

    summary = prune_report_directory(
        reports,
        max_history_files=1,
        max_total_bytes=1000,
        dry_run=True,
    )

    names = sorted(p.name for p in reports.iterdir() if p.is_file())
    assert names == [
        "history-a.md",
        "history-b.md",
        "latest-selfdescribe-smoke.md",
    ]
    assert summary.pruned == 1


class TestReportRetentionDryRunExecuteParity:
    def test_dry_run_and_execute_same_kept_pruned_counts(self, tmp_path: Path) -> None:
        reports_a = tmp_path / "dry" / ".codex-autorunner" / "reports"
        reports_b = tmp_path / "exec" / ".codex-autorunner" / "reports"

        for reports in (reports_a, reports_b):
            reports.mkdir(parents=True, exist_ok=True)
            _write(reports / "latest-summary.md", 200)
            for i in range(5):
                _write(reports / f"history-{i:02d}.md", 200)

        dry_summary = prune_report_directory(
            reports_a, max_history_files=2, max_total_bytes=10_000, dry_run=True
        )
        exec_summary = prune_report_directory(
            reports_b, max_history_files=2, max_total_bytes=10_000, dry_run=False
        )

        assert dry_summary.kept == exec_summary.kept
        assert dry_summary.pruned == exec_summary.pruned
        assert dry_summary.pruned == 3

    def test_execute_deletes_files_matching_pruned_count(self, tmp_path: Path) -> None:
        reports = tmp_path / ".codex-autorunner" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        _write(reports / "latest-summary.md", 200)
        for i in range(5):
            _write(reports / f"history-{i:02d}.md", 200)

        summary = prune_report_directory(
            reports, max_history_files=2, max_total_bytes=10_000, dry_run=False
        )

        remaining = sorted(p.name for p in reports.iterdir() if p.is_file())
        assert summary.pruned == 3
        assert len(remaining) == 3
        assert "latest-summary.md" in remaining

    def test_dry_run_preserves_all_files_execute_verifies(self, tmp_path: Path) -> None:
        reports = tmp_path / ".codex-autorunner" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        _write(reports / "latest-report.md", 100)
        _write(reports / "history-old.md", 100)

        dry_summary = prune_report_directory(
            reports, max_history_files=0, max_total_bytes=10_000, dry_run=True
        )
        assert dry_summary.pruned == 1
        assert sorted(p.name for p in reports.iterdir() if p.is_file()) == [
            "history-old.md",
            "latest-report.md",
        ]

    def test_stable_prefix_always_preserved_in_both_modes(self, tmp_path: Path) -> None:
        for dry_run in (True, False):
            reports = tmp_path / f"mode-{'dry' if dry_run else 'exec'}"
            reports.mkdir(parents=True, exist_ok=True)
            _write(reports / "latest-a.md", 500)
            _write(reports / "latest-b.md", 500)
            _write(reports / "history-c.md", 500)

            summary = prune_report_directory(
                reports,
                max_history_files=0,
                max_total_bytes=10,
                dry_run=dry_run,
            )

            remaining = sorted(p.name for p in reports.iterdir() if p.is_file())
            assert "latest-a.md" in remaining
            assert "latest-b.md" in remaining
            assert summary.pruned == 1
