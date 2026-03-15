import concurrent.futures
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import codex_autorunner.core.hub as hub_module
from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.destinations import default_car_docker_container_name
from codex_autorunner.core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.hub import HubSupervisor, RepoStatus
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

pytestmark = pytest.mark.slow


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


def test_run_coroutine_uses_asyncio_run_without_running_loop(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    try:

        async def _value() -> int:
            return 42

        assert supervisor._run_coroutine(_value()) == 42
    finally:
        supervisor.shutdown()


def test_run_coroutine_uses_explicit_loop_when_running_loop_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    supervisor = HubSupervisor(load_hub_config(hub_root))

    class _FakeLoop:
        def __init__(self) -> None:
            self.closed = False
            self.run_called = False

        def run_until_complete(self, coro):  # type: ignore[no-untyped-def]
            self.run_called = True
            coro.close()
            return "fallback-result"

        def close(self) -> None:
            self.closed = True

    fake_loop = _FakeLoop()
    monkeypatch.setattr(hub_module.asyncio, "get_running_loop", lambda: object())
    monkeypatch.setattr(
        hub_module.asyncio,
        "run",
        lambda _coro: (_ for _ in ()).throw(
            AssertionError("asyncio.run should not run")
        ),
    )
    monkeypatch.setattr(hub_module.asyncio, "new_event_loop", lambda: fake_loop)
    try:

        async def _value() -> str:
            return "unused"

        result = supervisor._run_coroutine(_value())
        assert result == "fallback-result"
        assert fake_loop.run_called is True
        assert fake_loop.closed is True
    finally:
        supervisor.shutdown()


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
    assert data["agent_workspaces"] == []


def test_hub_supervisor_can_create_list_and_remove_agent_workspaces(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(load_hub_config(hub_root))
    workspace = supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )
    assert workspace.runtime == "zeroclaw"
    assert workspace.display_name == "ZeroClaw Main"
    assert workspace.path == (
        hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    assert workspace.path.exists()
    assert workspace.resource_kind == "agent_workspace"

    listed = supervisor.list_agent_workspaces(use_cache=False)
    assert [item.id for item in listed] == ["zc-main"]
    assert listed[0].path == workspace.path

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    manifest_workspace = manifest.get_agent_workspace("zc-main")
    assert manifest_workspace is not None
    assert manifest_workspace.path == Path(
        ".codex-autorunner/runtimes/zeroclaw/zc-main"
    )

    state_path = hub_root / ".codex-autorunner" / "hub_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["agent_workspaces"][0]["id"] == "zc-main"
    assert payload["agent_workspaces"][0]["resource_kind"] == "agent_workspace"

    supervisor.remove_agent_workspace("zc-main")
    assert workspace.path.exists() is False
    assert supervisor.list_agent_workspaces(use_cache=False) == []


def test_hub_api_lists_agent_workspaces_as_typed_resources(tmp_path: Path):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200
    payload = response.json()
    assert payload["repos"] == []
    workspace = payload["agent_workspaces"][0]
    assert workspace["id"] == "zc-main"
    assert workspace["runtime"] == "zeroclaw"
    assert workspace["path"] == ".codex-autorunner/runtimes/zeroclaw/zc-main"
    assert workspace["resource_kind"] == "agent_workspace"
    assert workspace["effective_destination"] == {"kind": "local"}


def test_hub_agent_workspace_crud_routes_support_remove_and_delete(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    client = TestClient(create_hub_app(hub_root))

    create_resp = client.post(
        "/hub/agent-workspaces",
        json={
            "id": "zc-main",
            "runtime": "zeroclaw",
            "display_name": "ZeroClaw Main",
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["id"] == "zc-main"
    assert created["runtime"] == "zeroclaw"
    assert created["display_name"] == "ZeroClaw Main"
    workspace_path = (
        hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    assert workspace_path.exists()

    list_resp = client.get("/hub/agent-workspaces")
    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert [item["id"] for item in list_payload["agent_workspaces"]] == ["zc-main"]

    detail_resp = client.get("/hub/agent-workspaces/zc-main")
    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["configured_destination"] is None
    assert detail_payload["source"] == "default"
    assert detail_payload["path"] == ".codex-autorunner/runtimes/zeroclaw/zc-main"

    update_resp = client.patch(
        "/hub/agent-workspaces/zc-main",
        json={"enabled": False},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["enabled"] is False

    destination_resp = client.post(
        "/hub/agent-workspaces/zc-main/destination",
        json={"kind": "docker", "image": "ghcr.io/acme/zeroclaw:latest"},
    )
    assert destination_resp.status_code == 200
    destination_payload = destination_resp.json()
    assert destination_payload["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/zeroclaw:latest",
    }
    assert destination_payload["source"] == "configured"

    remove_resp = client.post("/hub/agent-workspaces/zc-main/remove", json={})
    assert remove_resp.status_code == 200
    assert remove_resp.json() == {
        "status": "ok",
        "workspace_id": "zc-main",
        "delete_dir": False,
    }
    assert workspace_path.exists()
    assert client.get("/hub/agent-workspaces/zc-main").status_code == 404

    recreate_resp = client.post(
        "/hub/agent-workspaces",
        json={"id": "zc-main", "runtime": "zeroclaw"},
    )
    assert recreate_resp.status_code == 200

    delete_resp = client.post("/hub/agent-workspaces/zc-main/delete", json={})
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {
        "status": "ok",
        "workspace_id": "zc-main",
        "delete_dir": True,
    }
    assert not workspace_path.exists()


def test_hub_agent_workspace_job_routes_submit_expected_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    app = create_hub_app(hub_root)
    submissions: list[dict[str, object]] = []

    async def _fake_submit(kind: str, func, *, request_id: Optional[str] = None):
        result = await func()
        submissions.append({"kind": kind, "request_id": request_id, "result": result})

        class _Job:
            def to_dict(self) -> dict[str, object]:
                return {
                    "job_id": f"job-{len(submissions)}",
                    "kind": kind,
                    "status": "succeeded",
                    "created_at": "2026-03-08T00:00:00Z",
                    "started_at": "2026-03-08T00:00:00Z",
                    "finished_at": "2026-03-08T00:00:01Z",
                    "result": result if isinstance(result, dict) else None,
                    "error": None,
                }

        return _Job()

    monkeypatch.setattr(app.state.job_manager, "submit", _fake_submit)

    client = TestClient(app)

    create_resp = client.post(
        "/hub/jobs/agent-workspaces",
        json={"id": "zc-main", "runtime": "zeroclaw"},
    )
    assert create_resp.status_code == 200
    assert create_resp.json()["kind"] == "hub.create_agent_workspace"
    workspace_path = (
        hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    assert workspace_path.exists()

    remove_resp = client.post("/hub/jobs/agent-workspaces/zc-main/remove", json={})
    assert remove_resp.status_code == 200
    assert remove_resp.json()["kind"] == "hub.remove_agent_workspace"
    assert workspace_path.exists()

    recreate_resp = client.post(
        "/hub/jobs/agent-workspaces",
        json={"id": "zc-main", "runtime": "zeroclaw"},
    )
    assert recreate_resp.status_code == 200
    assert recreate_resp.json()["kind"] == "hub.create_agent_workspace"

    delete_resp = client.post("/hub/jobs/agent-workspaces/zc-main/delete", json={})
    assert delete_resp.status_code == 200
    assert delete_resp.json()["kind"] == "hub.delete_agent_workspace"
    assert not workspace_path.exists()

    assert [item["kind"] for item in submissions] == [
        "hub.create_agent_workspace",
        "hub.remove_agent_workspace",
        "hub.create_agent_workspace",
        "hub.delete_agent_workspace",
    ]


@pytest.mark.slow
@pytest.mark.docker_managed_cleanup
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
    with TestClient(app) as client:
        resp = client.get("/hub/repos")
    assert resp.status_code == 200
    data = resp.json()
    base_payload = next(item for item in data["repos"] if item["id"] == "base")
    worktree_payload = next(item for item in data["repos"] if item["id"] == worktree.id)
    expected = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    assert base_payload["effective_destination"] == expected
    assert worktree_payload["effective_destination"] == expected


@pytest.mark.slow
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
    assert worktree_payload["pma_chat_bound_thread_count"] == 1
    assert worktree_payload["non_pma_chat_bound_thread_count"] == 0
    assert worktree_payload["cleanup_blocked_by_chat_binding"] is False


@pytest.mark.slow
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
    assert worktree_payload["pma_chat_bound_thread_count"] == 1
    assert worktree_payload["non_pma_chat_bound_thread_count"] == 0
    assert worktree_payload["cleanup_blocked_by_chat_binding"] is False


@pytest.mark.slow
def test_hub_archive_state_endpoint_archives_and_resets_runtime_state(tmp_path: Path):
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
        branch="feature/archive-state",
        start_point="HEAD",
    )
    worktree_car = worktree.path / ".codex-autorunner"
    (worktree_car / "tickets" / "TICKET-123-demo.md").write_text(
        "demo ticket", encoding="utf-8"
    )
    (worktree_car / "contextspace" / "active_context.md").write_text(
        "active context", encoding="utf-8"
    )
    dispatch_dir = worktree_car / "runs" / "run-1" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    (dispatch_dir / "DISPATCH.md").write_text("dispatch", encoding="utf-8")
    store = PmaThreadStore(hub_root)
    created = store.create_thread("codex", worktree.path, repo_id=worktree.id)

    app = create_hub_app(hub_root)
    client = TestClient(app)

    repos_resp = client.get("/hub/repos")
    assert repos_resp.status_code == 200
    worktree_payload = next(
        item for item in repos_resp.json()["repos"] if item["id"] == worktree.id
    )
    assert worktree_payload["has_car_state"] is True

    archive_resp = client.post(
        "/hub/worktrees/archive-state",
        json={"worktree_repo_id": worktree.id},
    )
    assert archive_resp.status_code == 200
    payload = archive_resp.json()
    assert "tickets" in payload["archived_paths"]
    assert "runs" in payload["archived_paths"]

    snapshot_root = Path(payload["snapshot_path"])
    assert (snapshot_root / "tickets" / "TICKET-123-demo.md").exists()
    assert (snapshot_root / "runs" / "run-1" / "dispatch" / "DISPATCH.md").exists()
    assert (worktree_car / "contextspace" / "active_context.md").read_text(
        encoding="utf-8"
    ) == ""
    assert not (worktree_car / "tickets" / "TICKET-123-demo.md").exists()
    assert not (worktree_car / "runs").exists()

    repos_after_resp = client.get("/hub/repos")
    assert repos_after_resp.status_code == 200
    worktree_after = next(
        item for item in repos_after_resp.json()["repos"] if item["id"] == worktree.id
    )
    assert worktree_after["has_car_state"] is False
    thread = store.get_thread(created["managed_thread_id"])
    assert thread is not None
    assert thread["lifecycle_status"] == "archived"


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


@pytest.mark.slow
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
        json={
            "force": True,
            "force_attestation": "REMOVE base",
            "delete_dir": True,
            "delete_worktrees": True,
        },
    )
    assert remove_resp.status_code == 200
    assert not base.path.exists()
    assert not worktree.path.exists()


def test_hub_remove_repo_route_forwards_force_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    app = create_hub_app(hub_root)
    captured: dict[str, object] = {}

    def _fake_remove_repo(
        repo_id: str,
        *,
        force: bool = False,
        delete_dir: bool = True,
        delete_worktrees: bool = False,
        force_attestation: Optional[dict[str, str]] = None,
    ) -> None:
        captured["repo_id"] = repo_id
        captured["force"] = force
        captured["delete_dir"] = delete_dir
        captured["delete_worktrees"] = delete_worktrees
        captured["force_attestation"] = force_attestation

    monkeypatch.setattr(app.state.hub_supervisor, "remove_repo", _fake_remove_repo)

    client = TestClient(app)
    resp = client.post(
        "/hub/repos/base/remove",
        json={
            "force": True,
            "force_attestation": "REMOVE base",
            "delete_dir": True,
            "delete_worktrees": False,
        },
    )
    assert resp.status_code == 200
    assert captured == {
        "repo_id": "base",
        "force": True,
        "delete_dir": True,
        "delete_worktrees": False,
        "force_attestation": {
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "REMOVE base",
            "target_scope": "hub.remove_repo:base",
        },
    }


def test_hub_repo_job_routes_submit_expected_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    repo_dir = hub_root / "demo#scan"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    app = create_hub_app(hub_root)
    submissions: list[dict[str, object]] = []

    async def _fake_submit(kind: str, func, *, request_id: Optional[str] = None):
        result = await func()
        submissions.append({"kind": kind, "request_id": request_id, "result": result})

        class _Job:
            def to_dict(self) -> dict[str, object]:
                return {
                    "job_id": f"job-{len(submissions)}",
                    "kind": kind,
                    "status": "succeeded",
                    "created_at": "2026-03-08T00:00:00Z",
                    "started_at": "2026-03-08T00:00:00Z",
                    "finished_at": "2026-03-08T00:00:01Z",
                    "result": result if isinstance(result, dict) else None,
                    "error": None,
                }

        return _Job()

    monkeypatch.setattr(app.state.job_manager, "submit", _fake_submit)

    client = TestClient(app)

    scan_resp = client.post("/hub/jobs/scan")
    assert scan_resp.status_code == 200
    assert scan_resp.json()["kind"] == "hub.scan_repos"

    create_resp = client.post("/hub/jobs/repos", json={"id": "base"})
    assert create_resp.status_code == 200
    assert create_resp.json()["kind"] == "hub.create_repo"
    assert (hub_root / "base").exists()

    remove_resp = client.post(
        "/hub/jobs/repos/base/remove",
        json={
            "force": True,
            "force_attestation": "REMOVE base",
            "delete_dir": True,
        },
    )
    assert remove_resp.status_code == 200
    assert remove_resp.json()["kind"] == "hub.remove_repo"
    assert not (hub_root / "base").exists()

    assert [item["kind"] for item in submissions] == [
        "hub.scan_repos",
        "hub.create_repo",
        "hub.remove_repo",
    ]
    assert submissions[0]["result"] == {"status": "ok"}
    assert submissions[1]["result"]["id"] == "base"
    assert submissions[2]["result"] == {"status": "ok"}


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


def test_cleanup_worktree_removes_car_managed_docker_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base_entry = manifest.get("base")
    assert base_entry is not None
    base_entry.destination = {"kind": "docker", "image": "busybox:latest"}
    save_manifest(manifest_path, manifest, hub_root)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/docker-cleanup-managed",
        start_point="HEAD",
    )

    calls: list[list[str]] = []

    def _fake_run_docker(self, args, *, timeout_seconds=None):
        _ = self, timeout_seconds
        args_list = [str(part) for part in args]
        calls.append(args_list)
        if args_list[0] == "inspect":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="true\n",
                stderr="",
            )
        if args_list[0] == "stop":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="stopped\n",
                stderr="",
            )
        if args_list[0] == "rm":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="removed\n",
                stderr="",
            )
        raise AssertionError(f"unexpected docker call: {args_list}")

    monkeypatch.setattr(HubSupervisor, "_run_docker_command", _fake_run_docker)

    result = supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=False)
    assert result["status"] == "ok"
    docker_cleanup = result["docker_cleanup"]
    assert isinstance(docker_cleanup, dict)
    assert docker_cleanup["status"] == "removed"
    expected_name = default_car_docker_container_name(worktree.path.resolve())
    assert docker_cleanup["container_name"] == expected_name
    assert calls == [
        ["inspect", "--format", "{{.State.Running}}", expected_name],
        ["stop", "-t", "10", expected_name],
        ["rm", expected_name],
    ]
    assert not worktree.path.exists()


def test_cleanup_worktree_skips_explicit_docker_container_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base_entry = manifest.get("base")
    assert base_entry is not None
    base_entry.destination = {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "shared-container",
    }
    save_manifest(manifest_path, manifest, hub_root)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/docker-cleanup-explicit",
        start_point="HEAD",
    )

    def _unexpected_run_docker(self, args, *, timeout_seconds=None):
        _ = self, args, timeout_seconds
        raise AssertionError("explicit container_name should not be auto-cleaned")

    monkeypatch.setattr(HubSupervisor, "_run_docker_command", _unexpected_run_docker)

    result = supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=False)
    assert result["status"] == "ok"
    docker_cleanup = result["docker_cleanup"]
    assert isinstance(docker_cleanup, dict)
    assert docker_cleanup["status"] == "skipped_explicit"
    assert docker_cleanup["container_name"] == "shared-container"
    assert "explicit container_name" in str(docker_cleanup["message"])
    assert not worktree.path.exists()


def test_cleanup_worktree_continues_when_docker_cleanup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base_entry = manifest.get("base")
    assert base_entry is not None
    base_entry.destination = {"kind": "docker", "image": "busybox:latest"}
    save_manifest(manifest_path, manifest, hub_root)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/docker-cleanup-error",
        start_point="HEAD",
    )

    def _fake_run_docker(self, args, *, timeout_seconds=None):
        _ = self, timeout_seconds
        args_list = [str(part) for part in args]
        if args_list[0] == "inspect":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="true\n",
                stderr="",
            )
        if args_list[0] == "stop":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="stopped\n",
                stderr="",
            )
        if args_list[0] == "rm":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=1,
                stdout="",
                stderr="docker daemon unavailable",
            )
        raise AssertionError(f"unexpected docker call: {args_list}")

    monkeypatch.setattr(HubSupervisor, "_run_docker_command", _fake_run_docker)

    result = supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=False)
    assert result["status"] == "ok"
    docker_cleanup = result["docker_cleanup"]
    assert isinstance(docker_cleanup, dict)
    assert docker_cleanup["status"] == "error"
    assert "docker rm failed" in str(docker_cleanup["message"])
    assert not worktree.path.exists()


@pytest.mark.docker_managed_cleanup
def test_hub_api_cleanup_worktree_returns_docker_cleanup_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base_entry = manifest.get("base")
    assert base_entry is not None
    base_entry.destination = {"kind": "docker", "image": "busybox:latest"}
    save_manifest(manifest_path, manifest, hub_root)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/docker-cleanup-api",
        start_point="HEAD",
    )

    calls: list[list[str]] = []

    def _fake_run_docker(self, args, *, timeout_seconds=None):
        _ = self, timeout_seconds
        args_list = [str(part) for part in args]
        calls.append(args_list)
        if args_list[0] == "inspect":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="true\n",
                stderr="",
            )
        if args_list[0] == "stop":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="stopped\n",
                stderr="",
            )
        if args_list[0] == "rm":
            return subprocess.CompletedProcess(
                args=args_list,
                returncode=0,
                stdout="removed\n",
                stderr="",
            )
        raise AssertionError(f"unexpected docker call: {args_list}")

    monkeypatch.setattr(HubSupervisor, "_run_docker_command", _fake_run_docker)

    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        resp = client.post(
            "/hub/worktrees/cleanup",
            json={"worktree_repo_id": worktree.id, "archive": False},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["docker_cleanup"]["status"] == "removed"
    expected_name = default_car_docker_container_name(worktree.path.resolve())
    assert payload["docker_cleanup"]["container_name"] == expected_name
    assert calls == [
        ["inspect", "--format", "{{.State.Running}}", expected_name],
        ["stop", "-t", "10", expected_name],
        ["rm", expected_name],
    ]


def test_hub_api_cleanup_worktree_forwards_force_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    app = create_hub_app(hub_root)
    captured: dict[str, object] = {}

    def _fake_cleanup_worktree(
        *,
        worktree_repo_id: str,
        delete_branch: bool = False,
        delete_remote: bool = False,
        archive: bool = True,
        force: bool = False,
        force_archive: bool = False,
        archive_note: Optional[str] = None,
        force_attestation: Optional[dict[str, str]] = None,
    ) -> dict[str, object]:
        captured["worktree_repo_id"] = worktree_repo_id
        captured["delete_branch"] = delete_branch
        captured["delete_remote"] = delete_remote
        captured["archive"] = archive
        captured["force"] = force
        captured["force_archive"] = force_archive
        captured["archive_note"] = archive_note
        captured["force_attestation"] = force_attestation
        return {"status": "ok"}

    monkeypatch.setattr(
        app.state.hub_supervisor, "cleanup_worktree", _fake_cleanup_worktree
    )

    client = TestClient(app)
    resp = client.post(
        "/hub/worktrees/cleanup",
        json={
            "worktree_repo_id": "base--feature",
            "archive": False,
            "force": True,
            "force_archive": False,
            "archive_note": "cleanup",
            "force_attestation": "REMOVE base--feature",
        },
    )
    assert resp.status_code == 200
    assert captured == {
        "worktree_repo_id": "base--feature",
        "delete_branch": False,
        "delete_remote": False,
        "archive": False,
        "force": True,
        "force_archive": False,
        "archive_note": "cleanup",
        "force_attestation": {
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "REMOVE base--feature",
            "target_scope": "hub.worktree.cleanup:base--feature",
        },
    }


def test_cleanup_worktree_allows_pma_only_bound_without_force(tmp_path: Path):
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
    created = store.create_thread("codex", worktree.path, repo_id=worktree.id)

    supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True)
    assert not worktree.path.exists()
    thread = store.get_thread(created["managed_thread_id"])
    assert thread is not None
    assert thread["lifecycle_status"] == "archived"


def test_archive_worktree_archives_bound_pma_threads(tmp_path: Path):
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
        branch="feature/archive-pma-threads",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    repo_bound = store.create_thread("codex", worktree.path, repo_id=worktree.id)
    workspace_bound = store.create_thread("opencode", worktree.path)
    other = store.create_thread("codex", base.path, repo_id=base.id)

    payload = supervisor.archive_worktree(worktree_repo_id=worktree.id)

    assert payload["status"] in {"complete", "partial"}
    archived_repo_bound = store.get_thread(repo_bound["managed_thread_id"])
    archived_workspace_bound = store.get_thread(workspace_bound["managed_thread_id"])
    untouched = store.get_thread(other["managed_thread_id"])
    assert archived_repo_bound is not None
    assert archived_repo_bound["lifecycle_status"] == "archived"
    assert archived_workspace_bound is not None
    assert archived_workspace_bound["lifecycle_status"] == "archived"
    assert untouched is not None
    assert untouched["lifecycle_status"] == "active"


def test_cleanup_worktree_allows_mixed_chat_bound_with_force(tmp_path: Path):
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
    _write_discord_binding(
        hub_root, channel_id="discord-chan-force", repo_id=worktree.id
    )

    supervisor.cleanup_worktree(
        worktree_repo_id=worktree.id,
        archive=True,
        force=True,
        force_attestation={
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "cleanup mixed chat-bound worktree",
            "target_scope": f"hub.worktree.cleanup:{worktree.id}",
        },
    )
    assert not worktree.path.exists()


def test_cleanup_worktree_rejects_mixed_chat_bound_without_force(tmp_path: Path):
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
        branch="feature/chat-guard-mixed",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    store.create_thread("codex", worktree.path, repo_id=worktree.id)
    _write_discord_binding(
        hub_root, channel_id="discord-chan-mixed", repo_id=worktree.id
    )

    with pytest.raises(
        ValueError,
        match="Refusing to clean up chat-bound worktree",
    ):
        supervisor.cleanup_worktree(worktree_repo_id=worktree.id, archive=True)

    assert worktree.path.exists()


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

    supervisor.cleanup_worktree(
        worktree_repo_id=worktree.id,
        archive=True,
        force=True,
        force_attestation={
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "cleanup chat-bound worktree",
            "target_scope": f"hub.worktree.cleanup:{worktree.id}",
        },
    )
    assert not worktree.path.exists()


def test_cleanup_worktree_force_requires_attestation(tmp_path: Path):
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
        branch="feature/force-attestation-required",
        start_point="HEAD",
    )

    with pytest.raises(
        ValueError,
        match="--force requires --force-attestation for dangerous actions.",
    ):
        supervisor.cleanup_worktree(
            worktree_repo_id=worktree.id,
            archive=True,
            force=True,
        )

    assert worktree.path.exists()


@pytest.mark.slow
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
    assert worktree_payload["discord_chat_bound_thread_count"] == 1
    assert worktree_payload["non_pma_chat_bound_thread_count"] == 1
    assert worktree_payload["cleanup_blocked_by_chat_binding"] is True


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
