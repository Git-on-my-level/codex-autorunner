import base64
import hashlib

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.hub_repo_routes.mount_manager import (
    _LazyRepoApp,
)
from codex_autorunner.surfaces.web.static_assets import (
    _inline_script_hashes,
    render_pma_index_html,
)

PMA_MANUAL_SCREENSHOT_ROUTES = (
    "/pma",
    "/pma-memory",
    "/dashboard",
    "/repos",
    "/repos/example",
    "/repos/example/tickets",
    "/repos/example/tickets/TICKET-100",
    "/worktrees",
    "/worktrees/example",
    "/worktrees/example/tickets",
    "/worktrees/example/tickets/TICKET-100",
    "/tickets",
    "/tickets/TICKET-100",
    "/contextspace/example",
    "/settings",
)


def _script_hash(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def test_pma_top_level_routes_serve_new_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    for path in PMA_MANUAL_SCREENSHOT_ROUTES:
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>PMA Hub</title>" in response.text
        assert "/_app/immutable/entry/app." in response.text


def test_pma_dynamic_spa_fallback_routes_with_runtime_ids(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    for path in (
        "/repos/repo.with.dots",
        "/repos/codex-autorunner--discord-5/",
        "/repos/codex-autorunner--discord-5/tickets/100",
        "/repos/codex-autorunner--discord-5/tickets/",
        "/repos/codex-autorunner--discord-5/tickets/100/",
        "/worktrees/base--ticket-290",
        "/worktrees/base--ticket-290/tickets/tkt_pma_ui_regression_fixtures_smoke_qa",
        "/worktrees/base--ticket-290/tickets/",
        "/worktrees/base--ticket-290/tickets/tkt_pma_ui_regression_fixtures_smoke_qa/",
        "/tickets/tkt_pma_ui_regression_fixtures_smoke_qa",
        "/tickets/TICKET-290-pma-ui-regression-fixtures-and-smoke-qa",
        "/contextspace/worktree-1",
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


def test_pma_index_csp_allows_sveltekit_bootstrap_without_weakening_legacy(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    pma_csp = client.get("/pma").headers["Content-Security-Policy"]
    legacy_csp = client.get("/legacy").headers["Content-Security-Policy"]

    assert "script-src 'self' 'sha256-" in pma_csp
    assert "'unsafe-inline'" not in pma_csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "script-src 'self';" in legacy_csp


def test_inline_script_hashes_match_mixed_case_script_tags():
    assert _inline_script_hashes("<SCRIPT>alpha()</SCRIPT>") == [
        _script_hash("alpha()")
    ]
    assert _inline_script_hashes('<ScRiPt type="module">beta()</sCrIpT>') == [
        _script_hash("beta()")
    ]


def test_pma_base_path_routes_redirect_and_serve_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(
        create_hub_app(hub_root, base_path="/car"), follow_redirects=False
    )

    assert client.get("/").headers["location"] == "/car/"
    assert client.get("/pma").headers["location"] == "/car/pma"
    assert client.get("/pma-memory").headers["location"] == "/car/pma-memory"
    assert (
        client.get("/worktrees/example").headers["location"] == "/car/worktrees/example"
    )
    response = client.get("/car/pma-memory")
    assert response.status_code == 200
    assert "<title>PMA Hub</title>" in response.text
    assert 'globalThis.__CAR_BASE_PATH__ = "/car";' in response.text
    assert 'href="/car/_app/immutable/entry/start.' in response.text
    assert 'import("/car/_app/immutable/entry/start.' in response.text
    assert '"/_app/' not in response.text


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


def test_pma_index_base_path_rewrites_asset_urls(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        """
<link href="/_app/immutable/entry/start.abc.js" rel="modulepreload">
<script>
  import("/_app/immutable/entry/start.abc.js");
</script>
""",
        encoding="utf-8",
    )

    html = render_pma_index_html(static_dir, base_path="/car/")

    assert 'href="/car/_app/immutable/entry/start.abc.js"' in html
    assert 'import("/car/_app/immutable/entry/start.abc.js")' in html
    assert '"/_app/' not in html


def test_inline_script_hashes_match_malformed_end_tag_spacing():
    assert _inline_script_hashes("<script>gamma()</script\t\n bar>") == [
        _script_hash("gamma()")
    ]


def test_legacy_repo_gate_escapes_repo_and_query_derived_href_values(hub_env):
    client = TestClient(create_hub_app(hub_env.hub_root), follow_redirects=False)
    payload = "%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E"

    response = client.get(f"/repos/{payload}/terminal?next=%22%3E%3Cimg%20src=x%3E")

    assert response.status_code == 200
    assert (
        'href="/repos/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E"'
        in response.text
    )
    assert (
        'href="/legacy/repos/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E/terminal'
        in response.text
    )
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert 'next="><img src=x>' not in response.text


async def test_legacy_repo_mount_debug_page_escapes_deep_path_query_href_values(
    hub_env,
):
    repo_id = hub_env.repo_id
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        raise AssertionError("legacy debug page should not read request body")

    def build_repo_app(_repo_path):
        raise AssertionError("legacy debug page should not build the repo app")

    app = _LazyRepoApp(
        prefix=repo_id,
        repo_path=hub_env.repo_root,
        build_repo_app=build_repo_app,
        logger=None,
        hub_started=lambda: False,
    )

    await app(
        {
            "type": "http",
            "path": "/terminal/subpath",
            "root_path": f"/repos/{repo_id}",
            "query_string": b'next="><img src=x>',
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 200
    body = sent[1]["body"].decode("utf-8")
    assert "Legacy/debug route" in body
    assert f'href="/repos/{repo_id}"' in body
    assert (
        f'href="/repos/{repo_id}/terminal/subpath?next=&quot;&gt;&lt;img src=x&gt;'
        f'&amp;legacy=1"'
    ) in body
    assert 'next="><img src=x>' not in body
    assert "<img src=x>" not in body
