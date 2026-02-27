from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.integrations.agents.destination_wrapping import WrappedCommand
from codex_autorunner.integrations.agents.wiring import (
    AgentBackendFactory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.manifest import load_manifest, save_manifest
from tests.conftest import write_test_config


def _make_repo_config(tmp_path: Path) -> tuple[Path, Path]:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(repo_root / REPO_OVERRIDE_FILENAME, {})
    return hub_root, repo_root


def test_build_app_server_supervisor_factory_local_command_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)
    config.effective_destination = {"kind": "local"}

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)
            captured["state_root"] = kwargs.get("state_root")

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _FakeSupervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.wrap_command_for_destination",
        lambda **_: (_ for _ in ()).throw(AssertionError("should not wrap local")),
    )

    factory = build_app_server_supervisor_factory(config)
    factory("autorunner", None)

    assert captured["command"] == config.app_server.command
    assert captured["state_root"] == config.app_server.state_root


def test_build_app_server_supervisor_factory_docker_wraps_command(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)
    config.effective_destination = {"kind": "docker", "image": "busybox:latest"}

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)
            captured["state_root"] = kwargs.get("state_root")

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _FakeSupervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.wrap_command_for_destination",
        lambda **_: WrappedCommand(
            command=["docker", "exec", "car-ws-123", "codex", "app-server"],
            state_root_override=repo_root
            / ".codex-autorunner"
            / "app_server_workspaces",
        ),
    )

    factory = build_app_server_supervisor_factory(config)
    factory("autorunner", None)

    assert captured["command"] == [
        "docker",
        "exec",
        "car-ws-123",
        "codex",
        "app-server",
    ]
    assert (
        captured["state_root"]
        == repo_root / ".codex-autorunner" / "app_server_workspaces"
    )


def test_agent_backend_factory_codex_supervisor_wraps_for_docker(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)
    config.effective_destination = {"kind": "docker", "image": "busybox:latest"}

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)
            captured["state_root"] = kwargs.get("state_root")

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _FakeSupervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.wrap_command_for_destination",
        lambda **_: WrappedCommand(
            command=["docker", "exec", "ctr", "codex", "app-server"],
            state_root_override=repo_root
            / ".codex-autorunner"
            / "app_server_workspaces",
        ),
    )

    factory = AgentBackendFactory(repo_root, config)
    factory._ensure_codex_supervisor()

    assert captured["command"] == ["docker", "exec", "ctr", "codex", "app-server"]
    assert (
        captured["state_root"]
        == repo_root / ".codex-autorunner" / "app_server_workspaces"
    )


def test_agent_backend_factory_passes_docker_override_to_opencode_factory(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)
    config.effective_destination = {"kind": "docker", "image": "busybox:latest"}

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.wrap_command_for_destination",
        lambda **_: WrappedCommand(
            command=["docker", "exec", "ctr", "opencode", "serve"]
        ),
    )

    def _fake_build_opencode_supervisor_from_repo_config(
        _config, **kwargs
    ):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "supervisor"

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_opencode_supervisor_from_repo_config",
        _fake_build_opencode_supervisor_from_repo_config,
    )

    factory = AgentBackendFactory(repo_root, config)
    supervisor = factory._ensure_opencode_supervisor()
    assert supervisor == "supervisor"
    assert captured["command_override"] == [
        "docker",
        "exec",
        "ctr",
        "opencode",
        "serve",
    ]


def test_derive_repo_config_sets_effective_destination_from_manifest(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    workspace_root = hub_root / "workspace"
    worktrees_root = hub_root / "worktrees"
    base_repo_root = workspace_root / "base"
    wt_repo_root = worktrees_root / "base--feat"
    base_repo_root.mkdir(parents=True)
    wt_repo_root.mkdir(parents=True)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    base = manifest.ensure_repo(hub_root, base_repo_root, repo_id="base", kind="base")
    base.destination = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    manifest.ensure_repo(
        hub_root,
        wt_repo_root,
        repo_id="base--feat",
        kind="worktree",
        worktree_of="base",
    )
    save_manifest(hub_config.manifest_path, manifest, hub_root)

    base_cfg = derive_repo_config(hub_config, base_repo_root, load_env=False)
    wt_cfg = derive_repo_config(hub_config, wt_repo_root, load_env=False)
    assert base_cfg.effective_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
    assert wt_cfg.effective_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
