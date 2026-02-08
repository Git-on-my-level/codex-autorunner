import concurrent.futures
import json
import shutil
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
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor, RepoStatus
from codex_autorunner.core.runner_controller import ProcessRunnerController
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.manifest import load_manifest, sanitize_repo_id
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


def test_scan_writes_hub_state(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
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
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_dir, git_required=False)

    lock_path = repo_dir / ".codex-autorunner" / "lock"
    lock_path.write_text("999999", encoding="utf-8")

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
    _write_config(cfg_path, cfg)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    client = TestClient(app)
    resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repos"][0]["id"] == "demo"


def test_list_repos_thread_safety(tmp_path: Path):
    """Test that list_repos is thread-safe and doesn't return None or inconsistent state."""
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

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
    _write_config(cfg_path, cfg)
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
    _write_config(cfg_path, cfg)
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
    _write_config(cfg_path, cfg)

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
    _write_config(cfg_path, cfg)
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


def test_hub_create_repo_keeps_existing_mounts(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)
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
    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()


def test_hub_remove_repo_with_worktrees(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg_path = hub_root / CONFIG_FILENAME
    _write_config(cfg_path, cfg)

    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
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


def test_sync_main_raises_when_local_default_diverges_from_origin(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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


def test_create_worktree_allows_existing_branch_without_start_point(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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

    worktree = supervisor.create_worktree(base_repo_id="base", branch="feature/test")
    assert worktree.branch == "feature/test"
    assert worktree.path.exists()
    assert _git_stdout(worktree.path, "rev-parse", "HEAD") == first_sha


def test_create_worktree_fails_if_explicit_start_point_mismatches_existing_branch(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
        base_repo_id="base", branch="feature/setup-ok"
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
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
        supervisor.create_worktree(base_repo_id="base", branch="feature/setup-fail")

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


def test_set_worktree_setup_commands_route_updates_manifest(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    _write_config(hub_root / CONFIG_FILENAME, cfg)

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
