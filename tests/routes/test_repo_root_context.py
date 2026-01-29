from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.utils import find_repo_root
from codex_autorunner.web.app import create_repo_app


def test_repo_root_context_applied_when_cwd_differs(tmp_path, monkeypatch):
    seed_hub_files(tmp_path, force=True)
    repo_root = tmp_path / "repo"
    seed_repo_files(repo_root, force=True, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    app: FastAPI = create_repo_app(repo_root)

    @app.get("/root-check")
    def root_check():
        return {"root": str(find_repo_root())}

    # Ensure the test route is matched before the catch-all UI route.
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app) as client:
        resp = client.get("/root-check")
        assert resp.status_code == 200
        assert Path(resp.json()["root"]) == repo_root
