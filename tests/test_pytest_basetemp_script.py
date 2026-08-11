from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pytest_basetemp_script_prints_run_scoped_path(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    run_token = "script-run"
    monkeypatch.setenv("CAR_PYTEST_RUN_TOKEN", run_token)

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "pytest_basetemp.py"),
            "--repo-root",
            str(tmp_path / "repo"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    basetemp = Path(result.stdout.strip())
    assert basetemp.name == "basetemp"
    assert basetemp.parent.name == run_token
    assert basetemp.parent.parent.name == "t"
    assert basetemp.parent.parent.parent.name.startswith("cp-")


def test_check_script_gives_parallel_chat_apps_lane_a_disjoint_basetemp() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    check_script = (repo_root / "scripts" / "check.sh").read_text()

    assert (
        'CHAT_APPS_PYTEST_RUN_TOKEN="${CAR_PYTEST_RUN_TOKEN}-chat-apps"' in check_script
    )
    assert 'CAR_PYTEST_RUN_TOKEN="$CHAT_APPS_PYTEST_RUN_TOKEN"' in check_script
    assert 'PYTEST_BASETEMP="$CHAT_APPS_PYTEST_BASETEMP"' in check_script
