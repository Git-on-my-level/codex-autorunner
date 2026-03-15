from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.destinations import (
    DockerDestination,
    DockerMount,
    LocalDestination,
    parse_destination_config,
    resolve_effective_agent_workspace_destination,
    resolve_effective_repo_destination,
    validate_destination_write_payload,
)
from codex_autorunner.manifest import ManifestAgentWorkspace, ManifestRepo


def test_parse_destination_config_local() -> None:
    parsed = parse_destination_config({"kind": "local"})
    assert parsed.valid is True
    assert isinstance(parsed.destination, LocalDestination)
    assert parsed.destination.to_dict() == {"kind": "local"}


def test_parse_destination_config_docker() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "container_name": "car-demo",
            "mounts": [{"source": "/tmp/src", "target": "/src", "read_only": True}],
            "env_passthrough": ["CAR_*", "OPENAI_*"],
            "env": {"OPENAI_API_KEY": "sk-test"},
            "workdir": "/src",
        }
    )
    assert parsed.valid is True
    assert isinstance(parsed.destination, DockerDestination)
    assert parsed.destination.mounts == (
        DockerMount(source="/tmp/src", target="/src", read_only=True),
    )
    assert parsed.destination.env == {"OPENAI_API_KEY": "sk-test"}
    assert parsed.destination.to_dict() == {
        "kind": "docker",
        "image": "ghcr.io/acme/car:latest",
        "container_name": "car-demo",
        "mounts": [{"source": "/tmp/src", "target": "/src", "read_only": True}],
        "env_passthrough": ["CAR_*", "OPENAI_*"],
        "env": {"OPENAI_API_KEY": "sk-test"},
        "workdir": "/src",
    }


def test_parse_destination_config_docker_with_profile() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "profile": "full-dev",
        }
    )
    assert parsed.valid is True
    assert isinstance(parsed.destination, DockerDestination)
    assert parsed.destination.profile == "full-dev"
    assert parsed.destination.to_dict() == {
        "kind": "docker",
        "image": "ghcr.io/acme/car:latest",
        "profile": "full-dev",
    }


def test_parse_destination_config_invalid_docker() -> None:
    parsed = parse_destination_config({"kind": "docker", "image": 42})
    assert parsed.valid is False
    assert isinstance(parsed.destination, LocalDestination)
    assert "requires non-empty 'image'" in parsed.errors[0]


def test_parse_destination_config_invalid_docker_profile() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "profile": "unknown",
        }
    )
    assert parsed.valid is False
    assert isinstance(parsed.destination, LocalDestination)
    assert "unsupported docker profile 'unknown'" in parsed.errors[0]


def test_parse_destination_config_invalid_mount_read_only_type() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "mounts": [{"source": "/tmp/src", "target": "/src", "read_only": "yes"}],
        }
    )
    assert parsed.valid is False
    assert isinstance(parsed.destination, LocalDestination)
    assert "mounts[0].read_only must be a boolean" in parsed.errors[0]


def test_parse_destination_config_supports_readonly_alias() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "mounts": [{"source": "/tmp/src", "target": "/src", "readonly": True}],
        }
    )
    assert parsed.valid is True
    assert isinstance(parsed.destination, DockerDestination)
    assert parsed.destination.mounts == (
        DockerMount(source="/tmp/src", target="/src", read_only=True),
    )


def test_parse_destination_config_invalid_explicit_env_map() -> None:
    parsed = parse_destination_config(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "env": {"OPENAI_API_KEY": 123},
        }
    )
    assert parsed.valid is False
    assert isinstance(parsed.destination, LocalDestination)
    assert "env['OPENAI_API_KEY'] must be a string value" in parsed.errors[0]


def test_validate_destination_write_payload_returns_canonical_docker_dict() -> None:
    result = validate_destination_write_payload(
        {
            "kind": "docker",
            "image": "ghcr.io/acme/car:latest",
            "mounts": [{"source": "/tmp/src", "target": "/src", "readOnly": True}],
            "profile": "FULL-DEV",
        },
        context="destination",
    )
    assert result.valid is True
    assert result.normalized_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/car:latest",
        "mounts": [{"source": "/tmp/src", "target": "/src", "read_only": True}],
        "profile": "full-dev",
    }


def test_validate_destination_write_payload_surfaces_parser_errors() -> None:
    result = validate_destination_write_payload(
        {"kind": "docker", "image": "", "profile": "bad"},
        context="destination",
    )
    assert result.valid is False
    assert result.normalized_destination is None
    assert "docker destination requires non-empty 'image'" in result.errors[0]
    assert "unsupported docker profile 'bad'" in result.errors[1]


def test_resolve_effective_repo_destination_defaults_to_local() -> None:
    repo = ManifestRepo(id="base", path=Path("workspace/base"), kind="base")
    resolution = resolve_effective_repo_destination(repo, {"base": repo})
    assert resolution.source == "default"
    assert resolution.to_dict() == {"kind": "local"}
    assert resolution.issues == ()


def test_resolve_effective_repo_destination_inherits_for_worktree() -> None:
    base = ManifestRepo(
        id="base",
        path=Path("workspace/base"),
        kind="base",
        destination={"kind": "docker", "image": "ghcr.io/acme/base:latest"},
    )
    wt = ManifestRepo(
        id="base--feat",
        path=Path("worktrees/base--feat"),
        kind="worktree",
        worktree_of="base",
    )
    resolution = resolve_effective_repo_destination(wt, {"base": base, wt.id: wt})
    assert resolution.source == "base"
    assert resolution.to_dict() == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }


def test_resolve_effective_repo_destination_reports_invalid_own_destination() -> None:
    base = ManifestRepo(
        id="base",
        path=Path("workspace/base"),
        kind="base",
        destination={"kind": "docker", "image": "ghcr.io/acme/base:latest"},
    )
    wt = ManifestRepo(
        id="base--feat",
        path=Path("worktrees/base--feat"),
        kind="worktree",
        worktree_of="base",
        destination={"kind": "docker", "image": ""},
    )
    resolution = resolve_effective_repo_destination(wt, {"base": base, wt.id: wt})
    assert resolution.source == "base"
    assert resolution.to_dict() == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
    assert any("requires non-empty 'image'" in message for message in resolution.issues)


def test_resolve_effective_agent_workspace_destination_defaults_to_local() -> None:
    workspace = ManifestAgentWorkspace(
        id="zc-main",
        runtime="zeroclaw",
        path=Path(".codex-autorunner/runtimes/zeroclaw/zc-main"),
    )
    resolution = resolve_effective_agent_workspace_destination(workspace)
    assert resolution.source == "default"
    assert resolution.to_dict() == {"kind": "local"}
    assert resolution.issues == ()


def test_resolve_effective_agent_workspace_destination_reports_invalid_config() -> None:
    workspace = ManifestAgentWorkspace(
        id="zc-main",
        runtime="zeroclaw",
        path=Path(".codex-autorunner/runtimes/zeroclaw/zc-main"),
        destination={"kind": "docker", "image": ""},
    )
    resolution = resolve_effective_agent_workspace_destination(workspace)
    assert resolution.source == "default"
    assert resolution.to_dict() == {"kind": "local"}
    assert any("requires non-empty 'image'" in message for message in resolution.issues)
