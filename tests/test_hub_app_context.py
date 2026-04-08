import asyncio
import json
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.app_server_command import GLOBAL_APP_SERVER_COMMAND_ENV
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.core.hub_diagnostics import (
    hub_clean_shutdown_path,
    hub_endpoint_path,
    hub_pid_path,
)
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from codex_autorunner.manifest import load_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web import app as web_app_module
from codex_autorunner.surfaces.web import app_state as web_app_state_module
from tests.conftest import write_test_config


def _stub_opencode_supervisor(monkeypatch) -> None:
    monkeypatch.setattr(
        web_app_state_module,
        "build_opencode_supervisor_from_repo_config",
        lambda *args, **kwargs: object(),
    )


def test_hub_app_state_includes_pma_context(hub_env, monkeypatch) -> None:
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(hub_env.hub_root)

    assert hasattr(app.state, "app_server_threads")
    assert isinstance(app.state.app_server_threads, AppServerThreadRegistry)
    assert app.state.app_server_threads.path == default_app_server_threads_path(
        Path(hub_env.hub_root)
    )

    assert hasattr(app.state, "app_server_events")
    assert isinstance(app.state.app_server_events, AppServerEventBuffer)

    assert hasattr(app.state, "opencode_supervisor")
    assert hasattr(app.state, "opencode_prune_interval")


def test_hub_dev_mode_includes_root_repo_for_source_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "car-src"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    (hub_root / ".git").mkdir()
    (hub_root / "src" / "codex_autorunner").mkdir(parents=True)
    (hub_root / "src" / "codex_autorunner" / "__init__.py").write_text(
        "__version__ = 'test'\n", encoding="utf-8"
    )
    (hub_root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    (hub_root / "pyproject.toml").write_text(
        '[project]\nname = "codex-autorunner"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("CAR_DEV_INCLUDE_ROOT_REPO", "1")
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(hub_root)

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    assert len(manifest.repos) == 1
    assert manifest.repos[0].path == Path(".")
    assert app.state.config.include_root_repo is True


def test_hub_lifespan_reaper_uses_config_root(hub_env, monkeypatch) -> None:
    called_roots: list[Path] = []

    def _fake_reap(root: Path):
        called_roots.append(root)
        return SimpleNamespace(killed=0, removed=0, skipped=0)

    monkeypatch.setattr(web_app_module, "reap_managed_processes", _fake_reap)
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(hub_env.hub_root)
    with TestClient(app):
        pass

    assert called_roots == [app.state.config.root]


def test_hub_lifespan_records_pid_and_clean_shutdown(hub_env, monkeypatch) -> None:
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(hub_env.hub_root)
    pid_path = hub_pid_path(hub_env.hub_root)
    clean_shutdown_path = hub_clean_shutdown_path(hub_env.hub_root)

    with TestClient(app):
        assert pid_path.exists()
        assert pid_path.read_text(encoding="utf-8").strip().isdigit()
        assert clean_shutdown_path.exists() is False

    assert clean_shutdown_path.exists()
    assert clean_shutdown_path.read_text(encoding="utf-8").strip()


def test_hub_lifespan_writes_and_cleans_endpoint_file(hub_env, monkeypatch) -> None:
    _stub_opencode_supervisor(monkeypatch)
    app = create_hub_app(
        hub_env.hub_root,
        base_path="/car",
        endpoint_host="127.0.0.1",
        endpoint_port=4517,
    )
    endpoint_path = hub_endpoint_path(hub_env.hub_root)

    with TestClient(app):
        assert endpoint_path.exists()
        assert endpoint_path.read_text(encoding="utf-8").strip()

    assert endpoint_path.exists() is False


def test_hub_lifespan_clears_stale_endpoint_without_bind_metadata(
    hub_env, monkeypatch
) -> None:
    _stub_opencode_supervisor(monkeypatch)
    endpoint_path = hub_endpoint_path(hub_env.hub_root)
    endpoint_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint_path.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:4517/car",
                "host": "127.0.0.1",
                "port": 4517,
                "base_path": "/car",
            }
        ),
        encoding="utf-8",
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app):
        assert endpoint_path.exists() is False

    assert endpoint_path.exists() is False


@pytest.mark.asyncio
async def test_hub_lifespan_restores_exception_hooks_on_startup_failure(
    hub_env, monkeypatch
) -> None:
    _stub_opencode_supervisor(monkeypatch)
    original_sys_excepthook = sys.excepthook
    original_threading_excepthook = threading.excepthook
    loop = asyncio.get_running_loop()
    original_asyncio_exception_handler = loop.get_exception_handler()

    def _boom_record_hub_startup(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_app_module, "record_hub_startup", _boom_record_hub_startup)
    app = create_hub_app(hub_env.hub_root)

    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            pass

    assert sys.excepthook is original_sys_excepthook
    assert threading.excepthook is original_threading_excepthook
    assert loop.get_exception_handler() is original_asyncio_exception_handler


@pytest.mark.parametrize(
    ("repo_defaults", "expected_interval"),
    [
        ({}, web_app_module._DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS),
        (
            {"flow_retention": {"sweep_interval_seconds": 300}},
            300.0,
        ),
        (
            {"flow_retention": {"sweep_interval_seconds": "abc"}},
            web_app_module._DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS,
        ),
        (
            {"flow_retention": {"sweep_interval_seconds": 0}},
            web_app_module._DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS,
        ),
        (
            {"flow_retention": {"sweep_interval_seconds": -30}},
            web_app_module._DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS,
        ),
        (
            {"flow_retention": {"sweep_interval_seconds": True}},
            web_app_module._DEFAULT_HUB_FLOW_SWEEP_INTERVAL_SECONDS,
        ),
    ],
)
def test_resolve_hub_flow_sweep_interval_seconds_sanitizes_repo_defaults(
    repo_defaults, expected_interval
) -> None:
    interval = web_app_module._resolve_hub_flow_sweep_interval_seconds(
        repo_defaults,
        logging.getLogger("test.hub.flow_sweep"),
    )

    assert interval == expected_interval


def test_hub_lifespan_ignores_invalid_repo_default_flow_sweep_interval(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "flow_retention": {"sweep_interval_seconds": "abc"},
            },
        },
    )
    _stub_opencode_supervisor(monkeypatch)

    app = create_hub_app(hub_root)

    with TestClient(app):
        pass


def test_hub_opencode_prune_interval_uses_opencode_ttl(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "app_server": {"idle_ttl_seconds": 120},
            "opencode": {"idle_ttl_seconds": 240},
        },
    )

    monkeypatch.setattr(
        web_app_state_module,
        "build_opencode_supervisor_from_repo_config",
        lambda *args, **kwargs: object(),
    )

    app = create_hub_app(hub_root)

    assert app.state.opencode_prune_interval == 120.0


def test_hub_repo_apps_build_lazily(hub_env, monkeypatch) -> None:
    _stub_opencode_supervisor(monkeypatch)
    build_calls: list[Path] = []

    def _fake_create_repo_app(repo_root, server_overrides=None, hub_config=None):
        repo_path = Path(repo_root)
        build_calls.append(repo_path)
        app = FastAPI()

        @app.get("/api/version")
        def version():
            return {"repo": repo_path.name}

        return app

    monkeypatch.setattr(web_app_module, "create_repo_app", _fake_create_repo_app)

    app = create_hub_app(hub_env.hub_root)
    assert build_calls == []

    with TestClient(app) as client:
        assert build_calls == []
        response = client.get(f"/repos/{hub_env.repo_id}/api/version")
        assert response.status_code == 200
        assert response.json() == {"repo": hub_env.repo_root.name}

    assert build_calls == [hub_env.repo_root]


def test_build_app_server_supervisor_prefers_global_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    monkeypatch.setenv(GLOBAL_APP_SERVER_COMMAND_ENV, "/env/codex app-server --web")
    config = web_app_state_module.load_hub_config(hub_root)

    captured: dict[str, object] = {}

    class _FakeSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = list(command)

    monkeypatch.setattr(
        web_app_state_module, "WorkspaceAppServerSupervisor", _FakeSupervisor
    )

    supervisor, _interval = web_app_state_module._build_app_server_supervisor(
        config.app_server,
        logger=logging.getLogger("test.hub.app_server"),
        event_prefix="hub",
    )

    assert supervisor is not None
    assert captured["command"] == ["/env/codex", "app-server", "--web"]
