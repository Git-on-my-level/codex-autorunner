import json
from pathlib import Path

from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.manifest import load_manifest
from codex_autorunner.server import create_hub_app


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(["add", "README.md"], path, check=True)
    run_git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        path,
        check=True,
    )


def _create_hub_supervisor(hub_root: Path) -> HubSupervisor:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    return HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )


def _assert_repo_canonical_state_v1(repo_entry: dict) -> None:
    canonical = repo_entry.get("canonical_state_v1") or {}
    assert canonical.get("schema_version") == 1
    assert canonical.get("repo_id") == repo_entry["id"]
    assert Path(str(canonical.get("repo_root") or "")).name == repo_entry["id"]
    assert canonical.get("ingest_source") == "ticket_files"
    assert isinstance(canonical.get("recommended_actions"), list)
    assert canonical.get("recommendation_confidence") in {"high", "medium", "low"}
    assert canonical.get("observed_at")
    assert canonical.get("recommendation_generated_at")


def test_hub_destination_routes_show_set_and_persist(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
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
    base_entry = next(item for item in list_payload["repos"] if item["id"] == "base")
    assert base_entry["effective_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
    }
    _assert_repo_canonical_state_v1(base_entry)

    set_local = client.post("/hub/repos/base/destination", json={"kind": "local"})
    assert set_local.status_code == 200
    assert set_local.json()["effective_destination"] == {"kind": "local"}

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    base = manifest.get("base")
    assert base is not None
    assert base.destination == {"kind": "local"}


def test_hub_destination_set_route_supports_extended_docker_fields(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    supervisor.create_repo("base")

    app = create_hub_app(hub_root)
    client = TestClient(app)

    set_docker = client.post(
        "/hub/repos/base/destination",
        json={
            "kind": "docker",
            "image": "busybox:latest",
            "container_name": "car-demo",
            "env_passthrough": ["CAR_*", "PATH"],
            "mounts": [{"source": "/tmp/src", "target": "/workspace/src"}],
        },
    )
    assert set_docker.status_code == 200
    payload = set_docker.json()
    assert payload["configured_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "env_passthrough": ["CAR_*", "PATH"],
        "mounts": [{"source": "/tmp/src", "target": "/workspace/src"}],
    }
    assert payload["effective_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "env_passthrough": ["CAR_*", "PATH"],
        "mounts": [{"source": "/tmp/src", "target": "/workspace/src"}],
    }

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    base = manifest.get("base")
    assert base is not None
    assert base.destination == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "env_passthrough": ["CAR_*", "PATH"],
        "mounts": [{"source": "/tmp/src", "target": "/workspace/src"}],
    }


def test_hub_destination_set_route_rejects_invalid_input(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    supervisor.create_repo("base")
    client = TestClient(create_hub_app(hub_root))

    missing_image = client.post("/hub/repos/base/destination", json={"kind": "docker"})
    assert missing_image.status_code == 400
    assert "image is required for docker destination" in missing_image.json()["detail"]

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
    assert (
        "Each mount requires non-empty source and target" in bad_mount.json()["detail"]
    )

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
    supervisor = _create_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
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


def test_hub_channel_directory_route_lists_and_filters(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _create_hub_supervisor(hub_root)
    store = ChannelDirectoryStore(hub_root)
    store.record_seen(
        "discord",
        "chan-123",
        "guild-1",
        "CAR HQ / #ops",
        {"guild_id": "guild-1"},
    )
    store.record_seen(
        "telegram",
        "-1001",
        "77",
        "Team Room / Build",
        {"chat_type": "supergroup"},
    )

    client = TestClient(create_hub_app(hub_root))

    listed = client.get("/hub/chat/channels")
    assert listed.status_code == 200
    rows = listed.json()["entries"]
    keys = {row["key"] for row in rows}
    assert "discord:chan-123:guild-1" in keys
    assert "telegram:-1001:77" in keys

    filtered = client.get("/hub/chat/channels", params={"query": "hq", "limit": 10})
    assert filtered.status_code == 200
    filtered_rows = filtered.json()["entries"]
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["key"] == "discord:chan-123:guild-1"

    limited = client.get("/hub/chat/channels", params={"limit": 1})
    assert limited.status_code == 200
    assert len(limited.json()["entries"]) == 1

    bad_limit = client.get("/hub/chat/channels", params={"limit": 0})
    assert bad_limit.status_code == 400
    assert "limit must be greater than 0" in bad_limit.json()["detail"]

    bad_limit_high = client.get("/hub/chat/channels", params={"limit": 1001})
    assert bad_limit_high.status_code == 400
    assert "limit must be <= 1000" in bad_limit_high.json()["detail"]


def test_hub_ui_exposes_destination_and_channel_directory_controls() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    index_html = (
        repo_root / "src" / "codex_autorunner" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="hub-channel-query"' in index_html
    assert 'id="hub-channel-search"' in index_html
    assert 'id="hub-channel-refresh"' in index_html
    assert 'id="hub-channel-list"' in index_html
    assert "Copy Ref copies a channel ref" in index_html

    hub_source = (
        repo_root / "src" / "codex_autorunner" / "static_src" / "hub.ts"
    ).read_text(encoding="utf-8")
    assert "set_destination" in hub_source
    assert "/hub/repos/${encodeURIComponent(repo.id)}/destination" in hub_source
    assert "/hub/chat/channels" in hub_source
    assert "container_name" in hub_source
    assert "env_passthrough" in hub_source
    assert "mounts" in hub_source
    assert "copy_channel_key" in hub_source
    assert "Copied channel ref" in hub_source
