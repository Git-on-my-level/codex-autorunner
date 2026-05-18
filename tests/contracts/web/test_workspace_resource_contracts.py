from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.artifact_delivery import ArtifactDeliveryService
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.filebox import ensure_structure, inbox_dir
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.app import create_repo_app


def _seed_snapshot(repo_root: Path, snapshot_id: str = "snap-1") -> None:
    contextspace = (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / "wt-1"
        / snapshot_id
        / "contextspace"
    )
    contextspace.mkdir(parents=True, exist_ok=True)
    (contextspace / "active_context.md").write_text(
        "Archived context", encoding="utf-8"
    )


@dataclass(frozen=True)
class _HubEnv:
    client: TestClient
    repo_id: str
    repo_root: Path


@pytest.fixture()
def repo_client(tmp_path: Path) -> tuple[TestClient, Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_hub_files(tmp_path, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir()
    return TestClient(create_repo_app(repo_root)), repo_root


@pytest.fixture()
def hub_env(tmp_path: Path) -> _HubEnv:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    repo_id = "repo-1"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    return _HubEnv(
        client=TestClient(create_hub_app(hub_root)),
        repo_id=repo_id,
        repo_root=repo_root,
    )


def test_contextspace_tree_keeps_pinned_docs_first(repo_client) -> None:
    client, repo_root = repo_client
    extra = repo_root / ".codex-autorunner" / "contextspace" / "scratch.md"
    extra.write_text("scratch", encoding="utf-8")

    response = client.get("/api/contextspace/tree")

    assert response.status_code == 200
    tree = response.json()["tree"]
    assert [node["path"] for node in tree[:3]] == [
        "active_context.md",
        "decisions.md",
        "spec.md",
    ]
    assert [node["is_pinned"] for node in tree[:3]] == [True, True, True]
    assert tree[3]["path"] == "scratch.md"


def test_archive_file_and_download_share_resolution_contract(repo_client) -> None:
    client, repo_root = repo_client
    _seed_snapshot(repo_root)
    params = {"snapshot_id": "snap-1", "path": "contextspace/active_context.md"}

    read_response = client.get("/api/archive/file", params=params)
    download_response = client.get("/api/archive/download", params=params)
    bad_read = client.get(
        "/api/archive/file",
        params={"snapshot_id": "snap-1", "path": "../active_context.md"},
    )
    bad_download = client.get(
        "/api/archive/download",
        params={"snapshot_id": "snap-1", "path": "../active_context.md"},
    )

    assert read_response.status_code == 200
    assert download_response.status_code == 200
    assert read_response.text.encode() == download_response.content
    assert bad_read.status_code == bad_download.status_code == 400


def test_filebox_local_and_hub_listing_share_serialized_fields(
    hub_env: _HubEnv,
) -> None:
    ensure_structure(hub_env.repo_root)
    (inbox_dir(hub_env.repo_root) / "note.txt").write_text("hello", encoding="utf-8")

    repo_response = hub_env.client.get(f"/repos/{hub_env.repo_id}/api/filebox")
    hub_response = hub_env.client.get(f"/hub/filebox/{hub_env.repo_id}")

    assert repo_response.status_code == 200
    assert hub_response.status_code == 200
    repo_entry = repo_response.json()["inbox"][0]
    hub_entry = hub_response.json()["inbox"][0]
    assert {key: repo_entry[key] for key in ("name", "box", "size", "source")} == {
        key: hub_entry[key] for key in ("name", "box", "size", "source")
    }
    assert repo_entry["url"] == f"/repos/{hub_env.repo_id}/api/filebox/inbox/note.txt"
    assert hub_entry["url"] == f"/hub/filebox/{hub_env.repo_id}/inbox/note.txt"
    assert hub_entry["repo_id"] == hub_env.repo_id


def test_artifact_delivery_filters_are_stable_for_repo_and_hub(
    hub_env: _HubEnv,
) -> None:
    artifact = hub_env.repo_root / "report.txt"
    artifact.write_text("artifact", encoding="utf-8")
    service = ArtifactDeliveryService(hub_env.repo_root)
    first = service.enqueue_file(
        artifact,
        target_surface="discord",
        target_conversation_key="channel-1",
    )
    claimed = service.claim_next(
        target_surface="discord", target_conversation_key="channel-1"
    )
    assert claimed is not None
    service.mark_sending(first.delivery_id, claim_token=claimed.claim_token)
    service.mark_sent(first.delivery_id, claim_token=claimed.claim_token)
    service.enqueue_file(
        artifact,
        target_surface="telegram",
        target_conversation_key="topic-1",
    )

    repo_response = hub_env.client.get(
        f"/repos/{hub_env.repo_id}/api/artifacts/deliveries",
        params={"state": "sent", "surface": "discord", "conversation": "channel-1"},
    )
    hub_response = hub_env.client.get(
        f"/hub/filebox/{hub_env.repo_id}/artifacts/deliveries",
        params={"state": "pending", "surface": "telegram", "conversation": "topic-1"},
    )

    assert repo_response.status_code == 200
    assert hub_response.status_code == 200
    repo_payload = repo_response.json()
    hub_payload = hub_response.json()
    assert [item["state"] for item in repo_payload["deliveries"]] == ["sent"]
    assert [item["target_surface"] for item in repo_payload["deliveries"]] == [
        "discord"
    ]
    assert [item["state"] for item in hub_payload["deliveries"]] == ["pending"]
    assert [item["target_conversation_key"] for item in hub_payload["deliveries"]] == [
        "topic-1"
    ]
    assert hub_payload["repo_id"] == hub_env.repo_id
