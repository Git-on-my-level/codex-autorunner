from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app, create_repo_app
from codex_autorunner.surfaces.web.routes import flows as flow_routes

RUNNER = CliRunner()

# Characterization matrix for issue #828.
# This intentionally captures today's cross-surface behavior, including drift.
ISSUE_828_PARITY_MATRIX = {
    "filebox": {
        "invalid_box": {
            "api_status": 400,
            "hub_status": 400,
            "parity": "aligned",
        },
        "invalid_upload_filename": {
            "api_status": 400,
            "hub_status": 400,
            "parity": "aligned",
        },
    },
    "flow_start": {
        "no_tickets_default_start": {
            "web_status": 200,
            "cli_exit_code": 1,
            "parity": "drift",
        }
    },
    "destination": {
        "invalid_docker_profile": {
            "web_status": 400,
            "cli_exit_code": 0,
            "cli_effective_destination": {"kind": "local"},
            "cli_source": "default",
            "parity": "drift",
        }
    },
}


def _seed_hub_with_registered_repo(
    tmp_path: Path, *, repo_id: str = "repo"
) -> tuple[Path, Path]:
    hub_root = (tmp_path / "hub").resolve()
    hub_root.mkdir(parents=True, exist_ok=True)
    seed_hub_files(hub_root, force=True)

    repo_root = (hub_root / "worktrees" / repo_id).resolve()
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    return hub_root, repo_root


def _seed_standalone_repo(tmp_path: Path) -> Path:
    repo_root = (tmp_path / "repo").resolve()
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    return repo_root


def test_issue_828_filebox_parity_matrix_characterization(tmp_path: Path) -> None:
    hub_root, _repo_root = _seed_hub_with_registered_repo(tmp_path, repo_id="repo")
    matrix = ISSUE_828_PARITY_MATRIX["filebox"]

    with TestClient(create_hub_app(hub_root), raise_server_exceptions=False) as client:
        invalid_box_api = client.get("/repos/repo/api/filebox/not-a-box")
        invalid_box_hub = client.get("/hub/filebox/repo/not-a-box/missing.txt")

        assert invalid_box_api.status_code == matrix["invalid_box"]["api_status"]
        assert invalid_box_hub.status_code == matrix["invalid_box"]["hub_status"]
        assert invalid_box_api.json()["detail"] == "Invalid box"
        assert invalid_box_hub.json()["detail"] == "Invalid box"
        assert matrix["invalid_box"]["parity"] == "aligned"

        files = {"../bad.txt": ("safe.txt", b"x", "text/plain")}
        bad_upload_api = client.post("/repos/repo/api/filebox/inbox", files=files)
        bad_upload_hub = client.post("/hub/filebox/repo/inbox", files=files)

        assert (
            bad_upload_api.status_code
            == matrix["invalid_upload_filename"]["api_status"]
        )
        assert (
            bad_upload_hub.status_code
            == matrix["invalid_upload_filename"]["hub_status"]
        )
        assert bad_upload_api.json()["detail"] == "Invalid filename"
        assert bad_upload_hub.json()["detail"] == "Invalid filename"
        assert matrix["invalid_upload_filename"]["parity"] == "aligned"


def test_issue_828_flow_start_parity_matrix_characterization(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = _seed_standalone_repo(tmp_path / "standalone")
    external_hub_root = (tmp_path / "external-hub").resolve()
    external_hub_root.mkdir(parents=True, exist_ok=True)
    seed_hub_files(external_hub_root, force=True)

    matrix = ISSUE_828_PARITY_MATRIX["flow_start"]["no_tickets_default_start"]
    # Keep test deterministic and avoid starting subprocess workers.
    monkeypatch.setattr(
        flow_routes, "_start_flow_worker", lambda *_args, **_kwargs: None
    )

    with TestClient(
        create_repo_app(repo_root), raise_server_exceptions=False
    ) as client:
        web_start = client.post("/api/flows/ticket_flow/start", json={})
    assert web_start.status_code == matrix["web_status"]
    assert isinstance(web_start.json().get("id"), str)

    cli_start = RUNNER.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "start",
            "--repo",
            str(repo_root),
            "--hub",
            str(external_hub_root),
        ],
    )
    assert cli_start.exit_code == matrix["cli_exit_code"]
    assert "No tickets found." in cli_start.output
    assert "Fix the above errors before starting the ticket flow." in cli_start.output
    assert matrix["parity"] == "drift"


def test_issue_828_destination_parity_matrix_characterization(tmp_path: Path) -> None:
    hub_root, _repo_root = _seed_hub_with_registered_repo(tmp_path, repo_id="base")
    matrix = ISSUE_828_PARITY_MATRIX["destination"]["invalid_docker_profile"]

    with TestClient(create_hub_app(hub_root), raise_server_exceptions=False) as client:
        web_response = client.post(
            "/hub/repos/base/destination",
            json={
                "kind": "docker",
                "image": "busybox:latest",
                "profile": "full_deev",
            },
        )
    assert web_response.status_code == matrix["web_status"]
    assert "unsupported docker profile 'full_deev'" in web_response.json()["detail"]

    cli_response = RUNNER.invoke(
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
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert cli_response.exit_code == matrix["cli_exit_code"]
    payload = json.loads(cli_response.output)
    assert payload["effective_destination"] == matrix["cli_effective_destination"]
    assert payload["source"] == matrix["cli_source"]
    assert payload["configured_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "profile": "full_deev",
    }
    assert any(
        "unsupported docker profile 'full_deev'" in issue
        for issue in payload.get("issues", [])
    )
    assert matrix["parity"] == "drift"
