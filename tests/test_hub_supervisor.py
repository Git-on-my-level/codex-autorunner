import json
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor, RepoStatus
from codex_autorunner.server import create_hub_app


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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


def _unwrap_fastapi(sub_app) -> Optional[FastAPI]:
    current = sub_app
    while not isinstance(current, FastAPI):
        current = getattr(current, "app", None)
        if current is None:
            return None
    return current


def _find_repo_mount(app: FastAPI, repo_id: str) -> Optional[Mount]:
    mount_path = f"/repos/{repo_id}"
    root = _unwrap_fastapi(app)
    if root is None:
        return None
    for route in root.router.routes:
        if isinstance(route, Mount) and route.path == mount_path:
            return route
    return None


def test_scan_writes_hub_state(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    supervisor = HubSupervisor(load_hub_config(hub_root))
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
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_dir, git_required=False)

    lock_path = repo_dir / ".codex-autorunner" / "lock"
    lock_path.write_text("999999", encoding="utf-8")

    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor.scan()
    snapshots = supervisor.list_repos()
    snap = next(r for r in snapshots if r.id == "demo")
    assert snap.status == RepoStatus.LOCKED
    assert snap.lock_status.value.startswith("locked")


def test_hub_api_lists_repos(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repos"][0]["id"] == "demo"


def test_hub_home_served_and_repo_mounted(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'id="hub-shell"' in resp.content

    assert (repo_dir / ".codex-autorunner" / "state.json").exists()
    assert not (repo_dir / ".codex-autorunner" / "config.yml").exists()

    state_resp = client.get("/repos/demo/api/state")
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert state["status"] == "idle"


def test_hub_repo_lifespan_started_for_mount(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        resp = client.get("/hub/repos")
        assert resp.status_code == 200
        mount = _find_repo_mount(app, "demo")
        assert mount is not None
        fastapi_app = _unwrap_fastapi(mount.app)
        assert fastapi_app is not None
        shutdown_event = getattr(fastapi_app.state, "shutdown_event", None)
        assert shutdown_event is not None
        assert shutdown_event.is_set() is False


def test_hub_scan_starts_lifespan_for_new_repo(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "alpha"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        resp = client.get("/hub/repos")
        assert resp.status_code == 200

        repo_new = hub_root / "beta"
        (repo_new / ".git").mkdir(parents=True, exist_ok=True)
        scan_resp = client.post("/hub/repos/scan")
        assert scan_resp.status_code == 200

        mount = _find_repo_mount(app, "beta")
        assert mount is not None
        fastapi_app = _unwrap_fastapi(mount.app)
        assert fastapi_app is not None
        shutdown_event = getattr(fastapi_app.state, "shutdown_event", None)
        assert shutdown_event is not None


def test_hub_scan_unmount_exits_lifespan(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        resp = client.get("/hub/repos")
        assert resp.status_code == 200
        mount = _find_repo_mount(app, "demo")
        assert mount is not None
        fastapi_app = _unwrap_fastapi(mount.app)
        assert fastapi_app is not None
        shutdown_event = getattr(fastapi_app.state, "shutdown_event", None)
        assert shutdown_event is not None
        assert shutdown_event.is_set() is False

        shutil.rmtree(repo_dir)
        scan_resp = client.post("/hub/repos/scan")
        assert scan_resp.status_code == 200
        assert _find_repo_mount(app, "demo") is None
        assert shutdown_event.is_set() is True


def test_hub_repo_id_sanitized_for_unsafe_name(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo#1"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    repo = next(r for r in resp.json()["repos"] if r["display_name"] == "demo#1")
    assert repo["id"] == "demo-1"
    state_resp = client.get("/repos/demo-1/api/state")
    assert state_resp.status_code == 200


def test_hub_init_endpoint_mounts_repo(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["auto_init_missing"] = False
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
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

        state_resp = client.get("/repos/demo/api/state")
        assert state_resp.status_code == 200


def test_parallel_run_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_a = hub_root / "alpha"
    repo_b = hub_root / "beta"
    (repo_a / ".git").mkdir(parents=True, exist_ok=True)
    (repo_b / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_a, git_required=False)
    seed_repo_files(repo_b, git_required=False)

    run_calls = []

    def fake_run_loop(self, stop_after_runs=None, external_stop_flag=None):
        run_calls.append(self.repo_root.name)
        time.sleep(0.05)

    monkeypatch.setattr(Engine, "run_loop", fake_run_loop)

    threads: list[threading.Thread] = []

    def spawn_fn(cmd: list[str], engine: Engine) -> object:
        action = cmd[3] if len(cmd) > 3 else ""
        once = action == "once" or "--once" in cmd

        def _run() -> None:
            engine.run_loop(stop_after_runs=1 if once else None)

        thread = threading.Thread(target=_run, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    supervisor = HubSupervisor(load_hub_config(hub_root), spawn_fn=spawn_fn)
    supervisor.scan()
    supervisor.run_repo("alpha", once=True)
    supervisor.run_repo("beta", once=True)

    for thread in threads:
        thread.join(timeout=1.0)

    snapshots = supervisor.list_repos()
    assert set(run_calls) == {"alpha", "beta"}
    for snap in snapshots:
        lock_path = snap.path / ".codex-autorunner" / "lock"
        assert not lock_path.exists()


def test_hub_clone_repo_endpoint(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

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
    assert (repo_dir / ".codex-autorunner" / "state.json").exists()


def test_hub_remove_repo_with_worktrees(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

    supervisor = HubSupervisor(load_hub_config(hub_root))
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(base_repo_id="base", branch="feature/test")

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
