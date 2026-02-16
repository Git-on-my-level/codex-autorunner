from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.manifest import load_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web import app as web_app_module


def test_hub_app_state_includes_pma_context(hub_env) -> None:
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
    app = create_hub_app(hub_env.hub_root)
    with TestClient(app):
        pass

    assert called_roots == [app.state.config.root]
