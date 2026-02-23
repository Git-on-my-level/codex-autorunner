import os
from pathlib import Path

from codex_autorunner.core.app_server_utils import (
    app_server_env,
    build_app_server_env,
    seed_codex_home,
)


def _path_entries(value: str) -> list[str]:
    return [entry for entry in value.split(os.pathsep) if entry]


def test_build_app_server_env_prepends_workspace_shim_without_git_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shim_dir = workspace / ".codex-autorunner" / "bin"
    shim_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"

    env = build_app_server_env(
        ["/bin/sh", "-c", "true"],
        workspace,
        state_dir,
        base_env={"PATH": "/usr/bin"},
    )

    entries = _path_entries(env["PATH"])
    assert entries[0] == str(shim_dir)
    assert env["CODEX_HOME"] == str(state_dir / "codex_home")


def test_app_server_env_includes_workspace_root_only_when_root_car_exists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    env_without_root_car = app_server_env(
        ["/bin/sh", "-c", "true"],
        workspace,
        base_env={"PATH": "/usr/bin"},
    )
    entries_without_root_car = _path_entries(env_without_root_car["PATH"])
    assert str(workspace) not in entries_without_root_car

    (workspace / "car").write_text("#!/bin/sh\n", encoding="utf-8")
    env_with_root_car = app_server_env(
        ["/bin/sh", "-c", "true"],
        workspace,
        base_env={"PATH": "/usr/bin"},
    )
    entries_with_root_car = _path_entries(env_with_root_car["PATH"])
    assert str(workspace) in entries_with_root_car
    assert entries_with_root_car.index(str(workspace)) < entries_with_root_car.index(
        "/usr/bin"
    )


def test_app_server_env_workspace_shim_precedes_global_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shim_dir = workspace / ".codex-autorunner" / "bin"
    shim_dir.mkdir(parents=True)
    global_dir = tmp_path / "global-bin"
    global_dir.mkdir()

    env = app_server_env(
        ["/bin/sh", "-c", "true"],
        workspace,
        base_env={"PATH": f"{global_dir}{os.pathsep}/usr/bin"},
    )
    entries = _path_entries(env["PATH"])
    assert str(shim_dir) in entries
    assert str(global_dir) in entries
    assert entries.index(str(shim_dir)) < entries.index(str(global_dir))


def test_seed_codex_home_copies_global_config_files(
    tmp_path: Path, monkeypatch
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text('{"token":"abc"}\n', encoding="utf-8")
    source_config_toml = source_home / "config.toml"
    source_config_toml.write_text("multi_agent = true\n", encoding="utf-8")
    source_config_json = source_home / "config.json"
    source_config_json.write_text('{"model":"gpt-5.3-codex"}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    target_home = tmp_path / "target-codex-home"
    target_home.mkdir()

    seed_codex_home(target_home, event_prefix="test")

    target_auth = target_home / "auth.json"
    assert target_auth.is_symlink()
    assert target_auth.resolve() == source_auth.resolve()
    assert (target_home / "config.toml").read_text(
        encoding="utf-8"
    ) == source_config_toml.read_text(encoding="utf-8")
    assert (target_home / "config.json").read_text(
        encoding="utf-8"
    ) == source_config_json.read_text(encoding="utf-8")


def test_seed_codex_home_resyncs_config_when_auth_already_seeded(
    tmp_path: Path, monkeypatch
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"token":"abc"}\n', encoding="utf-8")
    source_config_toml = source_home / "config.toml"
    source_config_toml.write_text("multi_agent = false\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    target_home = tmp_path / "target-codex-home"
    target_home.mkdir()

    seed_codex_home(target_home, event_prefix="test")
    source_config_toml.write_text("multi_agent = true\n", encoding="utf-8")
    seed_codex_home(target_home, event_prefix="test")

    assert (target_home / "config.toml").read_text(
        encoding="utf-8"
    ) == "multi_agent = true\n"
