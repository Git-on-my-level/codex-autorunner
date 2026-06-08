from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path("scripts/safe-refresh-local-linux-hub.sh")


def test_linux_wrapper_delegates_to_update_engine_runner() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "codex_autorunner.core.update.runner" in script
    # Orchestration now lives in Python; the bash should not re-implement it.
    assert "run_timed_phase() {" not in script
    assert "write_status() {" not in script


def test_linux_wrapper_preserves_env_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for var in (
        "PACKAGE_SRC",
        "UPDATE_STATUS_PATH",
        "UPDATE_TARGET",
        "UPDATE_BACKEND",
        "HELPER_PYTHON",
        "SYSTEMD_SCOPE",
        "UPDATE_HUB_SERVICE_NAME",
        "UPDATE_TELEGRAM_SERVICE_NAME",
        "UPDATE_DISCORD_SERVICE_NAME",
    ):
        assert var in script, var


def test_linux_wrapper_requires_status_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT.resolve())],
        cwd=SCRIPT.resolve().parent.parent,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "UPDATE_STATUS_PATH": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 1
    assert "UPDATE_STATUS_PATH is required" in output
    assert "syntax error" not in output


def test_linux_wrapper_execs_runner_with_source_pythonpath(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-python"
    argv_log = tmp_path / "argv.txt"
    env_log = tmp_path / "env.txt"
    fake_python.write_text(
        (
            '#!/bin/sh\nif [ "$1" = "-c" ]; then exec "$REAL_PYTHON" "$@"; fi\n'
            'printf "%s\\n" "$@" > "$ARGV_LOG"\n'
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
            "REAL_PYTHON": sys.executable,
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
    pythonpath = env_log.read_text(encoding="utf-8")
    assert str(SCRIPT.resolve().parent.parent / "src") in pythonpath


def test_linux_wrapper_allows_missing_package_src(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-python"
    argv_log = tmp_path / "argv.txt"
    env_log = tmp_path / "env.txt"
    missing_src = tmp_path / "missing-cache"
    fake_python.write_text(
        (
            '#!/bin/sh\nif [ "$1" = "-c" ]; then exec "$REAL_PYTHON" "$@"; fi\n'
            'printf "%s\\n" "$@" > "$ARGV_LOG"\n'
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
            "REAL_PYTHON": sys.executable,
            "ARGV_LOG": str(argv_log),
            "ENV_LOG": str(env_log),
            "UPDATE_STATUS_PATH": str(tmp_path / "update_status.json"),
            "PACKAGE_SRC": str(missing_src),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(missing_src / "src") in env_log.read_text(encoding="utf-8")
    assert str(missing_src) in argv_log.read_text(encoding="utf-8")


def test_linux_wrapper_passes_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT.resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
