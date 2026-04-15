from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "select_validation_lane.py"


def test_select_validation_lane_script_json_output() -> None:
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
        "shared_risk_paths": [],
        "unknown_paths": [],
    }


def test_select_validation_lane_script_text_output_from_stdin() -> None:
    result = subprocess.run(
        [sys.executable, str(_script_path())],
        check=False,
        capture_output=True,
        input="scripts/check.sh\n",
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.strip() == "\n".join(
        [
            "lane: aggregate",
            "reason: shared-risk-path",
            "changed_files: 1",
            "shared_risk_paths: scripts/check.sh",
        ]
    )


def test_select_validation_lane_script_runs_without_site_packages() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(_script_path()),
            "--format",
            "json",
            "src/codex_autorunner/core/update_targets.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lane"] == "core"
