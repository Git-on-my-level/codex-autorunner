from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    ConfigError,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config

runner = CliRunner()


def _seed_single_repo_manifest(hub_root: Path, repo_id: str = "base") -> None:
    seed_hub_files(hub_root, force=True)
    repo_path = hub_root / "repos" / repo_id
    repo_path.mkdir(parents=True, exist_ok=True)
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_path, repo_id=repo_id, kind="base")
    save_manifest(manifest_path, manifest, hub_root)


@pytest.mark.parametrize(
    ("shared_section", "invalid_value", "error_match"),
    [
        ("update", {"backend": 123}, "update.backend must be a string"),
        ("usage", {"cache_scope": 7}, "usage.cache_scope must be a string if provided"),
    ],
)
@pytest.mark.xfail(
    reason="Issue #830: hub validation is currently weaker than repo validation",
    strict=True,
)
def test_hub_and_repo_shared_section_validation_parity(
    tmp_path: Path,
    shared_section: str,
    invalid_value: dict[str, object],
    error_match: str,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            shared_section: invalid_value,
            "repo_defaults": {shared_section: invalid_value},
        },
    )
    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match=error_match):
        load_repo_config(repo_root, hub_path=hub_root)
    with pytest.raises(ConfigError, match=error_match):
        load_hub_config(hub_root)


def test_cli_and_web_destination_set_reject_invalid_profile_with_same_strictness(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_single_repo_manifest(hub_root, repo_id="base")

    cli_result = runner.invoke(
        app,
        [
            "hub",
            "destination",
            "set",
            "base",
            "docker",
            "--image",
            "busybox:latest",
            "--profile",
            "full_deev",
            "--path",
            str(hub_root),
        ],
    )
    client = TestClient(create_hub_app(hub_root))
    web_result = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "profile": "full_deev",
        },
    )
    assert web_result.status_code == 400
    assert "unsupported docker profile 'full_deev'" in web_result.json()["detail"]
    assert cli_result.exit_code != 0
    assert "unsupported docker profile 'full_deev'" in cli_result.output


def test_cli_and_web_destination_set_persist_same_canonical_payload(
    tmp_path: Path,
) -> None:
    cli_hub_root = tmp_path / "cli-hub"
    web_hub_root = tmp_path / "web-hub"
    _seed_single_repo_manifest(cli_hub_root, repo_id="base")
    _seed_single_repo_manifest(web_hub_root, repo_id="base")

    cli_result = runner.invoke(
        app,
        [
            "hub",
            "destination",
            "set",
            "base",
            "docker",
            "--image",
            "busybox:latest",
            "--name",
            "car-demo",
            "--profile",
            "full-dev",
            "--workdir",
            "/workspace",
            "--env",
            "CAR_*",
            "--env-map",
            "OPENAI_API_KEY=sk-test",
            "--mount",
            "/tmp/src:/workspace/src",
            "--mount-ro",
            "/tmp/cache:/workspace/cache",
            "--json",
            "--path",
            str(cli_hub_root),
        ],
    )
    assert cli_result.exit_code == 0
    cli_payload = json.loads(cli_result.output)

    web_client = TestClient(create_hub_app(web_hub_root))
    web_result = web_client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "container_name": "car-demo",
            "profile": "full-dev",
            "workdir": "/workspace",
            "env_passthrough": ["CAR_*"],
            "env": {"OPENAI_API_KEY": "sk-test"},
            "mounts": [
                {"source": "/tmp/src", "target": "/workspace/src"},
                {
                    "source": "/tmp/cache",
                    "target": "/workspace/cache",
                    "readOnly": True,
                },
            ],
        },
    )
    assert web_result.status_code == 200
    web_payload = web_result.json()

    assert (
        cli_payload["configured_destination"] == web_payload["configured_destination"]
    )
    assert cli_payload["effective_destination"] == web_payload["effective_destination"]
