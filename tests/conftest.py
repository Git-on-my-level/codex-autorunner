"""Test harness configuration.

This repo uses a `src/` layout. In some developer environments an older
installed `codex_autorunner` package can shadow the local sources.

Ensure tests always import the in-repo code.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS = 120
os.environ.setdefault("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS", "1")


_ORIGINAL_UNRAISABLE_HOOK = sys.unraisablehook


def _silence_event_loop_closed_unraisable(unraisable: sys.UnraisableHookArgs) -> None:
    exc = unraisable.exc_value
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        # Suppress noisy asyncio transport __del__ warnings that can surface when
        # cancelling restart tasks during teardown.
        return
    _ORIGINAL_UNRAISABLE_HOOK(unraisable)


sys.unraisablehook = _silence_event_loop_closed_unraisable


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """
    Apply a default per-test timeout to non-integration tests.

    This relies on `pytest-timeout` when installed; if it isn't installed, the
    marker is inert but still documents the intent.
    """
    _ = session, config
    for item in items:
        if item.get_closest_marker("integration") is not None:
            continue
        item.add_marker(pytest.mark.timeout(DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_codex_app_server_clients() -> None:
    """
    Ensure any CodexAppServerClient restart tasks are cancelled after the suite.

    Some tests intentionally crash the app-server to exercise auto-restart
    behavior; if an instance slips through without an explicit close, the
    pending restart task can emit \"Task was destroyed\" noise when the event
    loop shuts down. Running this cleanup keeps `make check` quiet for agents.
    """
    yield
    # Import lazily to avoid impacting non-app-server test collection time.
    import anyio

    from codex_autorunner.integrations.app_server.client import _close_all_clients

    anyio.run(_close_all_clients)


@pytest.fixture(autouse=True)
async def _cleanup_codex_app_server_clients_per_test() -> None:
    """
    Per-test cleanup so pending restart tasks are cancelled before the event loop
    for an async test tears down (avoids \"Task was destroyed\" noise).
    """
    yield
    from codex_autorunner.integrations.app_server.client import _close_all_clients

    await _close_all_clients()
    pending_restart_tasks = [
        t
        for t in asyncio.all_tasks()
        if t.get_coro().__qualname__.endswith(
            "CodexAppServerClient._restart_after_disconnect"
        )
    ]
    for t in pending_restart_tasks:
        t.cancel()
    if pending_restart_tasks:
        await asyncio.gather(*pending_restart_tasks, return_exceptions=True)


@pytest.fixture()
def hub_env(tmp_path: Path):
    """Create a minimal hub with a single initialized repo mounted under `/repos/<id>`."""

    # Import lazily so `pytest_configure()` can prepend the local src/ directory
    # before any `codex_autorunner` modules are loaded.
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.manifest import load_manifest, save_manifest

    @dataclass(frozen=True)
    class HubEnv:
        hub_root: Path
        repo_id: str
        repo_root: Path

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    # Put the repo under the hub's default repos_root (worktrees/ by default).
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)

    return HubEnv(hub_root=hub_root, repo_id=repo_id, repo_root=repo_root)


@pytest.fixture()
def repo(hub_env) -> Path:
    """Backwards-compatible repo fixture (the hub's single test repo root)."""
    return hub_env.repo_root


@pytest.fixture()
def hub_root_only(tmp_path: Path) -> Path:
    """Create a minimal hub without any repos, for testing server-dependent commands."""
    from codex_autorunner.bootstrap import seed_hub_files

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    return hub_root


@pytest.fixture()
def hub_server(hub_root_only: Path):
    """Create a hub with a TestClient for testing server-dependent CLI commands."""
    import socket
    import threading
    import time

    from codex_autorunner.server import create_hub_app

    # Create the hub app
    app = create_hub_app(hub_root_only)

    # Find an available port
    def find_available_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    port = find_available_port()

    # Run the app in a thread
    import uvicorn

    server_thread = None
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    def run_server():
        import asyncio

        asyncio.run(server.serve())

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    max_wait = 5
    for _i in range(max_wait * 10):
        try:
            import httpx

            response = httpx.get(f"http://127.0.0.1:{port}/hub/repos", timeout=0.1)
            if response.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("Server did not start in time")

    # Update hub config to use the dynamic port
    import yaml

    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.core.utils import atomic_write

    config = load_hub_config(hub_root_only)
    config_raw = config.raw if isinstance(config.raw, dict) else {}
    config_raw["server"] = config_raw.get("server", {})
    config_raw["server"]["port"] = port
    config_raw["server"]["host"] = "127.0.0.1"

    config_path = hub_root_only / "codex-autorunner.yml"
    atomic_write(config_path, yaml.safe_dump(config_raw, sort_keys=False))

    try:
        yield hub_root_only, port
    finally:
        # Cleanup is handled by the daemon thread
        pass
