import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.app_server_command import GLOBAL_APP_SERVER_COMMAND_ENV
from codex_autorunner.core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
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
