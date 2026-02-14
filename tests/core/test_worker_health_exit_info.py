from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from codex_autorunner.core.flows.worker_process import (
    check_worker_health,
    write_worker_exit_info,
)


def test_check_worker_health_dead_includes_stderr_tail(tmp_path: Path) -> None:
    repo_root = tmp_path
    run_id = str(uuid.uuid4())
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    (artifacts_dir / "worker.json").write_text(
        json.dumps({"pid": 999999, "cmd": [], "repo_root": str(repo_root)}),
        encoding="utf-8",
    )
    (artifacts_dir / "worker.err.log").write_text(
        "line1\nline2\nline3\nline4\nline5\nline6\n", encoding="utf-8"
    )

    health = check_worker_health(repo_root, run_id)
    assert health.status == "dead"
    assert health.stderr_tail is not None
    assert "line6" in health.stderr_tail


def test_write_worker_exit_info_creates_exit_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    run_id = str(uuid.uuid4())
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    spawned_at = time.time()
    (artifacts_dir / "worker.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "cmd": [],
                "repo_root": str(repo_root),
                "spawned_at": spawned_at,
            }
        ),
        encoding="utf-8",
    )
    (artifacts_dir / "worker.err.log").write_text(
        "error line 1\nerror line 2\n", encoding="utf-8"
    )

    write_worker_exit_info(repo_root, run_id, returncode=1)

    exit_path = artifacts_dir / "worker.exit.json"
    assert exit_path.exists()
    exit_data = json.loads(exit_path.read_text(encoding="utf-8"))
    assert exit_data["returncode"] == 1
    assert exit_data["pid"] == 12345
    assert exit_data["stderr_tail"] is not None
    assert "error line" in exit_data["stderr_tail"]


def test_check_worker_health_dead_uses_exit_info(tmp_path: Path) -> None:
    repo_root = tmp_path
    run_id = str(uuid.uuid4())
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    spawned_at = time.time()
    (artifacts_dir / "worker.json").write_text(
        json.dumps(
            {
                "pid": 999991,
                "cmd": [],
                "repo_root": str(repo_root),
                "spawned_at": spawned_at,
            }
        ),
        encoding="utf-8",
    )
    (artifacts_dir / "worker.err.log").write_text("stderr from log\n", encoding="utf-8")

    write_worker_exit_info(repo_root, run_id, returncode=42)

    health = check_worker_health(repo_root, run_id)
    assert health.status == "dead"
    assert health.exit_code == 42
    assert health.stderr_tail is not None
