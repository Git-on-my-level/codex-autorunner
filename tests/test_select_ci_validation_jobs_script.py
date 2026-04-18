from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1] / "scripts" / "select_ci_validation_jobs.py"
    )


def test_select_ci_validation_jobs_script_json_output() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "--format",
            "json",
            "src/codex_autorunner/core/update_targets.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "lane": "core",
        "reason": "single-lane-diff",
        "paths": ["src/codex_autorunner/core/update_targets.py"],
        "lanes_touched": ["core"],
        "lane_paths": {"core": ["src/codex_autorunner/core/update_targets.py"]},
        "shared_risk_paths": [],
        "unknown_paths": [],
        "run_core": True,
        "run_web_ui": False,
        "run_chat_apps": False,
        "run_aggregate": False,
    }


def test_select_ci_validation_jobs_script_writes_github_outputs(tmp_path: Path) -> None:
    github_output_path = tmp_path / "github-output.txt"
    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "--github-output",
            str(github_output_path),
        ],
        check=False,
        capture_output=True,
        input="scripts/check.sh\n",
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "lane: aggregate" in result.stdout

    outputs = dict(
        line.split("=", 1)
        for line in github_output_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert outputs == {
        "lane": "aggregate",
        "reason": "shared-risk-path",
        "run_core": "false",
        "run_web_ui": "false",
        "run_chat_apps": "false",
        "run_aggregate": "true",
    }


def test_select_ci_validation_jobs_script_runs_without_site_packages() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(_script_path()),
            "--format",
            "json",
            ".github/workflows/ci.yml",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lane"] == "aggregate"
