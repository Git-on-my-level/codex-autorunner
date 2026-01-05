from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.server import create_app, create_hub_app


def test_static_assets_served_with_base_path(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app)
    res = client.get("/car/static/styles.css")
    assert res.status_code == 200
    assert "body" in res.text
    js_res = client.get("/car/static/app.js")
    assert js_res.status_code == 200


def test_repo_root_trailing_slash_does_not_redirect(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app, follow_redirects=False)
    res = client.get("/car/repos/example-repo/")
    assert res.status_code != 308


def test_static_redirects_to_base_path(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app, follow_redirects=False)
    res = client.get("/static/app.js")
    assert res.status_code == 308
    assert res.headers.get("location") == "/car/static/app.js"


def test_root_path_proxy_serves_static(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app, root_path="/car", follow_redirects=False)
    res = client.get("/static/app.js")
    assert res.status_code == 200


def test_repo_mode_static_assets_served_with_base_path(tmp_path: Path) -> None:
    seed_repo_files(tmp_path, force=True, git_required=False)
    app = create_app(tmp_path, base_path="/car")
    client = TestClient(app)
    res = client.get("/car/static/app.js")
    assert res.status_code == 200
