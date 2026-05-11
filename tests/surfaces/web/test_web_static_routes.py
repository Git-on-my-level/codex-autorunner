import base64
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.hub_repo_routes.mount_manager import (
    _LazyRepoApp,
)
from codex_autorunner.surfaces.web.static_assets import (
    _inline_script_hashes,
    render_web_index_html,
)

PMA_MANUAL_SCREENSHOT_ROUTES = (
    "/chats",
    "/hub",
    "/repos",
    "/repos/smoke-repo",
    "/repos/smoke-repo/tickets",
    "/repos/smoke-repo/tickets/TICKET-350-smoke-fixture",
    "/repos/smoke-repo/worktrees/smoke-repo--review",
    "/repos/smoke-repo/worktrees/smoke-repo--review/contextspace",
    "/repos/smoke-repo/worktrees/smoke-repo--review/tickets",
    "/contextspace/local",
    "/tickets",
    "/tickets/TICKET-350-smoke-fixture",
    "/settings",
)


def _script_hash(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def _seed_manifest_worktree(hub_root: Path, *, base_id: str, worktree_id: str) -> Path:
    base_root = hub_root / base_id
    worktree_root = hub_root / "worktrees" / worktree_id
    seed_repo_files(base_root, force=True, git_required=False)
    seed_repo_files(worktree_root, force=True, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(
        hub_root,
        base_root,
        repo_id=base_id,
        kind="base",
    )
    manifest.ensure_repo(
        hub_root,
        worktree_root,
        repo_id=worktree_id,
        kind="worktree",
        worktree_of=base_id,
        branch="feature/test",
    )
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    return worktree_root


def test_pma_top_level_routes_serve_new_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _seed_manifest_worktree(
        hub_root, base_id="smoke-repo", worktree_id="smoke-repo--review"
    )
    client = TestClient(create_hub_app(hub_root))

    for path in PMA_MANUAL_SCREENSHOT_ROUTES:
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>Web Hub</title>" in response.text
        assert "/_app/immutable/entry/app." in response.text


def test_pma_dynamic_spa_fallback_routes_with_runtime_ids(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    for path in (
        "/chats/5b78deb3-306c-4287-af8e-b1c9f80124a7",
        "/chats/nested/deep-link/segment",
        "/repos/repo.with.dots",
        "/repos/codex-autorunner--discord-5/",
        "/repos/codex-autorunner--discord-5/tickets/100",
        "/repos/codex-autorunner--discord-5/tickets/",
        "/repos/codex-autorunner--discord-5/tickets/100/",
        "/tickets/tkt_pma_ui_regression_fixtures_smoke_qa",
        "/tickets/TICKET-290-web-ui-regression-fixtures-and-smoke-qa",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>Web Hub</title>" in response.text
        assert "/_app/immutable/entry/app." in response.text


def test_removed_legacy_frontend_routes_redirect_permanently(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _seed_manifest_worktree(hub_root, base_id="base", worktree_id="base--ticket-290")
    client = TestClient(create_hub_app(hub_root), follow_redirects=False)

    worktrees = client.get("/worktrees")
    worktree = client.get("/worktrees/base--ticket-290/tickets/TICKET-100")

    assert worktrees.status_code == 308
    assert worktrees.headers["location"] == "/repos"
    assert worktree.status_code == 308
    assert (
        worktree.headers["location"]
        == "/repos/base/worktrees/base--ticket-290/tickets/TICKET-100"
    )


def test_scope_frontend_routes_cover_hub_repo_and_parent_scoped_worktree(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _seed_manifest_worktree(hub_root, base_id="base", worktree_id="base--review")
    client = TestClient(create_hub_app(hub_root), follow_redirects=False)

    cases = (
        "/chats",
        "/repos/base",
        "/repos/base/worktrees/base--review",
        "/repos/base/worktrees/base--review/contextspace",
        "/repos/base/worktrees/base--review/tickets/TICKET-100",
    )
    for path in cases:
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>Web Hub</title>" in response.text


def test_worktree_frontend_route_rejects_missing_parent_scope(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _seed_manifest_worktree(hub_root, base_id="base", worktree_id="base--review")
    client = TestClient(create_hub_app(hub_root), follow_redirects=False)

    missing = client.get("/worktrees/missing-worktree")
    mismatched = client.get("/repos/other/worktrees/base--review")

    assert missing.status_code == 404
    assert missing.json()["detail"] == "Worktree not found: missing-worktree"
    assert mismatched.status_code == 404
    assert (
        mismatched.json()["detail"]
        == "Worktree not found in repo scope: other/base--review"
    )


def test_worktree_frontend_route_rejects_orphaned_manifest_worktree(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    worktree_root = hub_root / "worktrees" / "orphan"
    seed_repo_files(worktree_root, force=True, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(
        hub_root,
        worktree_root,
        repo_id="orphan",
        kind="worktree",
    )
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    client = TestClient(create_hub_app(hub_root), follow_redirects=False)

    response = client.get("/worktrees/orphan")

    assert response.status_code == 400
    assert response.json()["detail"] == "Worktree route requires parent repo scope"


def test_web_static_assets_are_served_without_legacy_static_by_default(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    page = client.get("/chats")
    asset_path = page.text.split('href="/_app/', 1)[1].split('"', 1)[0]
    asset_response = client.get(f"/_app/{asset_path}")

    assert asset_response.status_code == 200
    assert "max-age=31536000" in asset_response.headers.get("Cache-Control", "")
    assert client.get("/legacy").status_code == 404
    assert client.get("/static/generated/app.js").status_code == 404
    assert client.get("/hub/usage").status_code == 404
    assert client.get("/hub/usage/series").status_code == 404


def test_pma_index_csp_allows_sveltekit_bootstrap(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    pma_csp = client.get("/chats").headers["Content-Security-Policy"]

    assert "script-src 'self' 'sha256-" in pma_csp
    assert "'unsafe-inline'" not in pma_csp.split("script-src", 1)[1].split(";", 1)[0]


def test_inline_script_hashes_match_mixed_case_script_tags():
    assert _inline_script_hashes("<SCRIPT>alpha()</SCRIPT>") == [
        _script_hash("alpha()")
    ]
    assert _inline_script_hashes('<ScRiPt type="module">beta()</sCrIpT>') == [
        _script_hash("beta()")
    ]


def test_removed_web_hub_routes_are_not_registered(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    assert client.get("/pma").status_code == 404
    assert client.get("/pma-memory").status_code == 404
    assert client.get("/dashboard").status_code == 404


def test_pma_base_path_routes_redirect_and_serve_spa(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(
        create_hub_app(hub_root, base_path="/car"), follow_redirects=False
    )

    assert client.get("/").headers["location"] == "/car/"
    assert client.get("/chats").headers["location"] == "/car/chats"
    assert (
        client.get("/worktrees/example").headers["location"] == "/car/worktrees/example"
    )
    response = client.get("/car/chats")
    assert response.status_code == 200
    assert "<title>Web Hub</title>" in response.text
    assert 'globalThis.__CAR_BASE_PATH__ = "/car";' in response.text
    assert 'href="/car/_app/immutable/entry/start.' in response.text
    assert 'import("/car/_app/immutable/entry/start.' in response.text
    assert '"/_app/' not in response.text


def test_repo_mount_frontend_routes_redirect_to_pma_by_default(hub_env):
    client = TestClient(create_hub_app(hub_env.hub_root), follow_redirects=False)
    repo_id = hub_env.repo_id

    primary = client.get(f"/repos/{repo_id}")
    assert primary.status_code == 200
    assert "<title>Web Hub</title>" in primary.text

    repo_root = client.get(f"/repos/{repo_id}/")
    assert repo_root.status_code == 200
    assert "<title>Web Hub</title>" in repo_root.text

    legacy_prompt = client.get(f"/repos/{repo_id}/terminal")
    assert legacy_prompt.status_code == 307
    assert legacy_prompt.headers["location"] == f"/repos/{repo_id}"

    legacy_terminal = client.get(f"/legacy/repos/{repo_id}/terminal")
    assert legacy_terminal.status_code == 404


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

    html = render_web_index_html(static_dir, base_path="/car/")

    assert 'href="/car/_app/immutable/entry/start.abc.js"' in html
    assert 'import("/car/_app/immutable/entry/start.abc.js")' in html
    assert '"/_app/' not in html


def test_inline_script_hashes_match_malformed_end_tag_spacing():
    assert _inline_script_hashes("<script>gamma()</script\t\n bar>") == [
        _script_hash("gamma()")
    ]


def test_removed_repo_gate_redirects_encoded_repo_and_query_values(hub_env):
    client = TestClient(create_hub_app(hub_env.hub_root), follow_redirects=False)
    payload = "%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E"

    response = client.get(f"/repos/{payload}/terminal?next=%22%3E%3Cimg%20src=x%3E")

    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "/repos/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E"
    )


async def test_removed_repo_mount_debug_query_redirects_without_building_repo_app(
    hub_env,
):
    repo_id = hub_env.repo_id
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        raise AssertionError("redirect should not read request body")

    def build_repo_app(_repo_path):
        raise AssertionError("redirect should not build the repo app")

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

    assert sent[0]["status"] == 307
    assert sent[0]["headers"] == [(b"location", f"/repos/{repo_id}".encode())]
    assert sent[1]["body"] == b""
