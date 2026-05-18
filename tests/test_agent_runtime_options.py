from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from codex_autorunner.agents.runtime_options import (
    AgentRuntimeOptionsError,
    resolve_agent_runtime_options,
)
from codex_autorunner.core.agent_config import AgentConfig, AgentProfileConfig
from codex_autorunner.core.config import CONFIG_FILENAME, load_repo_config
from codex_autorunner.core.state import RunnerState
from tests.conftest import write_test_config


def _config(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    return load_repo_config(repo_root, hub_path=hub_root)


def _state(**kwargs):
    return RunnerState(None, "idle", None, None, None, **kwargs)


def test_codex_options_resolve_defaults_and_workspace_write_sandbox(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state = _state(
        autorunner_approval_policy="unlessTrusted",
        autorunner_sandbox_mode="workspaceWrite",
        autorunner_workspace_write_network=True,
    )

    options = resolve_agent_runtime_options(
        "codex",
        state=state,
        config=config,
        workspace_root=tmp_path,
    )

    assert options.runtime_kind == "codex"
    assert options.model == "gpt-5.5"
    assert options.effective_approval_policy == "unlessTrusted"
    assert options.sandbox.mode == "workspaceWrite"
    assert options.sandbox.policy == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path)],
        "networkAccess": True,
    }
    assert options.turn_timeout_seconds == config.app_server.turn_timeout_seconds
    assert options.output_policy == config.app_server.output.policy


def test_opencode_options_include_protocol_model_payload(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _state(autorunner_effort_override="high")

    options = resolve_agent_runtime_options(
        "opencode",
        state=state,
        config=config,
        workspace_root=tmp_path,
    )

    assert options.runtime_kind == "opencode"
    assert options.model == "zai-coding-plan/glm-5.1"
    assert options.opencode_model_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    assert options.reasoning == "high"
    assert options.session_stall_timeout_seconds == (
        config.opencode.session_stall_timeout_seconds
    )


def test_profile_resolves_to_runtime_profile(tmp_path: Path) -> None:
    config = _config(tmp_path)
    opencode_cfg = config.agents["opencode"]
    config.agents["opencode"] = dataclasses.replace(
        opencode_cfg,
        profiles={"fast": AgentProfileConfig(base_url="http://fast.example")},
    )

    options = resolve_agent_runtime_options(
        "opencode",
        profile="fast",
        state=_state(),
        config=config,
        workspace_root=tmp_path,
    )

    assert options.requested_agent_id == "opencode"
    assert options.requested_profile == "fast"
    assert options.logical_agent_id == "opencode"
    assert options.runtime_agent_id == "opencode"
    assert options.runtime_profile == "fast"
    assert options.resolution_kind == "canonical_profile"


def test_configured_alias_keeps_runtime_kind(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["reviewer"] = AgentConfig(
        backend="opencode",
        binary="opencode",
        serve_command=None,
        base_url="http://reviewer.example",
        subagent_models=None,
    )

    options = resolve_agent_runtime_options(
        "reviewer",
        state=_state(),
        config=config,
        workspace_root=tmp_path,
    )

    assert options.requested_agent_id == "reviewer"
    assert options.runtime_agent_id == "reviewer"
    assert options.runtime_kind == "opencode"


def test_explicit_model_override_wins(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _state(autorunner_model_overrides={"opencode": "zai/settings"})

    options = resolve_agent_runtime_options(
        "opencode",
        state=state,
        config=config,
        workspace_root=tmp_path,
        explicit_model="zai/explicit",
    )

    assert options.model == "zai/explicit"
    assert options.opencode_model_payload == {
        "providerID": "zai",
        "modelID": "explicit",
    }


def test_hermes_options_are_resolved_without_opencode_payload(tmp_path: Path) -> None:
    config = _config(tmp_path)

    options = resolve_agent_runtime_options(
        "hermes",
        state=_state(),
        config=config,
        workspace_root=tmp_path,
    )

    assert options.runtime_kind == "hermes"
    assert options.model is None
    assert options.opencode_model_payload is None


def test_rejects_unsupported_open_code_model_shape(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(AgentRuntimeOptionsError, match="provider/model"):
        resolve_agent_runtime_options(
            "opencode",
            state=_state(),
            config=config,
            workspace_root=tmp_path,
            explicit_model="glm-5.1",
        )


def test_rejects_workspace_write_without_workspace_root(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(AgentRuntimeOptionsError, match="workspace root"):
        resolve_agent_runtime_options(
            "codex",
            state=_state(autorunner_sandbox_mode="workspaceWrite"),
            config=config,
        )
