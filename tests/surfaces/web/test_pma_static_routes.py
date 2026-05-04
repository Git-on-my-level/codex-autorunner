from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app


def test_pma_top_level_routes_serve_new_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    for path in ("/pma", "/dashboard", "/repos", "/tickets", "/settings"):
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
