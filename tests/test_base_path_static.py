import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def _base_path_app(tmp_path_factory):
    hub_root = tmp_path_factory.mktemp("hub")
    seed_hub_files(hub_root, force=True)
    repo_dir = hub_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_dir, force=True, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_dir, repo_id="demo", display_name="demo")
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    yield create_hub_app(hub_root, base_path="/car")


def test_static_assets_served_with_base_path(_base_path_app) -> None:
    client = TestClient(_base_path_app)
    res = client.get("/car/static/styles.css")
    assert res.status_code == 200
    assert "body" in res.text
    js_res = client.get("/car/static/generated/app.js")
    assert js_res.status_code == 200


def test_repo_root_trailing_slash_does_not_redirect(_base_path_app) -> None:
    client = TestClient(_base_path_app, follow_redirects=False)
    res = client.get("/car/repos/example-repo/")
    assert res.status_code != 308


def test_static_redirects_to_base_path(_base_path_app) -> None:
    client = TestClient(_base_path_app, follow_redirects=False)
    res = client.get("/static/generated/app.js")
    assert res.status_code == 308
    assert res.headers.get("location") == "/car/static/generated/app.js"


def test_root_path_proxy_serves_static(_base_path_app) -> None:
    client = TestClient(_base_path_app, root_path="/car", follow_redirects=False)
    res = client.get("/static/generated/app.js")
    assert res.status_code == 200


def test_hub_routes_under_base_path(_base_path_app) -> None:
    client = TestClient(_base_path_app)
    assert client.get("/car/hub/version").status_code == 200
    assert client.get("/car/repos/demo/api/version").status_code == 200
    assert client.get("/car/repos/demo/static/generated/app.js").status_code == 200
