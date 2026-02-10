from __future__ import annotations

import json
import uuid
from pathlib import Path

from codex_autorunner.core.flows.worker_process import check_worker_health


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

