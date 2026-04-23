from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from codex_autorunner.core.app_server_command import GLOBAL_APP_SERVER_COMMAND_ENV
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.core.state import RunnerState
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


def test_build_app_server_supervisor_factory_preserves_runtime_policy_settings(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)
            captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _FakeSupervisor,
    )

    logger = logging.getLogger("test.destination_wiring")
    notification_handler = object()
    factory = build_app_server_supervisor_factory(config, logger=logger)
    factory("autorunner", notification_handler)

    kwargs = captured["kwargs"]
    assert captured["command"] == config.app_server.command
    assert kwargs["logger"] is logger
    assert kwargs["notification_handler"] is notification_handler
    assert kwargs["auto_restart"] == config.app_server.auto_restart
    assert kwargs["request_timeout"] == config.app_server.request_timeout
    assert (
        kwargs["turn_stall_timeout_seconds"]
        == config.app_server.turn_stall_timeout_seconds
    )
    assert (
        kwargs["turn_stall_poll_interval_seconds"]
        == config.app_server.turn_stall_poll_interval_seconds
    )
    assert (
        kwargs["turn_stall_recovery_min_interval_seconds"]
        == config.app_server.turn_stall_recovery_min_interval_seconds
    )
    assert (
        kwargs["turn_stall_max_recovery_attempts"]
        == config.app_server.turn_stall_max_recovery_attempts
    )
    assert kwargs["output_policy"] == config.app_server.output.policy


def test_build_app_server_supervisor_factory_prefers_global_env_override(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(GLOBAL_APP_SERVER_COMMAND_ENV, "/env/codex app-server --global")
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _FakeSupervisor,
    )

    factory = build_app_server_supervisor_factory(config)
    factory("autorunner", None)

    assert captured["command"] == ["/env/codex", "app-server", "--global"]


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


def test_agent_backend_factory_reuses_and_closes_cached_supervisors(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)

    class _FakeSupervisor:
        def __init__(self, command=None, **kwargs):  # type: ignore[no-untyped-def]
            self.command = list(command or [])
            self.kwargs = dict(kwargs)
            self.close_calls = 0

        async def close_all(self) -> None:
            self.close_calls += 1

    codex_supervisors: list[_FakeSupervisor] = []
    opencode_supervisors: list[_FakeSupervisor] = []

    def _fake_workspace_supervisor(command, **kwargs):  # type: ignore[no-untyped-def]
        supervisor = _FakeSupervisor(command, **kwargs)
        codex_supervisors.append(supervisor)
        return supervisor

    def _fake_opencode_supervisor(_config, **kwargs):  # type: ignore[no-untyped-def]
        supervisor = _FakeSupervisor(["opencode", "serve"], **kwargs)
        opencode_supervisors.append(supervisor)
        return supervisor

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.WorkspaceAppServerSupervisor",
        _fake_workspace_supervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_opencode_supervisor_from_repo_config",
        _fake_opencode_supervisor,
    )

    factory = AgentBackendFactory(repo_root, config)

    codex_first = factory._ensure_codex_supervisor()
    codex_second = factory._ensure_codex_supervisor()
    opencode_first = factory._ensure_opencode_supervisor()
    opencode_second = factory._ensure_opencode_supervisor()

    assert codex_first is codex_second
    assert opencode_first is opencode_second
    assert len(codex_supervisors) == 1
    assert len(opencode_supervisors) == 1

    asyncio.run(factory.close_all())
    asyncio.run(factory.close_all())

    assert codex_first.close_calls == 1
    assert opencode_first.close_calls == 1
    assert factory._codex_supervisor is None
    assert factory._opencode_supervisor is None


def test_agent_backend_factory_reuses_shared_opencode_supervisor_without_owning_it(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)

    class _SharedSupervisor:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close_all(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_opencode_supervisor_from_repo_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not build a new supervisor")
        ),
    )

    shared = _SharedSupervisor()
    factory = AgentBackendFactory(
        repo_root,
        config,
        shared_opencode_supervisor=shared,
    )

    assert factory._ensure_opencode_supervisor() is shared
    asyncio.run(factory.close_all())

    assert shared.close_calls == 0
    assert factory._opencode_supervisor is None


def test_agent_backend_factory_uses_alias_backed_opencode_runtime_config(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root, repo_root = _make_repo_config(tmp_path)
    config = load_repo_config(repo_root, hub_path=hub_root)
    config.agents["opencode-review"] = config.agents["opencode"].__class__(
        backend="opencode",
        binary="opencode",
        serve_command=None,
        base_url="http://127.0.0.1:4455",
        subagent_models=None,
    )

    captured: dict[str, object] = {}

    class _FakeOpenCodeBackend:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        def configure(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.OpenCodeBackend",
        _FakeOpenCodeBackend,
    )

    factory = AgentBackendFactory(repo_root, config)
    backend = factory(
        "opencode-review",
        RunnerState(None, "idle", None, None, None),
        None,
    )

    assert isinstance(backend, _FakeOpenCodeBackend)
    assert captured["base_url"] == "http://127.0.0.1:4455"


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
