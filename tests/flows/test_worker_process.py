from __future__ import annotations

import sys

from codex_autorunner.core.flows import worker_process


def test_check_worker_health_prefers_metadata_cmdline(monkeypatch, tmp_path):
    """Worker health should trust stored cmdline, not the current interpreter."""

    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    artifacts_dir = worker_process._worker_artifacts_dir(tmp_path, run_id)

    stored_cmd = [
        f"{sys.executable}-other",
        "-m",
        "codex_autorunner",
        "flow",
        "worker",
        "--repo",
        str(tmp_path),
        "--run-id",
        worker_process._normalized_run_id(run_id),
    ]
    # Sanity-check that we're simulating a different interpreter than the test runner.
    assert stored_cmd[0] != sys.executable

    worker_process._write_worker_metadata(
        worker_process._worker_metadata_path(artifacts_dir),
        pid=12345,
        cmd=stored_cmd,
        repo_root=tmp_path,
    )

    monkeypatch.setattr(worker_process, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(
        worker_process, "_read_process_cmdline", lambda pid: list(stored_cmd)
    )

    health = worker_process.check_worker_health(tmp_path, run_id)

    assert health.status == "alive"
    assert health.cmdline == stored_cmd
