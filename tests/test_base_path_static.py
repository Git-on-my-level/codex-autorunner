from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app


def test_static_assets_served_with_base_path(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app)
    res = client.get("/car/static/styles.css")
    assert res.status_code == 200
    assert "body" in res.text


def test_repo_root_trailing_slash_does_not_redirect(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    app = create_hub_app(tmp_path, base_path="/car")
    client = TestClient(app, follow_redirects=False)
    res = client.get("/car/repos/example-repo/")
    assert res.status_code != 308
