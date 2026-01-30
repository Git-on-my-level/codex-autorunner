import os
from pathlib import Path

from codex_autorunner.core.archive import archive_worktree_snapshot


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_worktree(tmp_path: Path) -> tuple[Path, Path]:
    base_repo = tmp_path / "base"
    worktree_repo = tmp_path / "worktree"
    base_repo.mkdir()
    worktree_repo.mkdir()

    car_root = worktree_repo / ".codex-autorunner"
    (car_root / "workspace").mkdir(parents=True)
    (car_root / "tickets").mkdir(parents=True)
    (car_root / "runs" / "run-1" / "dispatch").mkdir(parents=True)
    (car_root / "flows").mkdir(parents=True)

    _write(car_root / "workspace" / "notes.txt", "hello")
    _write(car_root / "tickets" / "TICKET-001.md", "ticket")
    _write(car_root / "runs" / "run-1" / "dispatch" / "DISPATCH.md", "dispatch")
    _write(car_root / "flows.db", "flows-db")
    _write(car_root / "config.yml", "config")
    _write(car_root / "state.sqlite3", "state")
    _write(car_root / "codex-autorunner.log", "log-a")
    _write(car_root / "codex-server.log", "log-b")

    run_one = car_root / "flows" / "11111111-1111-1111-1111-111111111111"
    run_two = car_root / "flows" / "22222222-2222-2222-2222-222222222222"
    run_one.mkdir(parents=True)
    run_two.mkdir(parents=True)
    _write(run_one / "meta.json", "one")
    _write(run_two / "meta.json", "two")
    os.utime(run_one, (100000, 100000))
    os.utime(run_two, (200000, 200000))

    return base_repo, worktree_repo


def test_archive_snapshot_copies_curated_paths(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert result.snapshot_path.exists()
    assert (result.snapshot_path / "workspace" / "notes.txt").read_text(
        encoding="utf-8"
    ) == "hello"
    assert (result.snapshot_path / "tickets" / "TICKET-001.md").exists()
    assert (
        result.snapshot_path / "runs" / "run-1" / "dispatch" / "DISPATCH.md"
    ).exists()
    assert (result.snapshot_path / "flows.db").exists()
    assert (result.snapshot_path / "config" / "config.yml").exists()
    assert (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert (result.snapshot_path / "logs" / "codex-autorunner.log").exists()
    assert (result.snapshot_path / "logs" / "codex-server.log").exists()

    meta = result.meta_path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in meta
    assert '"status": "complete"' in meta


def test_archive_snapshot_skips_symlink_escape(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    car_root = worktree_repo / ".codex-autorunner"

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    escape = car_root / "workspace" / "escape.txt"
    escape.symlink_to(secret)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert not (result.snapshot_path / "workspace" / "escape.txt").exists()


def test_archive_summary_counts_files_and_flows(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert result.flow_run_count == 2
    assert result.latest_flow_run_id == "22222222-2222-2222-2222-222222222222"

    expected_files = 10
    assert result.file_count == expected_files

    total_bytes = 0
    for path in result.snapshot_path.rglob("*"):
        if path.is_file() and path.name != "META.json":
            total_bytes += path.stat().st_size
    assert result.total_bytes == total_bytes
