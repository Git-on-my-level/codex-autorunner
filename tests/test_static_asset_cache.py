import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web import static_assets
from codex_autorunner.surfaces.web.static_assets import StaticAssetProvenance


def _write_required_assets(static_dir: Path) -> None:
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(
        "<html>__CAR_ASSET_VERSION__</html>", encoding="utf-8"
    )
    (static_dir / "styles.css").write_text("body { }", encoding="utf-8")
    generated_dir = static_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "bootstrap.js").write_text(
        "console.log('bootstrap');", encoding="utf-8"
    )
    (generated_dir / "loader.js").write_text("console.log('loader');", encoding="utf-8")
    (generated_dir / "app.js").write_text("console.log('app');", encoding="utf-8")
    (generated_dir / "github.js").write_text("console.log('github');", encoding="utf-8")
    vendor_dir = static_dir / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / "xterm.js").write_text("console.log('xterm');", encoding="utf-8")
    (vendor_dir / "xterm-addon-fit.js").write_text(
        "console.log('fit');", encoding="utf-8"
    )
    (vendor_dir / "xterm.css").write_text("body { }", encoding="utf-8")
    manifest = {
        "version": "test",
        "generated": [
            {"path": "generated/bootstrap.js", "hash": "test"},
            {"path": "generated/loader.js", "hash": "test"},
            {"path": "generated/app.js", "hash": "test"},
            {"path": "generated/github.js", "hash": "test"},
        ],
        "manual": [
            {"path": "index.html", "hash": "test"},
            {"path": "styles.css", "hash": "test"},
            {"path": "vendor/xterm.js", "hash": "test"},
            {"path": "vendor/xterm-addon-fit.js", "hash": "test"},
            {"path": "vendor/xterm.css", "hash": "test"},
        ],
    }
    (static_dir / "assets.json").write_text(json.dumps(manifest), encoding="utf-8")


@dataclass(frozen=True)
class _StaticHubEnv:
    hub_root: Path
    repo_id: str
    repo_root: Path


@pytest.fixture(scope="module")
def _static_hub_env(tmp_path_factory):
    hub_root = tmp_path_factory.mktemp("hub")
    seed_hub_files(hub_root, force=True)
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    yield _StaticHubEnv(hub_root=hub_root, repo_id=repo_id, repo_root=repo_root)


def test_materialize_static_assets_survives_source_removal(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    logger = logging.getLogger("test-static-assets")
    cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
    cache_dir, cache_context, provenance = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=5,
        max_cache_age_days=30,
        logger=logger,
    )
    assert cache_context is None
    assert provenance == StaticAssetProvenance.SOURCE_MATERIALIZE
    assert cache_dir.exists()
    assert cache_dir.parent == cache_root
    assert not any(cache_root.glob(".lock-*"))
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
    cache_dir, cache_context, provenance = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=5,
        max_cache_age_days=30,
        logger=logger,
    )
    assert cache_context is None
    assert cache_dir == existing_cache
    assert provenance == StaticAssetProvenance.EXISTING_CACHE_FALLBACK


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
    cache_dir, cache_context, provenance = static_assets.materialize_static_assets(
        cache_root,
        max_cache_entries=1,
        max_cache_age_days=None,
        logger=logger,
    )
    assert cache_context is None
    assert provenance == StaticAssetProvenance.SOURCE_MATERIALIZE
    entries = [
        path
        for path in cache_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert cache_dir in entries
    assert len(entries) <= 1


def test_repo_app_serves_cached_static_assets(
    _static_hub_env, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(_static_hub_env.hub_root)
    client = TestClient(app)
    shutil.rmtree(source_dir)
    res = client.get(f"/repos/{_static_hub_env.repo_id}/")
    assert res.status_code == 200
    static_res = client.get(f"/repos/{_static_hub_env.repo_id}/static/generated/app.js")
    assert static_res.status_code == 200


def test_static_assets_cached_and_compressed(
    _static_hub_env, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source_static"
    _write_required_assets(source_dir)
    (source_dir / "generated" / "big.js").write_text("a" * 2048, encoding="utf-8")
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(_static_hub_env.hub_root)
    client = TestClient(app)
    cache_res = client.get(f"/repos/{_static_hub_env.repo_id}/static/generated/app.js")
    assert cache_res.status_code == 200
    cache_control = cache_res.headers.get("Cache-Control", "")
    assert "must-revalidate" in cache_control
    assert "max-age=0" in cache_control
    assert "no-store" in cache_control
    vendor_res = client.get(f"/repos/{_static_hub_env.repo_id}/static/vendor/xterm.js")
    assert vendor_res.status_code == 200
    assert "max-age=31536000" in vendor_res.headers.get("Cache-Control", "")
    gzip_res = client.get(
        f"/repos/{_static_hub_env.repo_id}/static/generated/big.js",
        headers={"Accept-Encoding": "gzip"},
    )
    assert gzip_res.status_code == 200
    assert gzip_res.headers.get("Content-Encoding") == "gzip"


def test_repo_app_falls_back_to_hub_static_cache(
    _static_hub_env, tmp_path: Path, monkeypatch
) -> None:
    hub_config = load_hub_config(_static_hub_env.hub_root)
    hub_cache_root = hub_config.static_assets.cache_root
    existing_cache = hub_cache_root / "existing"
    _write_required_assets(existing_cache)
    source_dir = tmp_path / "missing_source"
    monkeypatch.setattr(static_assets, "resolve_static_dir", lambda: (source_dir, None))
    app = create_hub_app(_static_hub_env.hub_root)
    client = TestClient(app)
    res = client.get(f"/repos/{_static_hub_env.repo_id}/static/generated/app.js")
    assert res.status_code == 200


class TestStaticAssetCacheProvenance:
    def test_fresh_materialize_from_source(self, tmp_path: Path, monkeypatch) -> None:
        source_dir = tmp_path / "source_static"
        _write_required_assets(source_dir)
        monkeypatch.setattr(
            static_assets, "resolve_static_dir", lambda: (source_dir, None)
        )
        cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
        logger = logging.getLogger("test-fresh")

        cache_dir, cache_context, provenance = static_assets.materialize_static_assets(
            cache_root,
            max_cache_entries=5,
            max_cache_age_days=30,
            logger=logger,
        )
        assert cache_context is None
        assert cache_dir.exists()
        assert static_assets.missing_static_assets(cache_dir) == []
        assert provenance == StaticAssetProvenance.SOURCE_MATERIALIZE
        assert not any(cache_root.glob(".lock-*"))
        version = static_assets.asset_version(cache_dir)
        assert version
        assert version != "0"

    def test_cache_reuse_when_source_missing(self, tmp_path: Path, monkeypatch) -> None:
        cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
        existing = cache_root / "prior-cache"
        _write_required_assets(existing)
        source_dir = tmp_path / "missing_source"
        monkeypatch.setattr(
            static_assets, "resolve_static_dir", lambda: (source_dir, None)
        )
        logger = logging.getLogger("test-reuse")

        cache_dir, cache_context, provenance = static_assets.materialize_static_assets(
            cache_root,
            max_cache_entries=5,
            max_cache_age_days=30,
            logger=logger,
        )
        assert cache_dir == existing
        assert cache_context is None
        assert provenance == StaticAssetProvenance.EXISTING_CACHE_FALLBACK

    def test_hard_fail_when_no_source_and_no_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_dir = tmp_path / "missing_source"
        monkeypatch.setattr(
            static_assets, "resolve_static_dir", lambda: (source_dir, None)
        )
        cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
        logger = logging.getLogger("test-hard-fail")

        with pytest.raises(RuntimeError, match="Static UI assets missing"):
            static_assets.materialize_static_assets(
                cache_root,
                max_cache_entries=5,
                max_cache_age_days=30,
                logger=logger,
            )

    def test_fingerprint_cache_hit_avoids_recopy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source_dir = tmp_path / "source_static"
        _write_required_assets(source_dir)
        monkeypatch.setattr(
            static_assets, "resolve_static_dir", lambda: (source_dir, None)
        )
        cache_root = tmp_path / "repo_root" / ".codex-autorunner" / "static-cache"
        logger = logging.getLogger("test-fingerprint")

        first_dir, _, first_provenance = static_assets.materialize_static_assets(
            cache_root,
            max_cache_entries=5,
            max_cache_age_days=30,
            logger=logger,
        )
        second_dir, _, second_provenance = static_assets.materialize_static_assets(
            cache_root,
            max_cache_entries=5,
            max_cache_age_days=30,
            logger=logger,
        )

        assert first_dir == second_dir
        assert first_provenance == StaticAssetProvenance.SOURCE_MATERIALIZE
        assert second_provenance == StaticAssetProvenance.FINGERPRINT_CACHE_HIT
        assert not any(cache_root.glob(".lock-*"))

    def test_missing_static_assets_checks_manifest(self, tmp_path: Path) -> None:

        static_dir = tmp_path / "with_manifest"
        _write_required_assets(static_dir)
        assert static_assets.missing_static_assets(static_dir) == []

        (static_dir / "generated" / "app.js").unlink()
        missing = static_assets.missing_static_assets(static_dir)
        assert "generated/app.js" in missing

    def test_missing_static_assets_falls_back_to_required_list(
        self, tmp_path: Path
    ) -> None:
        static_dir = tmp_path / "no_manifest"
        static_dir.mkdir()
        missing = static_assets.missing_static_assets(static_dir)
        assert len(missing) > 0
        assert "index.html" in missing


class TestResolveStaticDirFallback:
    def test_resolve_returns_path_and_optional_context(self, monkeypatch) -> None:
        static_dir, context = static_assets.resolve_static_dir()
        assert isinstance(static_dir, Path)
        assert context is None or hasattr(context, "__enter__")

    def test_fallback_path_points_to_package_adjacent_static(
        self, monkeypatch, tmp_path
    ) -> None:
        source_dir = tmp_path / "nonexistent_source"
        monkeypatch.setattr(
            static_assets,
            "resolve_static_dir",
            lambda: (source_dir, None),
        )
        result_dir, result_ctx = static_assets.resolve_static_dir()
        assert result_dir == source_dir
        assert result_ctx is None
