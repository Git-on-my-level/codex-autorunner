from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support.web_test_helpers import create_test_hub_supervisor
from tests.surfaces.web._hub_test_support import init_git_repo

from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app

pytestmark = [
    pytest.mark.docker_managed_cleanup,
    pytest.mark.slow,
]


def test_hub_destination_routes_show_set_and_persist(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")

    app = create_hub_app(hub_root)
    client = TestClient(app)

    initial = client.get("/hub/repos/base/destination")
    assert initial.status_code == 200
    initial_payload = initial.json()
    assert initial_payload["repo_id"] == "base"
    assert initial_payload["configured_destination"] is None
    assert initial_payload["effective_destination"] == {"kind": "local"}
    assert initial_payload["source"] == "default"

    set_docker = client.post(
        "/hub/repos/base/destination",
        json={"kind": "docker", "image": "busybox:latest"},
    )
    assert set_docker.status_code == 200
    docker_payload = set_docker.json()
    assert docker_payload["configured_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
    }
    assert docker_payload["effective_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
    }
    assert docker_payload["source"] == "repo"

    list_payload = client.get("/hub/repos").json()
    assert list_payload.get("generated_at")
    repos_freshness = list_payload.get("freshness") or {}
    assert repos_freshness.get("generated_at")
    base_entry = next(item for item in list_payload["repos"] if item["id"] == "base")
    assert base_entry["effective_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
    }

    set_local = client.post("/hub/repos/base/destination", json={"kind": "local"})
    assert set_local.status_code == 200
    assert set_local.json()["effective_destination"] == {"kind": "local"}

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    base = manifest.get("base")
    assert base is not None
    assert base.destination == {"kind": "local"}


def test_hub_destination_show_route_includes_manifest_parse_issues(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base = manifest.get("base")
    assert base is not None
    base.destination = {"kind": "docker", "image": ""}
    save_manifest(manifest_path, manifest, hub_root)

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos/base/destination")
    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_destination"] == {"kind": "local"}
    assert any("requires non-empty 'image'" in issue for issue in payload["issues"])


def test_hub_destination_set_route_supports_extended_docker_fields(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")

    app = create_hub_app(hub_root)
    client = TestClient(app)

    set_docker = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "container_name": "car-demo",
            "profile": "full-dev",
            "workdir": "/workspace",
            "env_passthrough": ["CAR_*", "PATH"],
            "env": {"OPENAI_API_KEY": "sk-test", "CODEX_HOME": "/workspace/.codex"},
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
    assert set_docker.status_code == 200
    payload = set_docker.json()
    assert payload["configured_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "profile": "full-dev",
        "workdir": "/workspace",
        "env_passthrough": ["CAR_*", "PATH"],
        "env": {"OPENAI_API_KEY": "sk-test", "CODEX_HOME": "/workspace/.codex"},
        "mounts": [
            {"source": "/tmp/src", "target": "/workspace/src"},
            {
                "source": "/tmp/cache",
                "target": "/workspace/cache",
                "read_only": True,
            },
        ],
    }
    assert payload["effective_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "profile": "full-dev",
        "workdir": "/workspace",
        "env_passthrough": ["CAR_*", "PATH"],
        "env": {"OPENAI_API_KEY": "sk-test", "CODEX_HOME": "/workspace/.codex"},
        "mounts": [
            {"source": "/tmp/src", "target": "/workspace/src"},
            {
                "source": "/tmp/cache",
                "target": "/workspace/cache",
                "read_only": True,
            },
        ],
    }

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    base = manifest.get("base")
    assert base is not None
    assert base.destination == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "profile": "full-dev",
        "workdir": "/workspace",
        "env_passthrough": ["CAR_*", "PATH"],
        "env": {"OPENAI_API_KEY": "sk-test", "CODEX_HOME": "/workspace/.codex"},
        "mounts": [
            {"source": "/tmp/src", "target": "/workspace/src"},
            {
                "source": "/tmp/cache",
                "target": "/workspace/cache",
                "read_only": True,
            },
        ],
    }


def test_hub_destination_set_route_rejects_legacy_env_list_alias(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")

    client = TestClient(create_hub_app(hub_root))
    response = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "env": ["CAR_*", "PATH"],
        },
    )
    assert response.status_code == 422


def test_hub_destination_set_route_rejects_invalid_input(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")
    client = TestClient(create_hub_app(hub_root))

    missing_image = client.post("/hub/repos/base/destination", json={"kind": "docker"})
    assert missing_image.status_code == 400
    assert "requires non-empty 'image'" in missing_image.json()["detail"]

    bad_kind = client.post("/hub/repos/base/destination", json={"kind": "ssh"})
    assert bad_kind.status_code == 400
    assert "Use 'local' or 'docker'" in bad_kind.json()["detail"]

    bad_mount = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "mounts": [{"source": "/tmp/src"}],
        },
    )
    assert bad_mount.status_code == 400
    assert "mounts[0].target must be a non-empty string" in bad_mount.json()["detail"]

    bad_mount_read_only = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "mounts": [
                {"source": "/tmp/src", "target": "/workspace/src", "read_only": "yes"}
            ],
        },
    )
    assert bad_mount_read_only.status_code == 422
    detail = bad_mount_read_only.json()["detail"]
    assert any(item["loc"][-1] == "read_only" for item in detail)

    bad_env = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "env": {"": "value"},
        },
    )
    assert bad_env.status_code == 400
    assert "env keys must be non-empty strings" in bad_env.json()["detail"]

    bad_profile = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "profile": "full_deev",
        },
    )
    assert bad_profile.status_code == 400
    assert "unsupported docker profile 'full_deev'" in bad_profile.json()["detail"]


def test_hub_destination_set_route_rejects_unknown_top_level_keys(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")
    client = TestClient(create_hub_app(hub_root))

    response = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "unexpected": "value",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "unexpected" for item in detail)


def test_hub_destination_set_route_rejects_unknown_mount_keys(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    supervisor.create_repo("base")
    client = TestClient(create_hub_app(hub_root))

    response = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "mounts": [
                {
                    "source": "/tmp/src",
                    "target": "/workspace/src",
                    "mode": "rw",
                }
            ],
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "mode" for item in detail)

    unknown_repo = client.post(
        "/hub/repos/missing-repo/destination",
        json={"kind": "local"},
    )
    assert unknown_repo.status_code == 404
    assert "Repo not found" in unknown_repo.json()["detail"]


def test_hub_destination_web_mutation_preserves_inheritance_and_worktree_override(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-web",
        start_point="HEAD",
    )

    client = TestClient(create_hub_app(hub_root))

    pre_show = client.get(f"/hub/repos/{worktree.id}/destination")
    assert pre_show.status_code == 200
    assert pre_show.json()["effective_destination"] == {"kind": "local"}
    assert pre_show.json()["source"] == "default"

    set_base = client.post(
        "/hub/repos/base/destination",
        json={"kind": "docker", "image": "ghcr.io/acme/base:latest"},
    )
    assert set_base.status_code == 200

    inherited = client.get(f"/hub/repos/{worktree.id}/destination")
    assert inherited.status_code == 200
    inherited_payload = inherited.json()
    assert inherited_payload["configured_destination"] is None
    assert inherited_payload["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
    assert inherited_payload["source"] == "base"

    set_worktree_local = client.post(
        f"/hub/repos/{worktree.id}/destination",
        json={"kind": "local"},
    )
    assert set_worktree_local.status_code == 200
    wt_payload = set_worktree_local.json()
    assert wt_payload["configured_destination"] == {"kind": "local"}
    assert wt_payload["effective_destination"] == {"kind": "local"}
    assert wt_payload["source"] == "repo"

    list_payload = client.get("/hub/repos").json()
    base_row = next(item for item in list_payload["repos"] if item["id"] == "base")
    wt_row = next(item for item in list_payload["repos"] if item["id"] == worktree.id)
    assert base_row["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
    assert wt_row["effective_destination"] == {"kind": "local"}
