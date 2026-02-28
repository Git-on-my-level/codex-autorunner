import concurrent.futures
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor, RepoStatus
from codex_autorunner.core.locks import read_lock_info, write_lock_info
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.runner_controller import ProcessRunnerController
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.manifest import load_manifest, sanitize_repo_id, save_manifest
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


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


def _git_stdout(path: Path, *args: str) -> str:
    proc = run_git(list(args), path, check=True)
    return (proc.stdout or "").strip()


def _commit_file(path: Path, rel: str, content: str, message: str) -> str:
    file_path = path / rel
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    run_git(["add", rel], path, check=True)
    run_git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        path,
        check=True,
    )
    return _git_stdout(path, "rev-parse", "HEAD")


def _unwrap_fastapi_app(sub_app) -> Optional[FastAPI]:
    current = sub_app
    while not isinstance(current, FastAPI):
        current = getattr(current, "app", None)
        if current is None:
            return None
    return current


def _get_mounted_app(app: FastAPI, mount_path: str):
    for route in app.router.routes:
        if isinstance(route, Mount) and route.path == mount_path:
            return route.app
    return None


def _write_discord_binding(hub_root: Path, *, channel_id: str, repo_id: str) -> None:
    db_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    repo_id TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO channel_bindings (channel_id, repo_id)
                VALUES (?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET repo_id=excluded.repo_id
                """,
                (channel_id, repo_id),
            )
    finally:
        conn.close()


def test_scan_writes_hub_state(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
    )
    snapshots = supervisor.scan()

    state_path = hub_root / ".codex-autorunner" / "hub_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["last_scan_at"]
    snap = next(r for r in snapshots if r.id == "demo")
    assert snap.initialized is True
    state_repo = next(r for r in payload["repos"] if r["id"] == "demo")
    assert state_repo["status"] == snap.status.value


def test_locked_status_reported(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_dir, git_required=False)

    lock_path = repo_dir / ".codex-autorunner" / "lock"
    lock_started_at = "2025-01-01T00:00:00Z"
    write_lock_info(lock_path, 999999, started_at=lock_started_at)
    lock_info = read_lock_info(lock_path)
    assert lock_info.pid == 999999
    assert lock_info.started_at == lock_started_at

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
    )
    supervisor.scan()
    snapshots = supervisor.list_repos()
    snap = next(r for r in snapshots if r.id == "demo")
    assert snap.status == RepoStatus.LOCKED
    assert snap.lock_status.value.startswith("locked")


def test_hub_api_lists_repos(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repos"][0]["id"] == "demo"
    assert data["repos"][0]["effective_destination"] == {"kind": "local"}


def test_hub_api_exposes_effective_destination_inherited_from_base(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-inherit",
        start_point="HEAD",
    )
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base_entry = manifest.get("base")
    assert base_entry is not None
    base_entry.destination = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    save_manifest(manifest_path, manifest, hub_root)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    base_payload = next(item for item in data["repos"] if item["id"] == "base")
    worktree_payload = next(item for item in data["repos"] if item["id"] == worktree.id)
    expected = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    assert base_payload["effective_destination"] == expected
    assert worktree_payload["effective_destination"] == expected


def test_hub_api_marks_chat_bound_worktrees(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-bound",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    store.create_thread("codex", worktree.path, repo_id=worktree.id)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    worktree_payload = next(item for item in data["repos"] if item["id"] == worktree.id)
    assert worktree_payload["chat_bound"] is True
    assert worktree_payload["chat_bound_thread_count"] == 1


def test_hub_api_marks_chat_bound_worktrees_without_thread_list_cap(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-bound-uncapped",
        start_point="HEAD",
    )

    def _fail_list_threads(self, **_kwargs):
        raise AssertionError("list_threads should not be used for chat-bound counts")

    def _fake_count_threads_by_repo(self, *, agent=None, status=None):
        assert agent is None
        assert status == "active"
        return {worktree.id: 1, "noise-repo": 9001}

    monkeypatch.setattr(PmaThreadStore, "list_threads", _fail_list_threads)
    monkeypatch.setattr(
        PmaThreadStore, "count_threads_by_repo", _fake_count_threads_by_repo
    )
    PmaThreadStore(hub_root)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    worktree_payload = next(item for item in data["repos"] if item["id"] == worktree.id)
    assert worktree_payload["chat_bound"] is True
    assert worktree_payload["chat_bound_thread_count"] == 1


def test_hub_pin_parent_repo_endpoint_persists(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)

    pin_resp = client.post("/hub/repos/demo/pin", json={"pinned": True})
    assert pin_resp.status_code == 200
    assert "demo" in pin_resp.json()["pinned_parent_repo_ids"]

    list_resp = client.get("/hub/repos")
    assert list_resp.status_code == 200
    assert "demo" in list_resp.json()["pinned_parent_repo_ids"]

    state_path = hub_root / ".codex-autorunner" / "hub_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "demo" in state["pinned_parent_repo_ids"]

    unpin_resp = client.post("/hub/repos/demo/pin", json={"pinned": False})
    assert unpin_resp.status_code == 200
    assert "demo" not in unpin_resp.json()["pinned_parent_repo_ids"]


def test_hub_pin_parent_repo_rejects_worktree(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/pin-reject",
        start_point="HEAD",
    )

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.post(f"/hub/repos/{worktree.id}/pin", json={"pinned": True})
    assert resp.status_code == 400
    assert "Only base repos can be pinned" in resp.json()["detail"]


def test_list_repos_thread_safety(tmp_path: Path):
    """Test that list_repos is thread-safe and doesn't return None or inconsistent state."""
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    supervisor = HubSupervisor.from_path(hub_root)

    results = []
    errors = []

    def call_list_repos():
        try:
            repos = supervisor.list_repos(use_cache=False)
            results.append(repos)
        except Exception as e:
            errors.append(e)

    def invalidate_cache():
        supervisor._invalidate_list_cache()

    num_threads = 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for i in range(num_threads):
            if i % 2 == 0:
                futures.append(executor.submit(call_list_repos))
            else:
                futures.append(executor.submit(invalidate_cache))
        concurrent.futures.wait(futures)

    # No errors should have occurred
    assert len(errors) == 0, f"Errors occurred: {errors}"

    # All results should be non-empty lists
    for i, repos in enumerate(results):
        assert repos is not None, f"Result {i} was None"
        assert isinstance(repos, list), f"Result {i} was not a list: {type(repos)}"

    # All results should have the same repo IDs
    if results:
        repo_ids_sets = [set(repo.id for repo in repos) for repos in results]
        first_ids = repo_ids_sets[0]
        for i, ids in enumerate(repo_ids_sets[1:], 1):
            assert (
                ids == first_ids
            ), f"Result {i} has different repo IDs: {ids} vs {first_ids}"


def test_hub_home_served_and_repo_mounted(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'id="hub-shell"' in resp.content

    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()
    assert not (repo_dir / ".codex-autorunner" / "config.yml").exists()


def test_hub_mount_enters_repo_lifespan(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app):
        sub_app = _get_mounted_app(app, "/repos/demo")
        assert sub_app is not None
        fastapi_app = _unwrap_fastapi_app(sub_app)
        assert fastapi_app is not None
        assert hasattr(fastapi_app.state, "shutdown_event")


def test_hub_scan_starts_repo_lifespan(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        repo_dir = hub_root / "demo#scan"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

        resp = client.post("/hub/repos/scan")
        assert resp.status_code == 200
        payload = resp.json()
        entry = next(r for r in payload["repos"] if r["display_name"] == "demo#scan")
        assert entry["id"] == sanitize_repo_id("demo#scan")
        assert entry["mounted"] is True

        sub_app = _get_mounted_app(app, f"/repos/{entry['id']}")
        assert sub_app is not None
        fastapi_app = _unwrap_fastapi_app(sub_app)
        assert fastapi_app is not None
        assert hasattr(fastapi_app.state, "shutdown_event")


def test_hub_scan_unmounts_repo_and_exits_lifespan(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        sub_app = _get_mounted_app(app, "/repos/demo")
        assert sub_app is not None
        fastapi_app = _unwrap_fastapi_app(sub_app)
        assert fastapi_app is not None
        shutdown_event = fastapi_app.state.shutdown_event
        assert shutdown_event.is_set() is False

        shutil.rmtree(repo_dir)

        resp = client.post("/hub/repos/scan")
        assert resp.status_code == 200
        assert shutdown_event.is_set() is True
        assert _get_mounted_app(app, "/repos/demo") is None


@pytest.mark.slow
def test_hub_create_repo_keeps_existing_mounts(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_dir = hub_root / "alpha"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        assert _get_mounted_app(app, "/repos/alpha") is not None

        resp = client.post("/hub/repos", json={"id": "beta"})
        assert resp.status_code == 200
        assert _get_mounted_app(app, "/repos/alpha") is not None
        assert _get_mounted_app(app, "/repos/beta") is not None


def test_hub_init_endpoint_mounts_repo(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["auto_init_missing"] = False
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)

    scan_resp = client.post("/hub/repos/scan")
    assert scan_resp.status_code == 200
    scan_payload = scan_resp.json()
    demo = next(r for r in scan_payload["repos"] if r["id"] == "demo")
    assert demo["initialized"] is False

    init_resp = client.post("/hub/repos/demo/init")
    assert init_resp.status_code == 200
    init_payload = init_resp.json()
    assert init_payload["initialized"] is True
    assert init_payload["mounted"] is True
    assert init_payload.get("mount_error") is None


@pytest.mark.slow
def test_parallel_run_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)
    repo_a = hub_root / "alpha"
    repo_b = hub_root / "beta"
    (repo_a / ".git").mkdir(parents=True, exist_ok=True)
    (repo_b / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_a, git_required=False)
    seed_repo_files(repo_b, git_required=False)

    run_calls = []

    def fake_start(self, once: bool = False) -> None:
        run_calls.append(self.ctx.repo_root.name)
        time.sleep(0.05)

    monkeypatch.setattr(ProcessRunnerController, "start", fake_start)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.scan()
    supervisor.run_repo("alpha", once=True)
    supervisor.run_repo("beta", once=True)

    time.sleep(0.2)

    snapshots = supervisor.list_repos()
    assert set(run_calls) == {"alpha", "beta"}
    for snap in snapshots:
        lock_path = snap.path / ".codex-autorunner" / "lock"
        assert not lock_path.exists()


def test_hub_clone_repo_endpoint(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    source_repo = tmp_path / "source"
    _init_git_repo(source_repo)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.post(
        "/hub/repos",
        json={"git_url": str(source_repo), "id": "cloned"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == "cloned"
    repo_dir = hub_root / "cloned"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()


@pytest.mark.slow
def test_hub_remove_repo_with_worktrees(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/test",
        start_point="HEAD",
    )

    dirty_file = base.path / "DIRTY.txt"
    dirty_file.write_text("dirty\n", encoding="utf-8")

    app = create_hub_app(hub_root)
    client = TestClient(app)
    check_resp = client.get("/hub/repos/base/remove-check")
    assert check_resp.status_code == 200
    check_payload = check_resp.json()
    assert check_payload["is_clean"] is False
    assert worktree.id in check_payload["worktrees"]

    remove_resp = client.post(
        "/hub/repos/base/remove",
        json={"force": True, "delete_dir": True, "delete_worktrees": True},
    )
    assert remove_resp.status_code == 200
    assert not base.path.exists()
    assert not worktree.path.exists()


def test_sync_main_raises_when_local_default_diverges_from_origin(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    origin = tmp_path / "origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--bare"], origin, check=True)

    seed = tmp_path / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    run_git(["init"], seed, check=True)
    run_git(["branch", "-M", "main"], seed, check=True)
    _commit_file(seed, "README.md", "seed\n", "seed init")
    run_git(["remote", "add", "origin", str(origin)], seed, check=True)
    run_git(["push", "-u", "origin", "main"], seed, check=True)
    run_git(["symbolic-ref", "HEAD", "refs/heads/main"], origin, check=True)

    repo_dir = hub_root / "base"
    run_git(["clone", str(origin), str(repo_dir)], hub_root, check=True)
    local_sha = _commit_file(repo_dir, "LOCAL.txt", "local\n", "local only")
    origin_sha = _git_stdout(origin, "rev-parse", "refs/heads/main")
    assert local_sha != origin_sha

    supervisor = HubSupervisor.from_path(hub_root)
    supervisor.scan()

    with pytest.raises(ValueError, match="did not land on origin/main"):
        supervisor.sync_main("base")


def test_create_worktree_defaults_to_origin_default_branch_without_start_point(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    run_git(["branch", "-M", "master"], base.path, check=True)
    origin = tmp_path / "origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--bare"], origin, check=True)
    run_git(["remote", "add", "origin", str(origin)], base.path, check=True)
    run_git(["push", "-u", "origin", "master"], base.path, check=True)
    run_git(["symbolic-ref", "HEAD", "refs/heads/master"], origin, check=True)
    origin_default_sha = _git_stdout(base.path, "rev-parse", "origin/master")

    local_sha = _commit_file(base.path, "LOCAL.txt", "local\n", "local only")
    assert local_sha != origin_default_sha

    worktree = supervisor.create_worktree(base_repo_id="base", branch="feature/test")
    assert worktree.branch == "feature/test"
    assert worktree.path.exists()
    assert _git_stdout(worktree.path, "rev-parse", "HEAD") == origin_default_sha


def test_create_worktree_fails_if_explicit_start_point_mismatches_existing_branch(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    first_sha = _git_stdout(base.path, "rev-list", "--max-parents=0", "HEAD")
    _commit_file(base.path, "SECOND.txt", "second\n", "second")
    head_sha = _git_stdout(base.path, "rev-parse", "HEAD")
    assert first_sha != head_sha
    run_git(["branch", "feature/test", first_sha], base.path, check=True)

    with pytest.raises(ValueError, match="already exists and points to"):
        supervisor.create_worktree(
            base_repo_id="base",
            branch="feature/test",
            start_point="HEAD",
        )


def test_create_worktree_runs_configured_setup_commands(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    supervisor.set_worktree_setup_commands(
        "base", ["echo ready > SETUP_OK.txt", "echo done >> SETUP_OK.txt"]
    )

    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/setup-ok",
        start_point="HEAD",
    )
    setup_file = worktree.path / "SETUP_OK.txt"
    assert setup_file.exists()
    assert setup_file.read_text(encoding="utf-8") == "ready\ndone\n"
    log_path = worktree.path / ".codex-autorunner" / "logs" / "worktree-setup.log"
    assert log_path.exists()
    assert "commands=2" in log_path.read_text(encoding="utf-8")


def test_create_worktree_fails_setup_and_keeps_worktree(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    supervisor.set_worktree_setup_commands(
        "base", ["echo ok > PRE_FAIL.txt", "exit 17"]
    )

    with pytest.raises(ValueError, match="Worktree setup failed for command 2/2"):
        supervisor.create_worktree(
            base_repo_id="base",
            branch="feature/setup-fail",
            start_point="HEAD",
        )

    worktree_path = hub_root / "worktrees" / "base--feature-setup-fail"
    worktree_repo_id = "base--feature-setup-fail"
    assert worktree_path.exists()
    assert (worktree_path / "PRE_FAIL.txt").read_text(encoding="utf-8").strip() == "ok"
    log_text = (
        worktree_path / ".codex-autorunner" / "logs" / "worktree-setup.log"
    ).read_text(encoding="utf-8")
    assert "$ exit 17" in log_text
    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    assert manifest.get(worktree_repo_id) is not None

    supervisor.cleanup_worktree(worktree_repo_id=worktree_repo_id, archive=False)
    assert not worktree_path.exists()


def test_run_setup_commands_for_workspace_runs_base_commands_for_worktree(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    supervisor.set_worktree_setup_commands("base", ["echo setup >> NEWT_SETUP.txt"])
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/newt-setup",
        start_point="HEAD",
    )

    count = supervisor.run_setup_commands_for_workspace(
        worktree.path,
        repo_id_hint=worktree.id,
    )

    assert count == 1
    setup_file = worktree.path / "NEWT_SETUP.txt"
    assert setup_file.read_text(encoding="utf-8") == "setup\nsetup\n"


def test_run_setup_commands_for_workspace_uses_resolved_repo_path_with_hint(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    supervisor.set_worktree_setup_commands("base", ["echo target >> HINT_TARGET.txt"])
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/newt-setup-hint",
        start_point="HEAD",
    )

    stale_workspace = tmp_path / "stale-workspace"
    stale_workspace.mkdir(parents=True)

    count = supervisor.run_setup_commands_for_workspace(
        stale_workspace,
        repo_id_hint=worktree.id,
    )

    assert count == 1
    assert (worktree.path / "HINT_TARGET.txt").read_text(encoding="utf-8") == (
        "target\ntarget\n"
    )
    assert not (stale_workspace / "HINT_TARGET.txt").exists()


def test_cleanup_worktree_with_archive_rejects_dirty_worktree(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/dirty-guard",
        start_point="HEAD",
    )
    (worktree.path / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(
        ValueError, match="has uncommitted changes; commit or stash before archiving"
    ):
        supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True)

    assert worktree.path.exists()
    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    assert manifest.get(worktree.id) is not None


def test_cleanup_worktree_without_archive_allows_dirty_worktree(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/dirty-no-archive",
        start_point="HEAD",
    )
    (worktree.path / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")

    supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=False)
    assert not worktree.path.exists()


def test_cleanup_worktree_rejects_chat_bound_without_force(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-guard",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    store.create_thread("codex", worktree.path, repo_id=worktree.id)

    with pytest.raises(
        ValueError,
        match="Refusing to clean up chat-bound worktree",
    ):
        supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True)

    assert worktree.path.exists()


def test_cleanup_worktree_allows_chat_bound_with_force(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-guard-force",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    store.create_thread("codex", worktree.path, repo_id=worktree.id)

    supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True, force=True)
    assert not worktree.path.exists()


def test_cleanup_worktree_rejects_when_binding_lookup_fails_without_force(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-binding-error",
        start_point="HEAD",
    )

    def _raise_lookup_error(_repo_id: str) -> bool:
        raise RuntimeError("db temporarily unavailable")

    monkeypatch.setattr(supervisor, "_has_active_chat_binding", _raise_lookup_error)

    with pytest.raises(
        ValueError,
        match="Unable to verify active chat bindings",
    ):
        supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True)

    assert worktree.path.exists()


def test_cleanup_worktree_allows_force_when_binding_lookup_fails(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-binding-error-force",
        start_point="HEAD",
    )

    def _raise_lookup_error(_repo_id: str) -> bool:
        raise RuntimeError("db temporarily unavailable")

    monkeypatch.setattr(supervisor, "_has_active_chat_binding", _raise_lookup_error)

    supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True, force=True)
    assert not worktree.path.exists()


def test_hub_api_marks_chat_bound_worktrees_from_discord_binding_db(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    write_test_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/discord-bound",
        start_point="HEAD",
    )
    _write_discord_binding(hub_root, channel_id="discord-chan-1", repo_id=worktree.id)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    worktree_payload = next(item for item in data["repos"] if item["id"] == worktree.id)
    assert worktree_payload["chat_bound"] is True
    assert worktree_payload["chat_bound_thread_count"] == 1


def test_cleanup_worktree_rejects_discord_bound_worktree_without_force(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/discord-bound-cleanup-guard",
        start_point="HEAD",
    )
    _write_discord_binding(hub_root, channel_id="discord-chan-2", repo_id=worktree.id)

    with pytest.raises(
        ValueError,
        match="Refusing to clean up chat-bound worktree",
    ):
        supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=False)

    assert worktree.path.exists()


def test_set_worktree_setup_commands_route_updates_manifest(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    app = create_hub_app(hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/repos/base/worktree-setup",
        json={"commands": ["make setup", "pre-commit install"]},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["worktree_setup_commands"] == ["make setup", "pre-commit install"]

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.worktree_setup_commands == ["make setup", "pre-commit install"]


def test_set_worktree_setup_commands_route_accepts_legacy_array_payload(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    app = create_hub_app(hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/repos/base/worktree-setup",
        json=["make setup", "  ", "pre-commit install"],
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["worktree_setup_commands"] == ["make setup", "pre-commit install"]


def test_set_repo_destination_route_updates_manifest(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    app = create_hub_app(hub_root)
    client = TestClient(app)

    expected = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    resp = client.post(
        "/hub/repos/base/destination",
        json={"destination": expected},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["effective_destination"] == expected

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == expected


def test_set_repo_destination_route_accepts_direct_destination_payload(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    app = create_hub_app(hub_root)
    client = TestClient(app)

    expected = {"kind": "docker", "image": "ghcr.io/acme/base:edge"}
    resp = client.post(
        "/hub/repos/base/destination",
        json=expected,
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["effective_destination"] == expected

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == expected


def test_set_repo_settings_route_updates_manifest_atomically(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    app = create_hub_app(hub_root)
    client = TestClient(app)

    destination = {"kind": "docker", "image": "ghcr.io/acme/base:atomic"}
    commands = ["make setup", "pre-commit install", "  "]
    resp = client.post(
        "/hub/repos/base/settings",
        json={"destination": destination, "commands": commands},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["effective_destination"] == destination
    assert payload["worktree_setup_commands"] == ["make setup", "pre-commit install"]

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == destination
    assert entry.worktree_setup_commands == ["make setup", "pre-commit install"]


def test_set_repo_destination_route_remounts_base_and_dependents(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-route-remount",
        start_point="HEAD",
    )
    app = create_hub_app(hub_root)

    with TestClient(app) as client:
        base_before = _get_mounted_app(app, "/repos/base")
        worktree_before = _get_mounted_app(app, f"/repos/{worktree.id}")
        assert base_before is not None
        assert worktree_before is not None

        resp = client.post(
            "/hub/repos/base/destination",
            json={"destination": {"kind": "local"}},
        )
        assert resp.status_code == 200

        base_after = _get_mounted_app(app, "/repos/base")
        worktree_after = _get_mounted_app(app, f"/repos/{worktree.id}")
        assert base_after is not None
        assert worktree_after is not None
        assert base_after is not base_before
        assert worktree_after is not worktree_before


def test_set_repo_settings_route_remounts_base_and_dependents(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/settings-route-remount",
        start_point="HEAD",
    )
    app = create_hub_app(hub_root)

    with TestClient(app) as client:
        base_before = _get_mounted_app(app, "/repos/base")
        worktree_before = _get_mounted_app(app, f"/repos/{worktree.id}")
        assert base_before is not None
        assert worktree_before is not None

        resp = client.post(
            "/hub/repos/base/settings",
            json={
                "destination": {"kind": "local"},
                "commands": ["make setup"],
            },
        )
        assert resp.status_code == 200

        base_after = _get_mounted_app(app, "/repos/base")
        worktree_after = _get_mounted_app(app, f"/repos/{worktree.id}")
        assert base_after is not None
        assert worktree_after is not None
        assert base_after is not base_before
        assert worktree_after is not worktree_before


def test_set_repo_destination_route_force_remount_skips_uninitialized_dependents(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-route-uninitialized-dependent",
        start_point="HEAD",
    )
    app = create_hub_app(hub_root)

    with TestClient(app) as client:
        assert _get_mounted_app(app, f"/repos/{worktree.id}") is not None

        tickets_dir = worktree.path / ".codex-autorunner" / "tickets"
        shutil.rmtree(tickets_dir)
        assert not tickets_dir.exists()

        resp = client.post(
            "/hub/repos/base/destination",
            json={"destination": {"kind": "local"}},
        )
        assert resp.status_code == 200

        assert _get_mounted_app(app, "/repos/base") is not None
        assert _get_mounted_app(app, f"/repos/{worktree.id}") is None

        list_resp = client.get("/hub/repos")
        assert list_resp.status_code == 200
        repos = list_resp.json()["repos"]
        worktree_payload = next(item for item in repos if item["id"] == worktree.id)
        assert worktree_payload["initialized"] is False
        assert worktree_payload["mounted"] is False


def test_set_repo_destination_invalidates_cached_runners_for_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-invalidation",
        start_point="HEAD",
    )

    stopped: list[str] = []

    def fake_stop(self) -> None:
        stopped.append(self.repo_id)

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", fake_stop)

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None
    assert set(supervisor._runners) == {"base", worktree.id}

    destination = {"kind": "docker", "image": "ghcr.io/acme/base:invalidate"}
    snapshot = supervisor.set_repo_destination("base", destination)
    assert snapshot.effective_destination == destination
    assert set(stopped) == {"base", worktree.id}
    assert "base" not in supervisor._runners
    assert worktree.id not in supervisor._runners

    rebuilt = supervisor._ensure_runner("base")
    assert rebuilt is not None
    assert rebuilt is not base_runner


def test_set_repo_destination_invalidates_uncached_runners_for_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-invalidation-uncached",
        start_point="HEAD",
    )
    assert supervisor._runners == {}

    stopped: list[str] = []

    def fake_stop(self) -> None:
        stopped.append(self.repo_id)

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", fake_stop)

    destination = {"kind": "docker", "image": "ghcr.io/acme/base:uncached"}
    snapshot = supervisor.set_repo_destination("base", destination)
    assert snapshot.effective_destination == destination
    assert set(stopped) == {"base", worktree.id}
    assert supervisor._runners == {}


def test_set_repo_destination_save_failure_keeps_runners_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-save-failure",
        start_point="HEAD",
    )

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    previous_destination = {"kind": "docker", "image": "ghcr.io/acme/base:before-save"}
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    entry.destination = previous_destination
    save_manifest(manifest_path, manifest, hub_root)

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None

    stopped: list[str] = []

    def fake_stop(self) -> None:
        stopped.append(self.repo_id)

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", fake_stop)

    def failing_save_manifest(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "codex_autorunner.core.hub.save_manifest", failing_save_manifest
    )

    with pytest.raises(OSError, match="disk full"):
        supervisor.set_repo_destination(
            "base",
            {"kind": "docker", "image": "ghcr.io/acme/base:after-save"},
        )

    assert stopped == []
    assert supervisor._runners.get("base") is base_runner
    assert supervisor._runners.get(worktree.id) is worktree_runner
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == previous_destination


def test_set_repo_destination_stop_failure_keeps_failed_runner_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-invalidation-stop-failure",
        start_point="HEAD",
    )

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    previous_destination = {"kind": "docker", "image": "ghcr.io/acme/base:before-fail"}
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    entry.destination = previous_destination
    save_manifest(manifest_path, manifest, hub_root)

    def flaky_stop(self) -> None:
        if self.repo_id == worktree.id:
            raise RuntimeError("boom")

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", flaky_stop)

    with pytest.raises(ValueError, match="Failed to stop runner\\(s\\)"):
        supervisor.set_repo_destination(
            "base",
            {"kind": "docker", "image": "ghcr.io/acme/base:stop-failure"},
        )

    assert supervisor._runners.get("base") is base_runner
    assert supervisor._runners.get(worktree.id) is worktree_runner
    assert supervisor._ensure_runner("base") is base_runner
    assert supervisor._ensure_runner(worktree.id) is worktree_runner
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == previous_destination

    def successful_stop(self) -> None:
        return None

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", successful_stop)

    snapshot = supervisor.set_repo_destination(
        "base",
        {"kind": "docker", "image": "ghcr.io/acme/base:recovered"},
    )
    assert snapshot.effective_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:recovered",
    }
    assert "base" not in supervisor._runners
    assert worktree.id not in supervisor._runners


def test_set_repo_settings_invalidates_cached_runner(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")

    old_runner = supervisor._ensure_runner("base")
    assert old_runner is not None

    snapshot = supervisor.set_repo_settings(
        "base",
        {"kind": "docker", "image": "ghcr.io/acme/base:settings"},
        ["make setup", "pre-commit install"],
    )
    assert snapshot.effective_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:settings",
    }
    assert snapshot.worktree_setup_commands == ["make setup", "pre-commit install"]
    assert "base" not in supervisor._runners

    new_runner = supervisor._ensure_runner("base")
    assert new_runner is not None
    assert new_runner is not old_runner


def test_set_repo_settings_serializes_manifest_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")

    monkeypatch.setattr(
        supervisor, "_stop_and_invalidate_runners", lambda *_args, **_kwargs: None
    )

    first_in_lock = threading.Event()
    first_left_lock = threading.Event()
    second_blocked_on_lock = threading.Event()
    second_entered_lock = threading.Event()
    allow_first_save = threading.Event()
    first_save_called = threading.Event()
    errors: list[Exception] = []

    class ObservedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._owner: Optional[str] = None

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            acquired = self._lock.acquire(False)
            if acquired:
                self._owner = threading.current_thread().name
                if self._owner == "settings-thread":
                    first_in_lock.set()
                elif self._owner == "destination-thread":
                    second_entered_lock.set()
                return True
            if threading.current_thread().name == "destination-thread":
                second_blocked_on_lock.set()
            acquired = self._lock.acquire(blocking, timeout)
            if acquired:
                self._owner = threading.current_thread().name
                if self._owner == "settings-thread":
                    first_in_lock.set()
                elif self._owner == "destination-thread":
                    second_entered_lock.set()
            return acquired

        def release(self) -> None:
            owner = self._owner
            self._owner = None
            if owner == "settings-thread":
                first_left_lock.set()
            self._lock.release()

        def __enter__(self) -> "ObservedLock":
            self.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            self.release()
            return False

    supervisor._base_repo_settings_lock = ObservedLock()

    original_save_manifest = save_manifest

    def gated_save_manifest(path: Path, manifest, root: Path) -> None:
        thread_name = threading.current_thread().name
        if thread_name == "settings-thread":
            first_save_called.set()
            assert allow_first_save.wait(timeout=3.0)
            original_save_manifest(path, manifest, root)
            return
        if thread_name == "destination-thread":
            original_save_manifest(path, manifest, root)
            return
        original_save_manifest(path, manifest, root)

    monkeypatch.setattr("codex_autorunner.core.hub.save_manifest", gated_save_manifest)

    destination_settings = {"kind": "docker", "image": "ghcr.io/acme/base:settings"}
    destination_final = {"kind": "docker", "image": "ghcr.io/acme/base:final"}

    def run_settings() -> None:
        try:
            supervisor.set_repo_settings(
                "base", destination_settings, ["echo from-settings"]
            )
        except Exception as exc:  # pragma: no cover - assertion below inspects errors.
            errors.append(exc)

    def run_destination() -> None:
        try:
            supervisor.set_repo_destination("base", destination_final)
        except Exception as exc:  # pragma: no cover - assertion below inspects errors.
            errors.append(exc)

    settings_thread = threading.Thread(target=run_settings, name="settings-thread")
    settings_thread.start()
    assert first_in_lock.wait(timeout=3.0)
    destination_thread = threading.Thread(
        target=run_destination, name="destination-thread"
    )
    destination_thread.start()
    assert first_save_called.wait(timeout=3.0)
    assert second_blocked_on_lock.wait(timeout=3.0)
    assert first_left_lock.is_set() is False
    allow_first_save.set()
    assert first_left_lock.wait(timeout=3.0)
    assert second_entered_lock.wait(timeout=3.0)
    settings_thread.join(timeout=3.0)
    destination_thread.join(timeout=3.0)

    assert settings_thread.is_alive() is False
    assert destination_thread.is_alive() is False
    assert errors == []

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == destination_final
    assert entry.worktree_setup_commands == ["echo from-settings"]


def test_set_repo_settings_save_failure_keeps_runners_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/settings-save-failure",
        start_point="HEAD",
    )

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    previous_destination = {
        "kind": "docker",
        "image": "ghcr.io/acme/base:settings-before-save",
    }
    previous_commands = ["echo before"]
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    entry.destination = previous_destination
    entry.worktree_setup_commands = previous_commands
    save_manifest(manifest_path, manifest, hub_root)

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None

    stopped: list[str] = []

    def fake_stop(self) -> None:
        stopped.append(self.repo_id)

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", fake_stop)

    def failing_save_manifest(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "codex_autorunner.core.hub.save_manifest", failing_save_manifest
    )

    with pytest.raises(OSError, match="disk full"):
        supervisor.set_repo_settings(
            "base",
            {"kind": "docker", "image": "ghcr.io/acme/base:settings-after-save"},
            ["echo after"],
        )

    assert stopped == []
    assert supervisor._runners.get("base") is base_runner
    assert supervisor._runners.get(worktree.id) is worktree_runner
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == previous_destination
    assert entry.worktree_setup_commands == previous_commands


def test_set_repo_settings_stop_failure_keeps_manifest_and_failed_runner_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/settings-invalidation-stop-failure",
        start_point="HEAD",
    )

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    previous_destination = {"kind": "docker", "image": "ghcr.io/acme/base:settings-old"}
    previous_commands = ["echo old"]
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    entry.destination = previous_destination
    entry.worktree_setup_commands = previous_commands
    save_manifest(manifest_path, manifest, hub_root)

    def flaky_stop(self) -> None:
        if self.repo_id == worktree.id:
            raise RuntimeError("boom")

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", flaky_stop)

    with pytest.raises(ValueError, match="Failed to stop runner\\(s\\)"):
        supervisor.set_repo_settings(
            "base",
            {"kind": "docker", "image": "ghcr.io/acme/base:settings-new"},
            ["echo new"],
        )

    assert supervisor._runners.get("base") is base_runner
    assert supervisor._runners.get(worktree.id) is worktree_runner
    assert supervisor._ensure_runner("base") is base_runner
    manifest = load_manifest(manifest_path, hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == previous_destination
    assert entry.worktree_setup_commands == previous_commands

    def successful_stop(self) -> None:
        return None

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", successful_stop)
    snapshot = supervisor.set_repo_settings(
        "base",
        {"kind": "docker", "image": "ghcr.io/acme/base:settings-recovered"},
        ["echo recovered"],
    )
    assert snapshot.effective_destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:settings-recovered",
    }
    assert snapshot.worktree_setup_commands == ["echo recovered"]
    assert "base" not in supervisor._runners
    assert worktree.id not in supervisor._runners


def test_set_repo_destination_rollback_save_failure_restores_runners_with_once_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/settings-rollback-save-failure",
        start_point="HEAD",
    )

    base_runner = supervisor._ensure_runner("base")
    worktree_runner = supervisor._ensure_runner(worktree.id)
    assert base_runner is not None
    assert worktree_runner is not None
    base_runner._last_once = True

    monkeypatch.setattr(
        "codex_autorunner.core.hub.RepoRunner.running",
        property(lambda self: self.repo_id == "base"),
    )

    def flaky_stop(self) -> None:
        if self.repo_id == worktree.id:
            raise RuntimeError("boom")

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.stop", flaky_stop)

    start_calls: list[tuple[str, bool]] = []

    def fake_start(self, once: bool = False) -> None:
        start_calls.append((self.repo_id, once))

    monkeypatch.setattr("codex_autorunner.core.hub.RepoRunner.start", fake_start)

    save_calls = 0

    def fail_on_rollback(path: Path, manifest, root: Path) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls > 1:
            raise OSError("rollback write failed")
        save_manifest(path, manifest, root)

    monkeypatch.setattr("codex_autorunner.core.hub.save_manifest", fail_on_rollback)

    with pytest.raises(ValueError, match="Failed to roll back manifest"):
        supervisor.set_repo_destination(
            "base",
            {"kind": "docker", "image": "ghcr.io/acme/base:rollback-failure"},
        )

    assert supervisor._runners.get("base") is base_runner
    assert supervisor._runners.get(worktree.id) is worktree_runner
    assert start_calls == [("base", True)]


def test_set_repo_settings_route_validation_failure_is_atomic(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    supervisor.create_repo("base")
    supervisor.set_repo_destination("base", {"kind": "local"})
    supervisor.set_worktree_setup_commands("base", ["echo initial"])

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.post(
        "/hub/repos/base/settings",
        json={
            "destination": {"kind": "docker"},
            "commands": ["echo changed"],
        },
    )
    assert resp.status_code == 400
    assert "requires non-empty 'image'" in resp.json()["detail"]

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    entry = manifest.get("base")
    assert entry is not None
    assert entry.destination == {"kind": "local"}
    assert entry.worktree_setup_commands == ["echo initial"]


def test_set_repo_destination_route_rejects_invalid_payload(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/destination-route-reject",
        start_point="HEAD",
    )
    app = create_hub_app(hub_root)
    client = TestClient(app)

    invalid = client.post(
        "/hub/repos/base/destination",
        json={"destination": {"kind": "docker"}},
    )
    assert invalid.status_code == 400
    assert "requires non-empty 'image'" in invalid.json()["detail"]

    wrong_repo_kind = client.post(
        f"/hub/repos/{worktree.id}/destination",
        json={"kind": "local"},
    )
    assert wrong_repo_kind.status_code == 400
    assert "only be configured on base repos" in wrong_repo_kind.json()["detail"]
