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
