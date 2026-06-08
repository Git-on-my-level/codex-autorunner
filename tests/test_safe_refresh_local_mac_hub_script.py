from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path("scripts/safe-refresh-local-mac-hub.sh")


def test_mac_wrapper_delegates_to_update_engine_runner() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "codex_autorunner.core.update.runner" in script
    assert "--backend launchd" in script
    # Orchestration now lives in Python; the bash should not re-implement it.
    assert "_run_timed_phase()" not in script
    assert "launchctl kickstart" not in script


def test_mac_wrapper_preserves_env_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for var in (
        "PACKAGE_SRC",
        "UPDATE_STATUS_PATH",
        "UPDATE_TARGET",
        "HELPER_PYTHON",
        "CURRENT_VENV_LINK",
    ):
        assert var in script, var


def test_mac_wrapper_execs_runner_with_launchd_backend(tmp_path: Path) -> None:
    # Stub HELPER_PYTHON so the wrapper's exec is captured instead of running
    # a real update.
    fake_python = tmp_path / "fake-python"
    argv_log = tmp_path / "argv.txt"
    env_log = tmp_path / "env.txt"
    fake_python.write_text(
        (
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_LOG"\n'
            'printf "%s\\n" "$PYTHONPATH" > "$ENV_LOG"\nexit 0\n'
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = subprocess.run(
        [str(SCRIPT.resolve())],
        cwd=SCRIPT.resolve().parent.parent,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HELPER_PYTHON": str(fake_python),
            "ARGV_LOG": str(argv_log),
            "ENV_LOG": str(env_log),
            "UPDATE_STATUS_PATH": str(tmp_path / "update_status.json"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = argv_log.read_text(encoding="utf-8")
    assert "codex_autorunner.core.update.runner" in argv
    assert "--backend" in argv
    assert "launchd" in argv
    pythonpath = env_log.read_text(encoding="utf-8")
    assert str(SCRIPT.resolve().parent.parent / "src") in pythonpath


def test_mac_wrapper_passes_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT.resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
