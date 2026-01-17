import subprocess
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.snapshot import (
    SeedContext,
    summarize_changes,
)


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed: {cmd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _run(["git", "init"], repo_root)
    _run(["git", "config", "user.email", "test@example.com"], repo_root)
    _run(["git", "config", "user.name", "Test User"], repo_root)
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=True)
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo_root)
    _run(["git", "commit", "-m", "initial"], repo_root)
    return repo_root


def test_summarize_changes_prefers_git_diff_name_status(git_repo: Path) -> None:
    engine = Engine(git_repo)
    prev_sha = _run(["git", "rev-parse", "HEAD"], git_repo).strip()

    (git_repo / "README.md").write_text("hello world\n", encoding="utf-8")
    _run(["git", "add", "README.md"], git_repo)
    _run(["git", "commit", "-m", "update readme"], git_repo)

    seed = SeedContext(
        text="seed",
        bytes_read=0,
        file_hashes={},
        head_sha=_run(["git", "rev-parse", "HEAD"], git_repo).strip(),
        branch=None,
        seed_hash="seedhash",
    )
    out = summarize_changes(
        engine, previous_state={"head_sha": prev_sha}, current_seed=seed
    )
    assert "git diff --name-status" in out
    assert "README.md" in out


def test_summarize_changes_falls_back_to_git_status_for_working_tree(
    git_repo: Path,
) -> None:
    engine = Engine(git_repo)
    prev_sha = _run(["git", "rev-parse", "HEAD"], git_repo).strip()
    (git_repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

    seed = SeedContext(
        text="seed",
        bytes_read=0,
        file_hashes={},
        head_sha=prev_sha,
        branch=None,
        seed_hash="seedhash",
    )
    out = summarize_changes(
        engine, previous_state={"head_sha": prev_sha}, current_seed=seed
    )
    assert "git status --porcelain" in out
    assert "README.md" in out


def test_summarize_changes_falls_back_to_seed_hash_diffs(repo: Path) -> None:
    engine = Engine(repo)
    seed = SeedContext(
        text="seed",
        bytes_read=0,
        file_hashes={"a.txt": "new"},
        head_sha=None,
        branch=None,
        seed_hash="seedhash",
    )
    out = summarize_changes(
        engine,
        previous_state={"seed_file_hashes": {"a.txt": "old"}, "head_sha": None},
        current_seed=seed,
    )
    assert "seed inputs only" in out
    assert "`a.txt`" in out


def test_generate_snapshot_persists_state(repo: Path, monkeypatch) -> None:
    import asyncio

    from codex_autorunner.core.snapshot import SnapshotResult, SnapshotService

    engine = Engine(repo)

    # Mock SnapshotService.generate_snapshot to simulate agent writing the file
    async def mock_generate(self):
        content = "# Repo Snapshot\n\n" + ("a" * 500)
        snap_path = self.engine.repo_root / ".codex-autorunner" / "SNAPSHOT.md"
        state_path = self.engine.repo_root / ".codex-autorunner" / "snapshot_state.json"

        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(content, encoding="utf-8")

        state = {"truncated": False}
        import json

        state_path.write_text(json.dumps(state), encoding="utf-8")

        return SnapshotResult(content=content, truncated=False, state=state)

    monkeypatch.setattr(SnapshotService, "generate_snapshot", mock_generate)

    # Use the service instead of the standalone function
    service = SnapshotService(engine)
    result = asyncio.run(service.generate_snapshot())

    snap_path = repo / ".codex-autorunner" / "SNAPSHOT.md"
    state_path = repo / ".codex-autorunner" / "snapshot_state.json"
    assert snap_path.exists()
    assert state_path.exists()
    assert result.truncated is False
    assert "<!-- TRUNCATED" not in snap_path.read_text(encoding="utf-8")
