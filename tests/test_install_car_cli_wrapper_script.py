from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_install_car_cli_wrapper_dispatches_to_current_venv(tmp_path: Path) -> None:
    script_path = Path("scripts/install-car-cli-wrapper.sh").resolve()
    current_link = tmp_path / "pipx" / "venvs" / "codex-autorunner.current"
    current_target = tmp_path / "pipx" / "venvs" / "codex-autorunner.next-test"
    bin_dir = current_target / "bin"
    local_bin = tmp_path / "bin"
    log_path = tmp_path / "python-args.txt"

    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(
        "#!/usr/bin/env bash\n" f"printf '%s\\n' \"$@\" > {log_path}\n",
        encoding="utf-8",
    )
    (bin_dir / "python").chmod(0o755)
    current_link.parent.mkdir(parents=True, exist_ok=True)
    current_link.symlink_to(current_target)

    result = subprocess.run(
        [str(script_path)],
        env={
            **os.environ,
            "CURRENT_VENV_LINK": str(current_link),
            "LOCAL_BIN": str(local_bin),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    car_wrapper = local_bin / "car"
    assert car_wrapper.exists()
    assert os.access(car_wrapper, os.X_OK)
    assert f"{current_link}/bin/python" in car_wrapper.read_text(encoding="utf-8")

    run_result = subprocess.run(
        [str(car_wrapper), "--version"],
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run_result.returncode == 0, run_result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "codex_autorunner.cli",
        "--version",
    ]


def test_car_cli_wrapper_reports_missing_current_symlink(tmp_path: Path) -> None:
    script_path = Path("scripts/install-car-cli-wrapper.sh").resolve()
    current_link = tmp_path / "pipx" / "venvs" / "codex-autorunner.current"
    local_bin = tmp_path / "bin"

    result = subprocess.run(
        [str(script_path)],
        env={
            **os.environ,
            "CURRENT_VENV_LINK": str(current_link),
            "LOCAL_BIN": str(local_bin),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_result = subprocess.run(
        [str(local_bin / "car"), "--version"],
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run_result.returncode == 127
    assert f"active codex-autorunner venv symlink is missing: {current_link}" in (
        run_result.stderr
    )
    assert "run the hub refresh script" in run_result.stderr
