"""Update worker spawn must escape the hub's systemd cgroup.

Regression tests: when the hub runs under a systemd service with
KillMode=control-group (the default), an update worker spawned with
``start_new_session=True`` still lives in the service cgroup. The worker
restarts the hub service during cutover, and systemd's stop kills the whole
cgroup — including the worker, before it can write the terminal update
status. The hub then reports:

    Update not running; last update may have crashed.

``_build_systemd_run_spawn`` must produce a systemd-run transient-unit
command on Linux when systemd-run is available, and ``None`` otherwise.
"""

from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.update import _facade


def _linux_with_systemd_run(monkeypatch, path: str = "/usr/bin/systemd-run") -> None:
    monkeypatch.setattr(_facade.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_facade, "resolve_executable", lambda name, env=None: path)


def test_systemd_run_spawn_builds_transient_unit_command(monkeypatch) -> None:
    _linux_with_systemd_run(monkeypatch)

    result = _facade._build_systemd_run_spawn(
        cmd=[
            "python3",
            "-m",
            "codex_autorunner.core.update.runner",
            "--repo-url",
            "x",
        ],
        log_path=Path("/tmp/update-standalone.log"),
        env={"PYTHONPATH": "/tmp/update-src", "INVOCATION_ID": "abc"},
        sudo_prefix=["sudo", "-n"],
    )

    assert result is not None
    argv, env = result

    assert argv[:3] == ["sudo", "-n", "/usr/bin/systemd-run"]
    assert "--collect" in argv
    assert argv[argv.index("--unit") + 1].startswith("codex-autorunner-update-")
    assert "--same-dir" in argv
    assert "--setenv=PYTHONPATH=/tmp/update-src" in argv
    assert "--property=StandardOutput=append:/tmp/update-standalone.log" in argv
    assert "--property=StandardError=append:/tmp/update-standalone.log" in argv
    # worker argv is the tail
    assert argv[argv.index("python3") :] == [
        "python3",
        "-m",
        "codex_autorunner.core.update.runner",
        "--repo-url",
        "x",
    ]
    # INVOCATION_ID must be stripped so the transient unit is not tied to
    # the hub service identity
    assert "INVOCATION_ID" not in env
    assert env["PYTHONPATH"] == "/tmp/update-src"


def test_systemd_run_spawn_without_sudo_prefix(monkeypatch) -> None:
    _linux_with_systemd_run(monkeypatch)

    result = _facade._build_systemd_run_spawn(
        cmd=["python3", "-m", "codex_autorunner.core.update.runner"],
        log_path=Path("/tmp/update-standalone.log"),
        env={},
        sudo_prefix=None,
    )

    assert result is not None
    argv, _env = result
    assert argv[0] == "/usr/bin/systemd-run"
    assert not any(part == "sudo" for part in argv)


def test_systemd_run_spawn_returns_none_on_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(_facade.platform, "system", lambda: "Darwin")

    result = _facade._build_systemd_run_spawn(
        cmd=["python3", "-m", "codex_autorunner.core.update.runner"],
        log_path=Path("/tmp/update-standalone.log"),
        env={},
        sudo_prefix=None,
    )

    assert result is None


def test_systemd_run_spawn_returns_none_without_binary(monkeypatch) -> None:
    monkeypatch.setattr(_facade.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_facade, "resolve_executable", lambda name, env=None: None)

    result = _facade._build_systemd_run_spawn(
        cmd=["python3", "-m", "codex_autorunner.core.update.runner"],
        log_path=Path("/tmp/update-standalone.log"),
        env={},
        sudo_prefix=None,
    )

    assert result is None
