from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app


def test_pma_top_level_routes_serve_new_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    for path in (
        "/pma",
        "/dashboard",
        "/repos",
        "/repos/example",
        "/worktrees",
        "/worktrees/example",
        "/tickets",
        "/tickets/TICKET-100",
        "/contextspace/local",
        "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>PMA Hub</title>" in response.text
        assert "/_app/immutable/entry/app." in response.text


def test_pma_static_assets_are_served_separately_from_legacy_static(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    page = client.get("/pma")
    asset_path = page.text.split('href="/_app/', 1)[1].split('"', 1)[0]
    asset_response = client.get(f"/_app/{asset_path}")

    assert asset_response.status_code == 200
    assert "max-age=31536000" in asset_response.headers.get("Cache-Control", "")
    assert client.get("/legacy").status_code == 200


def test_pma_base_path_routes_redirect_and_serve_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(
        create_hub_app(hub_root, base_path="/car"), follow_redirects=False
    )

    assert client.get("/").headers["location"] == "/car/"
    assert client.get("/pma").headers["location"] == "/car/pma"
    assert (
        client.get("/worktrees/example").headers["location"] == "/car/worktrees/example"
    )
    assert (
        client.get("/contextspace/local").headers["location"]
        == "/car/contextspace/local"
    )

    response = client.get("/car/contextspace/local")
    assert response.status_code == 200
    assert "<title>PMA Hub</title>" in response.text


def test_repo_mount_frontend_routes_are_legacy_gated(hub_env):
    client = TestClient(create_hub_app(hub_env.hub_root), follow_redirects=False)
    repo_id = hub_env.repo_id

    primary = client.get(f"/repos/{repo_id}")
    assert primary.status_code == 200
    assert "<title>PMA Hub</title>" in primary.text

    repo_root = client.get(f"/repos/{repo_id}/")
    assert repo_root.status_code == 200
    assert "<title>PMA Hub</title>" in repo_root.text

    legacy_prompt = client.get(f"/repos/{repo_id}/terminal")
    assert legacy_prompt.status_code == 200
    assert "Legacy/debug route" in legacy_prompt.text
    assert f"/legacy/repos/{repo_id}/terminal" in legacy_prompt.text

    legacy_terminal = client.get(f"/legacy/repos/{repo_id}/terminal")
    assert legacy_terminal.status_code == 200
    assert "Legacy/debug CAR UI" in legacy_terminal.text
    assert "<title>Codex Autorunner</title>" in legacy_terminal.text
