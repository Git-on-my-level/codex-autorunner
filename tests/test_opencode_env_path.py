import os
from pathlib import Path
from unittest.mock import patch

from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor


def _path_entries(value: str) -> list[str]:
    return [entry for entry in value.split(os.pathsep) if entry]


def test_build_opencode_env_includes_workspace_car_bin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shim_dir = workspace / ".codex-autorunner" / "bin"
    shim_dir.mkdir(parents=True)

    supervisor = OpenCodeSupervisor(
        ["/usr/bin/true"],
        base_env={"PATH": "/usr/bin"},
    )

    env = supervisor._build_opencode_env(workspace)
    entries = _path_entries(env["PATH"])

    assert str(shim_dir) in entries
    assert entries.index(str(shim_dir)) < entries.index("/usr/bin")


def test_build_opencode_env_includes_workspace_root_when_car_exists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "car").write_text("#!/bin/sh\n", encoding="utf-8")

    supervisor = OpenCodeSupervisor(
        ["/usr/bin/true"],
        base_env={"PATH": "/usr/bin"},
    )

    env = supervisor._build_opencode_env(workspace)
    entries = _path_entries(env["PATH"])

    assert str(workspace) in entries
    assert entries.index(str(workspace)) < entries.index("/usr/bin")


def test_build_opencode_env_workspace_shim_precedes_global_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shim_dir = workspace / ".codex-autorunner" / "bin"
    shim_dir.mkdir(parents=True)
    global_dir = tmp_path / "global-bin"
    global_dir.mkdir()

    supervisor = OpenCodeSupervisor(
        ["/usr/bin/true"],
        base_env={"PATH": f"{global_dir}{os.pathsep}/usr/bin"},
    )

    env = supervisor._build_opencode_env(workspace)
    entries = _path_entries(env["PATH"])

    assert str(shim_dir) in entries
    assert str(global_dir) in entries
    assert entries.index(str(shim_dir)) < entries.index(str(global_dir))


def test_build_opencode_env_no_shim_dir_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    supervisor = OpenCodeSupervisor(
        ["/usr/bin/true"],
        base_env={"PATH": "/usr/bin"},
    )

    env = supervisor._build_opencode_env(workspace)
    entries = _path_entries(env["PATH"])

    assert "/usr/bin" in entries
    shim_dir = workspace / ".codex-autorunner" / "bin"
    assert str(shim_dir) not in entries


def test_build_opencode_env_prefers_workspace_auth_over_process_home(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_home = tmp_path / "process-home"
    process_home.mkdir()
    inferred_home = tmp_path / "inferred-home"
    auth_path = inferred_home / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(process_home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    supervisor = OpenCodeSupervisor(
        ["/usr/bin/true"],
        base_env={"PATH": "/usr/bin"},
    )

    with (
        patch(
            "codex_autorunner.core.utils.infer_home_from_workspace",
            return_value=inferred_home,
        ),
        patch(
            "codex_autorunner.agents.opencode.supervisor.infer_home_from_workspace",
            return_value=inferred_home,
        ),
    ):
        env = supervisor._build_opencode_env(workspace)

    assert env["HOME"] == str(inferred_home)
    assert env["XDG_DATA_HOME"] == str(inferred_home / ".local" / "share")
