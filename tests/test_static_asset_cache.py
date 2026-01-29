import logging
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.config import load_hub_config
from codex_autorunner.server import create_hub_app
from codex_autorunner.web import static_assets


def _write_required_assets(static_dir: Path) -> None:
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(
        "<html>__CAR_ASSET_VERSION__</html>", encoding="utf-8"
    )
    (static_dir / "bootstrap.js").write_text(
        "console.log('bootstrap');", encoding="utf-8"
    )
    (static_dir / "loader.js").write_text("console.log('loader');", encoding="utf-8")
    (static_dir / "styles.css").write_text("body { }", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('app');", encoding="utf-8")
    (static_dir / "github.js").write_text("console.log('github');", encoding="utf-8")
    vendor_dir = static_dir / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / "xterm.js").write_text("console.log('xterm');", encoding="utf-8")
    (vendor_dir / "xterm-addon-fit.js").write_text(
        "console.log('fit');", encoding="utf-8"
    )
    (vendor_dir / "xterm.css").write_text("body { }", encoding="utf-8")


def test_materialize_static_assets_survives_source_removal(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    logger = logging.getLogger("test-static-assets")
    cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
    cache_dir, cache_context = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=5,
        max_cache_age_days=30,
        logger=logger,
    )
    assert cache_context is None
    assert cache_dir.exists()
    assert cache_dir.parent == cache_root
    shutil.rmtree(source_dir)
    assert static_assets.missing_static_assets(cache_dir) == []


def test_materialize_static_assets_falls_back_to_existing_cache(
    tmp_path: Path, monkeypatch
) -> None:
    cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
    existing_cache = cache_root / "existing"
    _write_required_assets(existing_cache)
    source_dir = tmp_path / "missing_source"
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    logger = logging.getLogger("test-static-assets")
    cache_dir, cache_context = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=5,
        max_cache_age_days=30,
        logger=logger,
    )
    assert cache_context is None
    assert cache_dir == existing_cache


def test_materialize_static_assets_hard_fails_without_cache(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "missing_source"
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    logger = logging.getLogger("test-static-assets")
    cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
    with pytest.raises(RuntimeError):
        static_assets.materialize_static_assets(
            cache_root,
            max_cache_entries=5,
            max_cache_age_days=30,
            logger=logger,
        )


def test_materialize_static_assets_prunes_old_entries(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
    old_cache = cache_root / "old"
    older_cache = cache_root / "older"
    _write_required_assets(old_cache)
    _write_required_assets(older_cache)
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    logger = logging.getLogger("test-static-assets")
    cache_dir, cache_context = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=1,
        max_cache_age_days=None,
        logger=logger,
    )
    assert cache_context is None
    entries = [
        path
        for path in cache_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert cache_dir in entries
    assert len(entries) <= 1


def test_repo_app_serves_cached_static_assets(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    shutil.rmtree(source_dir)
    res = client.get(f"/repos/{hub_env.repo_id}/")
    assert res.status_code == 200
    static_res = client.get(f"/repos/{hub_env.repo_id}/static/app.js")
    assert static_res.status_code == 200


def test_static_assets_cached_and_compressed(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    (source_dir / "big.js").write_text("a" * 2048, encoding="utf-8")
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    cache_res = client.get(f"/repos/{hub_env.repo_id}/static/app.js")
    assert cache_res.status_code == 200
    cache_control = cache_res.headers.get("Cache-Control", "")
    assert "max-age=31536000" in cache_control
    gzip_res = client.get(
        f"/repos/{hub_env.repo_id}/static/big.js", headers={"Accept-Encoding": "gzip"}
    )
    assert gzip_res.status_code == 200
    assert gzip_res.headers.get("Content-Encoding") == "gzip"


def test_repo_app_falls_back_to_hub_static_cache(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    hub_config = load_hub_config(hub_env.hub_root)
    hub_cache_root = hub_config.static_assets.cache_root
    existing_cache = hub_cache_root / "existing"
    _write_required_assets(existing_cache)
    source_dir = tmp_path / "missing_source"
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    res = client.get(f"/repos/{hub_env.repo_id}/static/app.js")
    assert res.status_code == 200
