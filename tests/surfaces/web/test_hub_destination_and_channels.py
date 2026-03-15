import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.app_server_threads import (
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
)
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.flows import FlowEventType, FlowRunStatus, FlowStore
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.state import RunnerState, save_state
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.telegram.state import topic_key as telegram_topic_key
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app

pytestmark = pytest.mark.docker_managed_cleanup


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
    return _create_hub_supervisor_with_config(hub_root, cfg)


def _create_hub_supervisor_with_config(hub_root: Path, cfg: dict) -> HubSupervisor:
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    return HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )


def _write_discord_binding_rows(db_path: Path, rows: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    resource_kind TEXT,
                    resource_id TEXT,
                    pma_enabled INTEGER,
                    agent TEXT,
                    updated_at TEXT
                )
                """
            )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO channel_bindings (
                        channel_id,
                        guild_id,
                        workspace_path,
                        repo_id,
                        resource_kind,
                        resource_id,
                        pma_enabled,
                        agent,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        guild_id=excluded.guild_id,
                        workspace_path=excluded.workspace_path,
                        repo_id=excluded.repo_id,
                        resource_kind=excluded.resource_kind,
                        resource_id=excluded.resource_id,
                        pma_enabled=excluded.pma_enabled,
                        agent=excluded.agent,
                        updated_at=excluded.updated_at
                    """,
                    (
                        row.get("channel_id"),
                        row.get("guild_id"),
                        row.get("workspace_path"),
                        row.get("repo_id"),
                        row.get("resource_kind"),
                        row.get("resource_id"),
                        row.get("pma_enabled"),
                        row.get("agent"),
                        row.get("updated_at"),
                    ),
                )
    finally:
        conn.close()


def _write_telegram_topic_rows(
    db_path: Path,
    *,
    topics: list[dict],
    scopes: list[dict],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    active_thread_id TEXT,
                    payload_json TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            for row in topics:
                payload_json = row.get("payload_json")
                payload_text = (
                    json.dumps(payload_json) if isinstance(payload_json, dict) else "{}"
                )
                conn.execute(
                    """
                    INSERT INTO telegram_topics (
                        topic_key,
                        chat_id,
                        thread_id,
                        scope,
                        workspace_path,
                        repo_id,
                        active_thread_id,
                        payload_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_key) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        thread_id=excluded.thread_id,
                        scope=excluded.scope,
                        workspace_path=excluded.workspace_path,
                        repo_id=excluded.repo_id,
                        active_thread_id=excluded.active_thread_id,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        row.get("topic_key"),
                        row.get("chat_id"),
                        row.get("thread_id"),
                        row.get("scope"),
                        row.get("workspace_path"),
                        row.get("repo_id"),
                        row.get("active_thread_id"),
                        payload_text,
                        row.get("updated_at"),
                    ),
                )
            for scope in scopes:
                conn.execute(
                    """
                    INSERT INTO telegram_topic_scopes (
                        chat_id,
                        thread_id,
                        scope,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                        scope=excluded.scope,
                        updated_at=excluded.updated_at
                    """,
                    (
                        scope.get("chat_id"),
                        scope.get("thread_id"),
                        scope.get("scope"),
                        scope.get("updated_at"),
                    ),
                )
    finally:
        conn.close()


def _write_app_server_threads(path: Path, threads: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "threads": threads}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_usage_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _seed_flow_run(
    repo_root: Path,
    *,
    run_id: str,
    status: FlowRunStatus,
    diff_events: list[dict],
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={})
        store.update_flow_run_status(
            run_id,
            status,
            state={},
            started_at=started_at,
            finished_at=finished_at,
        )
        for index, payload in enumerate(diff_events, start=1):
            store.create_event(
                event_id=f"{run_id}-diff-{index}",
                run_id=run_id,
                event_type=FlowEventType.DIFF_UPDATED,
                data=payload,
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
    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at")
    assert freshness.get("recency_basis")
    assert freshness.get("basis_at")


def test_hub_repo_list_includes_ticket_flow_summary_and_run_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )
    (tickets_dir / "TICKET-002.md").write_text(
        "---\ntitle: Second\ngoal: verify it\nagent: codex\ndone: false\n---\n\nbody\n",
        encoding="utf-8",
    )
    _seed_flow_run(
        repo.path,
        run_id="run-paused",
        status=FlowRunStatus.PAUSED,
        diff_events=[],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    summary = repo_entry["ticket_flow"]
    assert summary["status"] == "paused"
    assert summary["done_count"] == 1
    assert summary["total_count"] == 2
    assert summary["run_id"] == "run-paused"

    display = repo_entry["ticket_flow_display"]
    assert display["status"] == "paused"
    assert display["done_count"] == 1
    assert display["total_count"] == 2
    assert display["run_id"] == "run-paused"
    assert display["is_active"] is True

    run_state = repo_entry["run_state"] or {}
    assert run_state["run_id"] == "run-paused"
    assert run_state["flow_status"] == "paused"
    assert run_state["recommended_action"]
    _assert_repo_canonical_state_v1(repo_entry)
    assert repo_entry["canonical_state_v1"]["represented_run_id"] == "run-paused"


def test_hub_scan_reuses_repo_summary_enrichment(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: false\n---\n\nbody\n",
        encoding="utf-8",
    )
    _seed_flow_run(
        repo.path,
        run_id="run-running",
        status=FlowRunStatus.RUNNING,
        diff_events=[],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.post("/hub/repos/scan")
    assert response.status_code == 200

    payload = response.json()
    assert payload["last_scan_at"]
    assert isinstance(payload["pinned_parent_repo_ids"], list)

    repo_entry = next(item for item in payload["repos"] if item["id"] == "base")
    summary = repo_entry["ticket_flow"]
    assert summary["status"] == "running"
    assert summary["done_count"] == 0
    assert summary["total_count"] == 1
    assert summary["run_id"] == "run-running"

    display = repo_entry["ticket_flow_display"]
    assert display["status"] == "running"
    assert display["is_active"] is True

    run_state = repo_entry["run_state"] or {}
    assert run_state["run_id"] == "run-running"
    assert run_state["flow_status"] == "running"
    _assert_repo_canonical_state_v1(repo_entry)
    assert repo_entry["canonical_state_v1"]["represented_run_id"] == "run-running"


def test_hub_repo_list_rewrites_stale_runner_last_run_to_authoritative_flow_run(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )

    _seed_flow_run(
        repo.path,
        run_id="older-failed",
        status=FlowRunStatus.FAILED,
        diff_events=[],
    )
    _seed_flow_run(
        repo.path,
        run_id="newer-completed",
        status=FlowRunStatus.COMPLETED,
        diff_events=[],
    )
    save_state(
        repo.path / ".codex-autorunner" / "state.sqlite3",
        RunnerState(
            last_run_id="older-failed",
            status="running",
            last_exit_code=137,
            last_run_started_at="2026-03-10T00:00:00+00:00",
            last_run_finished_at=None,
        ),
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    assert repo_entry["last_run_id"] == "newer-completed"
    assert repo_entry["last_exit_code"] is None
    assert repo_entry["ticket_flow"]["run_id"] == "newer-completed"
    assert repo_entry["canonical_state_v1"]["latest_run_id"] == "newer-completed"


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
    assert list_payload.get("generated_at")
    repos_freshness = list_payload.get("freshness") or {}
    assert repos_freshness.get("generated_at")
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


def test_hub_destination_show_route_includes_manifest_parse_issues(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
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


def test_hub_destination_set_route_accepts_legacy_env_list_alias(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
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
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured_destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "env_passthrough": ["CAR_*", "PATH"],
    }


def test_hub_destination_set_route_rejects_invalid_input(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
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
    assert bad_mount_read_only.status_code == 400
    assert (
        "mounts[0].read_only must be a boolean" in bad_mount_read_only.json()["detail"]
    )

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


def test_hub_channel_directory_route_enriches_entries_best_effort(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["telegram_bot"]["require_topics"] = True
    supervisor = _create_hub_supervisor_with_config(hub_root, cfg)
    repo_work = supervisor.create_repo("work")
    repo_final = supervisor.create_repo("final")
    _init_git_repo(repo_work.path)
    _init_git_repo(repo_final.path)
    (repo_work.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-work", None, "Work / #build", {})
    store.record_seen("discord", "chan-pma", None, "PMA / #ops", {})
    store.record_seen("telegram", "-200", "9", "PMA Topic", {})
    store.record_seen("telegram", "-300", "11", "Final Topic", {})
    store.record_seen("telegram", "-400", "12", "Clean Topic", {})

    _write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-work",
                "guild_id": None,
                "workspace_path": str(repo_work.path),
                "repo_id": None,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            },
            {
                "channel_id": "chan-pma",
                "guild_id": None,
                "workspace_path": str(repo_work.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "opencode",
                "updated_at": "2026-01-01T00:00:02Z",
            },
        ],
    )

    scoped_key = telegram_topic_key(-200, 9, scope="dev")
    stale_key = telegram_topic_key(-200, 9, scope="old")
    _write_telegram_topic_rows(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topics=[
            {
                "topic_key": scoped_key,
                "chat_id": -200,
                "thread_id": 9,
                "scope": "dev",
                "workspace_path": str(repo_work.path),
                "repo_id": None,
                "active_thread_id": "tg-pma-old",
                "payload_json": {"pma_enabled": True, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:03Z",
            },
            {
                "topic_key": stale_key,
                "chat_id": -200,
                "thread_id": 9,
                "scope": "old",
                "workspace_path": str(repo_final.path),
                "repo_id": "final",
                "active_thread_id": "tg-stale",
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:02Z",
            },
            {
                "topic_key": telegram_topic_key(-300, 11),
                "chat_id": -300,
                "thread_id": 11,
                "scope": None,
                "workspace_path": str(repo_final.path),
                "repo_id": None,
                "active_thread_id": "tg-direct-thread",
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:04Z",
            },
            {
                "topic_key": telegram_topic_key(-400, 12),
                "chat_id": -400,
                "thread_id": 12,
                "scope": None,
                "workspace_path": str(repo_final.path),
                "repo_id": "final",
                "active_thread_id": None,
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:05Z",
            },
        ],
        scopes=[
            {
                "chat_id": -200,
                "thread_id": 9,
                "scope": "dev",
                "updated_at": "2026-01-01T00:00:03Z",
            }
        ],
    )

    digest = hashlib.sha256(str(repo_work.path).encode("utf-8")).hexdigest()[:12]
    _write_app_server_threads(
        repo_work.path / ".codex-autorunner" / "app_server_threads.json",
        threads={
            f"{FILE_CHAT_PREFIX}discord.chan-work.{digest}": "discord-working-thread",
            PMA_OPENCODE_KEY: "discord-pma-thread",
            PMA_KEY: "wrong-global-pma-thread",
            f"{PMA_KEY}.{telegram_topic_key(-200, 9)}": "telegram-pma-thread",
        },
    )

    pma_store = PmaThreadStore(hub_root)
    discord_pma_thread = pma_store.create_thread(
        "opencode",
        repo_work.path,
        repo_id="work",
    )
    telegram_pma_thread = pma_store.create_thread(
        "codex",
        repo_work.path,
        repo_id="work",
    )

    _write_usage_rows(
        repo_work.path / ".codex-autorunner" / "usage" / "opencode_turn_usage.jsonl",
        rows=[
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "session_id": "discord-working-thread",
                "turn_id": "turn-old",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 1,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 4,
                },
            },
            {
                "timestamp": "2026-01-01T00:00:10Z",
                "session_id": "discord-working-thread",
                "turn_id": "turn-new",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 40,
                    "total_tokens": 100,
                },
            },
        ],
    )

    _seed_flow_run(
        repo_work.path,
        run_id="run-work",
        status=FlowRunStatus.RUNNING,
        diff_events=[
            {"insertions": 2, "deletions": 1, "files_changed": 1},
            {"insertions": 3, "deletions": 2, "files_changed": 2},
        ],
    )
    _seed_flow_run(
        repo_final.path,
        run_id="run-final",
        status=FlowRunStatus.COMPLETED,
        diff_events=[{"insertions": 1, "deletions": 0, "files_changed": 1}],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    work = rows["discord:chan-work"]
    assert work["repo_id"] == "work"
    assert work["workspace_path"] == str(repo_work.path)
    assert work["active_thread_id"] == "discord-working-thread"
    assert work["source"] == "discord"
    assert work["provenance"]["source"] == "discord"
    assert work["channel_status"] == "working"
    assert work["status_label"] == "working"
    assert work["diff_stats"] == {"insertions": 5, "deletions": 3, "files_changed": 3}
    assert work["dirty"] is True
    assert work["token_usage"] == {
        "total_tokens": 100,
        "input_tokens": 10,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "reasoning_output_tokens": 40,
        "turn_id": "turn-new",
        "timestamp": "2026-01-01T00:00:10Z",
    }
    assert (
        "display" in work and "seen_at" in work and "meta" in work and "entry" in work
    )

    discord_pma = rows["discord:chan-pma"]
    assert discord_pma["active_thread_id"] == "discord-pma-thread"
    assert discord_pma["source"] == "pma_thread"
    assert discord_pma["provenance"]["source"] == "pma_thread"
    assert discord_pma["provenance"]["agent"] == "opencode"
    assert (
        discord_pma["provenance"]["managed_thread_id"]
        == discord_pma_thread["managed_thread_id"]
    )
    assert (
        discord_pma["provenance"]["managed_thread_id"]
        != discord_pma["active_thread_id"]
    )

    telegram_pma = rows["telegram:-200:9"]
    assert telegram_pma["active_thread_id"] == "telegram-pma-thread"
    assert telegram_pma["source"] == "pma_thread"
    assert telegram_pma["provenance"]["source"] == "pma_thread"
    assert telegram_pma["provenance"]["agent"] == "codex"
    assert (
        telegram_pma["provenance"]["managed_thread_id"]
        == telegram_pma_thread["managed_thread_id"]
    )
    assert (
        telegram_pma["provenance"]["managed_thread_id"]
        != telegram_pma["active_thread_id"]
    )

    telegram_final = rows["telegram:-300:11"]
    assert telegram_final["active_thread_id"] == "tg-direct-thread"
    assert telegram_final["source"] == "telegram"
    assert telegram_final["provenance"]["source"] == "telegram"
    assert telegram_final["channel_status"] == "final"
    assert telegram_final["status_label"] == "final"
    assert telegram_final["dirty"] is False

    telegram_clean = rows["telegram:-400:12"]
    assert telegram_clean["source"] == "telegram"
    assert telegram_clean["provenance"]["source"] == "telegram"
    assert telegram_clean["channel_status"] == "clean"
    assert telegram_clean["status_label"] == "clean"


def test_hub_channel_directory_route_surfaces_agent_workspace_binding_metadata(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    workspace = supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-zc", None, "ZeroClaw / #main", {})

    _write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-zc",
                "guild_id": None,
                "workspace_path": str(workspace.path.resolve()),
                "repo_id": None,
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            }
        ],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200

    row = next(
        entry
        for entry in response.json()["entries"]
        if entry["key"] == "discord:chan-zc"
    )
    assert row["workspace_path"] == str(workspace.path.resolve())
    assert row.get("repo_id") is None
    assert row.get("resource_kind") == "agent_workspace"
    assert row.get("resource_id") == workspace.id
    assert row["provenance"]["resource_kind"] == "agent_workspace"
    assert row["provenance"]["resource_id"] == workspace.id


def test_hub_channel_directory_route_includes_pma_managed_threads(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/pma-provenance",
        start_point="HEAD",
    )

    store = PmaThreadStore(hub_root)
    created = store.create_thread("codex", worktree.path, repo_id=worktree.id)
    managed_thread_id = str(created["managed_thread_id"])

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    pma_key = f"pma_thread:{managed_thread_id}"
    assert pma_key in rows
    pma_row = rows[pma_key]
    assert pma_row["repo_id"] == worktree.id
    assert pma_row["workspace_path"] == str(worktree.path)
    assert pma_row["source"] == "pma_thread"
    assert pma_row["provenance"]["source"] == "pma_thread"
    assert pma_row["provenance"]["agent"] == "codex"
    assert pma_row["provenance"]["managed_thread_id"] == managed_thread_id
    assert pma_row["channel_status"] == "idle"
    assert pma_row["status_label"] == "idle"
    assert pma_row["provenance"]["status_reason_code"] == "thread_created"

    filtered = client.get("/hub/chat/channels", params={"query": "pma", "limit": 1})
    assert filtered.status_code == 200
    filtered_rows = filtered.json()["entries"]
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["key"] == pma_key


def test_hub_ui_exposes_destination_and_channel_directory_controls() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    index_html = (
        repo_root / "src" / "codex_autorunner" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="hub-repo-search"' in index_html
    assert 'id="hub-refresh"' in index_html
    assert 'id="hub-new-agent"' in index_html
    assert 'id="hub-agent-workspace-list"' in index_html
    assert 'id="create-agent-workspace-modal"' in index_html
    assert 'id="hub-scan"' not in index_html
    assert 'id="hub-quick-scan"' not in index_html
    assert 'id="hub-channel-query"' not in index_html
    assert 'id="hub-channel-search"' not in index_html
    assert 'id="hub-channel-refresh"' not in index_html
    assert "Channel Directory" not in index_html
    assert "Copy Ref copies a channel ref" not in index_html

    hub_source = (
        repo_root / "src" / "codex_autorunner" / "static_src" / "hub.ts"
    ).read_text(encoding="utf-8")
    assert "set_destination" in hub_source
    assert "/hub/repos/${encodeURIComponent(repo.id)}/destination" in hub_source
    assert "/hub/jobs/agent-workspaces" in hub_source
    assert (
        "/hub/agent-workspaces/${encodeURIComponent(workspace.id)}/destination"
        in hub_source
    )
    assert (
        "/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/remove"
        in hub_source
    )
    assert (
        "/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/delete"
        in hub_source
    )
    assert "/hub/chat/channels" in hub_source
    assert "container_name" in hub_source
    assert "profile" in hub_source
    assert "workdir" in hub_source
    assert "env_passthrough" in hub_source
    assert "body.env =" in hub_source
    assert "mounts" in hub_source
    assert "read_only" in hub_source
    assert "hubRepoSearchInput" in hub_source
    assert "hub-chat-binding-row" in hub_source
    assert "renderAgentWorkspaces" in hub_source
    assert 'header.textContent = "Channels"' not in hub_source
    assert "copy_channel_key" not in hub_source
    assert "Copied channel ref" not in hub_source


def test_hub_repo_list_includes_last_run_duration_seconds(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _create_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )

    _seed_flow_run(
        repo.path,
        run_id="run-completed",
        status=FlowRunStatus.COMPLETED,
        diff_events=[],
        started_at="2026-03-13T08:00:00Z",
        finished_at="2026-03-13T09:45:00Z",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    assert repo_entry["last_run_duration_seconds"] == 6300.0
    run_state = repo_entry["run_state"] or {}
    assert run_state["duration_seconds"] == 6300.0
