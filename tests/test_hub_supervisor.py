import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG, load_config
from codex_autorunner.engine import Engine
from codex_autorunner.hub import HubSupervisor, RepoStatus
from codex_autorunner.server import create_hub_app


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_scan_writes_hub_state(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    supervisor = HubSupervisor(load_config(hub_root))  # type: ignore[arg-type]
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

    supervisor = HubSupervisor(load_config(hub_root))  # type: ignore[arg-type]
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

    state_resp = client.get("/repos/demo/api/state")
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert state["status"] == "idle"


def test_hub_init_endpoint_mounts_repo(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["auto_init_missing"] = False
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

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

    supervisor = HubSupervisor(load_config(hub_root))  # type: ignore[arg-type]
    supervisor.scan()
    supervisor.run_repo("alpha", once=True)
    supervisor.run_repo("beta", once=True)

    for runner in supervisor._runners.values():
        if runner._thread:
            runner._thread.join(timeout=1.0)

    snapshots = supervisor.list_repos()
    assert set(run_calls) == {"alpha", "beta"}
    for snap in snapshots:
        lock_path = snap.path / ".codex-autorunner" / "lock"
        assert not lock_path.exists()
