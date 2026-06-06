from __future__ import annotations

import gzip
import http.server
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import Server, ServerConnection, serve

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.locks import process_is_active
from codex_autorunner.core.managed_processes.registry import read_process_record
from codex_autorunner.core.preview_services import PROCESS_KIND, PreviewServiceRegistry
from codex_autorunner.core.process_termination import terminate_record
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import services as services_routes
from codex_autorunner.surfaces.web.services.preview_capabilities import (
    PreviewCapabilityStore,
)


def test_preview_services_disabled_config_does_not_mount_routes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(hub_root, {"preview_services": {"enabled": False}})

    client = TestClient(create_hub_app(hub_root))

    assert client.get("/hub/services").status_code == 404
    assert client.get("/hub/read-models/services").status_code == 404
    assert client.get("/preview/services/svc_missing000/").status_code == 404


def test_preview_services_static_crud_and_read_model(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    html = hub_root / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/services/static",
        json={
            "path": str(html),
            "name": "Static preview",
            "scope_links": [{"kind": "repo", "id": "repo-1"}],
            "created_by": "test",
        },
    )

    assert created.status_code == 200
    service = created.json()["service"]
    service_id = service["service_id"]
    assert service["kind"] == "static_file"
    assert service["service_class"] == "preview"
    assert service["trust_level"] == "generated"
    assert service["ownership"] == "static"
    assert service["exposure"]["car_url"] == f"/preview/services/{service_id}/"
    assert created.json()["read_model"]["scope"] == "repo:repo-1"
    assert created.json()["read_model"]["capabilities"]["can_open"] is True
    assert created.json()["read_model"]["preview_url"] is None
    assert created.json()["read_model"]["preview_url_status"] == "not_issued"

    listing = client.get("/hub/services")
    assert listing.status_code == 200
    assert [item["service_id"] for item in listing.json()["services"]] == [service_id]

    read_model = client.get("/hub/read-models/services")
    assert read_model.status_code == 200
    payload = read_model.json()
    assert payload["counts"] == {
        "total": 1,
        "running": 0,
        "attention": 0,
        "managed": 0,
        "static": 1,
        "loopback": 0,
        "preview": 1,
        "application": 0,
        "infrastructure": 0,
    }
    assert payload["services"][0]["car_url"] == f"/preview/services/{service_id}/"
    assert payload["services"][0]["preview_url"] is None
    assert payload["services"][0]["preview_url_status"] == "not_issued"

    scoped = client.get("/hub/read-models/services", params={"scope": "repo:repo-1"})
    assert scoped.status_code == 200
    assert len(scoped.json()["services"]) == 1

    updated = client.patch(
        f"/hub/services/{service_id}",
        json={"name": "Updated preview", "metadata": {"ticket": "140"}},
    )
    assert updated.status_code == 200
    assert updated.json()["service"]["name"] == "Updated preview"
    assert updated.json()["service"]["metadata"] == {"ticket": "140"}

    deleted = client.delete(f"/hub/services/{service_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/hub/services/{service_id}").status_code == 404


def test_preview_static_file_opens_through_car_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    html = hub_root / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Static preview"},
    )

    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    opened = client.get(f"/preview/services/{service_id}/")
    assert opened.status_code == 200
    assert "<h1>Preview</h1>" in opened.text
    assert "content-security-policy" not in opened.headers
    assert "x-frame-options" not in opened.headers
    by_name = client.get(f"/preview/services/{service_id}/index.html")
    assert by_name.status_code == 200
    assert by_name.text == opened.text


def test_hosted_preview_capability_cannot_authorize_hub_apis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(
        hub_root,
        {
            "auth": {"mode": "hosted_bearer"},
            "server": {
                "auth_token_env": "CAR_TEST_TOKEN",
                "allowed_hosts": ["testserver"],
            },
        },
    )
    monkeypatch.setenv("CAR_TEST_TOKEN", "hub-secret")
    html = hub_root / "index.html"
    html.write_text(
        "<html><script>fetch('/hub/services')</script><h1>Preview</h1></html>",
        encoding="utf-8",
    )
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))

    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Static preview"},
        headers={"Authorization": "Bearer hub-secret"},
    )
    assert created.status_code == 200
    service = created.json()["read_model"]
    issued = client.post(
        f"/hub/services/{service['service_id']}/preview-token",
        headers={"Authorization": "Bearer hub-secret"},
    )
    assert issued.status_code == 200
    preview_url = issued.json()["preview_url"]
    preview_token = preview_url.split("/preview/p/", 1)[1].split("/", 1)[0]

    opened = client.get(preview_url)
    assert opened.status_code == 200
    assert "<h1>Preview</h1>" in opened.text
    asset = client.get(f"{preview_url}index.html")
    assert asset.status_code == 200

    denied_with_preview_token = client.get(
        "/hub/services",
        headers={"Authorization": f"Bearer {preview_token}"},
    )
    assert denied_with_preview_token.status_code == 401
    denied_with_cookie = client.get(
        "/hub/read-models/services",
        headers={"Cookie": "car_session=ambient", "Authorization": "Basic abc"},
    )
    assert denied_with_cookie.status_code == 401
    destructive_denied = client.post(
        f"/hub/services/{service['service_id']}/unlink",
        headers={"Authorization": f"Bearer {preview_token}"},
    )
    assert destructive_denied.status_code == 401

    allowed = client.get(
        "/hub/read-models/services",
        headers={"Authorization": "Bearer hub-secret"},
    )
    assert allowed.status_code == 200


def test_hosted_bearer_without_token_fails_startup(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(
        hub_root,
        {
            "auth": {"mode": "hosted_bearer"},
            "server": {
                "auth_token_env": "CAR_TEST_MISSING_TOKEN",
                "allowed_hosts": ["testserver"],
            },
        },
    )
    with pytest.raises(RuntimeError, match="auth.mode=hosted_bearer requires"):
        create_hub_app(hub_root, endpoint_host="0.0.0.0")


def test_hosted_direct_preview_services_redirects_to_capability_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(
        hub_root,
        {
            "auth": {"mode": "hosted_bearer"},
            "server": {
                "auth_token_env": "CAR_TEST_TOKEN",
                "allowed_hosts": ["testserver"],
            },
        },
    )
    monkeypatch.setenv("CAR_TEST_TOKEN", "hub-secret")
    html = hub_root / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))
    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Static preview"},
        headers={"Authorization": "Bearer hub-secret"},
    )
    service_id = created.json()["service"]["service_id"]

    unauthenticated = client.get(
        f"/preview/services/{service_id}/", follow_redirects=False
    )
    assert unauthenticated.status_code == 401
    redirected = client.get(
        f"/preview/services/{service_id}/",
        headers={"Authorization": "Bearer hub-secret"},
        follow_redirects=False,
    )
    assert redirected.status_code == 307
    assert "/preview/p/" in redirected.headers["location"]
    nested_redirect = client.get(
        f"/preview/services/{service_id}/index.html",
        headers={"Authorization": "Bearer hub-secret"},
        follow_redirects=False,
    )
    assert nested_redirect.status_code == 307
    nested_location = nested_redirect.headers["location"]
    assert nested_location.endswith("/index.html")
    nested_opened = client.get(nested_location)
    assert nested_opened.status_code == 200
    assert nested_opened.text == "<h1>Preview</h1>"


def test_preview_capability_store_expiry_and_service_revocation(
    tmp_path: Path,
) -> None:
    now = 1_000.0
    store = PreviewCapabilityStore(tmp_path, now=lambda: now)
    first = store.issue("svc_first123", ttl_seconds=10)
    second = store.issue("svc_second123", path_prefix="assets", ttl_seconds=100)

    assert store.validate(first.token).service_id == "svc_first123"
    assert store.validate(second.token).path_prefix == "assets"

    now += 11
    assert store.validate(first.token) is None
    assert store.validate(second.token).service_id == "svc_second123"

    assert store.revoke_service("svc_second123") == 1
    assert store.validate(second.token) is None


def test_preview_static_dir_opens_index_and_nested_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    static_dir = hub_root / "site"
    nested = static_dir / "assets"
    nested.mkdir(parents=True)
    (static_dir / "index.html").write_text("home", encoding="utf-8")
    (nested / "app.txt").write_text("asset", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/services/static",
        json={"path": str(static_dir), "kind": "static_dir", "name": "Site"},
    )

    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    issued = client.post(f"/hub/services/{service_id}/preview-token")
    assert issued.status_code == 200
    preview_url = issued.json()["preview_url"]
    assert client.get(f"/preview/services/{service_id}/").text == "home"
    asset = client.get(f"/preview/services/{service_id}/assets/app.txt")
    assert asset.status_code == 200
    assert asset.text == "asset"
    capability_asset = client.get(f"{preview_url}assets/app.txt")
    assert capability_asset.status_code == 200
    assert capability_asset.text == "asset"
    assert client.get("/preview/p/not-a-valid-token/").status_code == 403


def test_preview_static_security_rejects_unsafe_paths(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    static_dir = hub_root / "site"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("home", encoding="utf-8")
    (static_dir / ".env").write_text("SECRET=1", encoding="utf-8")
    (static_dir / ".hidden").write_text("hidden", encoding="utf-8")
    (static_dir / "id_rsa").write_text("key", encoding="utf-8")
    (static_dir / "private.pem").write_text("key", encoding="utf-8")
    git_dir = static_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("config", encoding="utf-8")
    codex_dir = static_dir / ".codex-autorunner" / "artifact"
    codex_dir.mkdir(parents=True)
    (codex_dir / "index.html").write_text("secret", encoding="utf-8")
    hidden_dir = static_dir / "public" / ".hidden-parent"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "index.html").write_text("hidden ancestor", encoding="utf-8")
    secrets_dir = static_dir / "safe" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "index.html").write_text("secret ancestor", encoding="utf-8")
    outside = hub_root / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    symlink = static_dir / "outside-link"
    symlink.symlink_to(outside)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(static_dir), "kind": "static_dir", "name": "Site"},
    )
    service_id = created.json()["service"]["service_id"]

    forbidden_paths = [
        "../outside.txt",
        ".env",
        ".hidden",
        ".git/config",
        ".codex-autorunner/artifact/index.html",
        "public/.hidden-parent/index.html",
        "safe/secrets/index.html",
        "id_rsa",
        "private.pem",
        "outside-link",
    ]
    for path in forbidden_paths:
        response = client.get(f"/preview/services/{service_id}/{path}")
        if path.startswith("../"):
            assert response.status_code in {403, 404}, path
        else:
            assert response.status_code == 403, path


def test_preview_static_file_under_hidden_ancestor_is_rejected(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    hidden_dir = hub_root / "public" / ".generated"
    hidden_dir.mkdir(parents=True)
    html = hidden_dir / "index.html"
    html.write_text("hidden", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Hidden file"},
    )
    service_id = created.json()["service"]["service_id"]

    opened = client.get(f"/preview/services/{service_id}/")

    assert opened.status_code == 403


def test_preview_static_dir_blocks_symlinked_directory_escape(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    static_dir = hub_root / "site"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("home", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "index.html").write_text("secret", encoding="utf-8")
    (static_dir / "linked").symlink_to(outside_dir, target_is_directory=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(static_dir), "kind": "static_dir", "name": "Site"},
    )
    service_id = created.json()["service"]["service_id"]

    opened = client.get(f"/preview/services/{service_id}/linked/index.html")

    assert opened.status_code == 403


def test_preview_static_dir_blocks_symlinked_directory_inside_root(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    static_dir = hub_root / "site"
    static_dir.mkdir()
    safe_dir = static_dir / "safe-real-dir"
    safe_dir.mkdir()
    (safe_dir / "index.html").write_text("safe", encoding="utf-8")
    (static_dir / "linkdir").symlink_to(safe_dir, target_is_directory=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(static_dir), "kind": "static_dir", "name": "Site"},
    )
    service_id = created.json()["service"]["service_id"]

    opened = client.get(f"/preview/services/{service_id}/linkdir/index.html")

    assert opened.status_code == 403


def test_preview_static_target_must_be_under_allowed_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(outside), "name": "Outside"},
    )

    assert created.status_code == 400
    assert "outside allowed roots" in created.json()["detail"]


def test_preview_static_registration_rejects_ambiguous_relative_hub_path(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/services/static",
        json={"path": "./dist", "name": "Relative"},
    )

    assert created.status_code == 400


def test_preview_static_workspace_source_registers_workspace_artifact(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    workspace_root = hub_root / ".codex-autorunner" / "workspaces" / "ws_demo123"
    dist = workspace_root / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("workspace artifact", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/services/static",
        json={
            "path": "ignored-for-workspace-source",
            "source": {
                "type": "workspace",
                "workspace_id": "ws_demo123",
                "path": "dist",
            },
            "kind": "static_dir",
            "name": "Workspace artifact",
        },
    )

    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    opened = client.get(f"/preview/services/{service_id}/")
    assert opened.status_code == 200
    assert opened.text == "workspace artifact"


def test_preview_static_workspace_scope_does_not_expand_registration_roots(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    outside_root = tmp_path / "outside-root"
    outside_root.mkdir()
    outside = outside_root / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={
            "path": str(outside),
            "name": "Outside",
            "scope_links": [{"kind": "workspace", "path": str(outside_root)}],
        },
    )
    assert created.status_code == 400
    assert "outside allowed roots" in created.json()["detail"]


def test_preview_static_allowed_roots_resolve_relative_to_hub_root(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    context = SimpleNamespace(
        config=SimpleNamespace(
            root=hub_root,
            raw={"preview_services": {"static_allowed_roots": ["allowed-static"]}},
        ),
        supervisor=SimpleNamespace(list_repos=lambda use_cache=True: []),
    )

    assert (hub_root / "allowed-static").resolve() in (
        services_routes._allowed_static_roots(context, SimpleNamespace())
    )


def test_preview_loopback_http_service_proxies_method_path_query_and_body(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    server, port = _start_loopback_capture_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/base/", "name": "Loopback"},
        )
        assert created.status_code == 200
        service_id = created.json()["service"]["service_id"]

        response = client.post(
            f"/preview/services/{service_id}/nested/path",
            params={"q": "one", "token": "hub-secret"},
            content=b"payload",
            headers={
                "Authorization": "Bearer hub-secret",
                "Cookie": "car_session=hub-secret",
                "X-Test-Header": "kept",
            },
        )

        assert response.status_code == 201
        assert response.headers["x-upstream"] == "seen"
        assert "set-cookie" not in response.headers
        assert response.json() == {
            "method": "POST",
            "path": "/base/nested/path?q=one&token=hub-secret",
            "body": "payload",
            "header": "kept",
            "authorization": None,
            "cookie": None,
            "accept_encoding": "identity",
            "forwarded_host": "testserver",
            "forwarded_proto": "http",
            "forwarded_port": "80",
            "forwarded_prefix": f"/preview/services/{service_id}",
            "real_ip": "testclient",
        }
    finally:
        server.shutdown()
        server.server_close()


def test_preview_loopback_http_strips_stale_encoded_response_headers(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    server, port = _start_loopback_gzip_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/", "name": "Gzip"},
        )
        service_id = created.json()["service"]["service_id"]

        response = client.get(
            f"/preview/services/{service_id}/encoded",
            headers={"Accept-Encoding": "identity"},
        )

        assert response.status_code == 200
        assert response.content == b"compressed payload"
        assert "content-encoding" not in response.headers
        assert "content-length" not in response.headers
    finally:
        server.shutdown()
        server.server_close()


def test_preview_loopback_http_rewrites_redirect_locations(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    server, port = _start_loopback_redirect_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/base/", "name": "Redirects"},
        )
        service_id = created.json()["service"]["service_id"]
        prefix = f"http://testserver/preview/services/{service_id}"

        root_redirect = client.get(
            f"/preview/services/{service_id}/root-redirect",
            follow_redirects=False,
        )
        assert root_redirect.status_code == 302
        assert root_redirect.headers["location"] == f"{prefix}/login"

        absolute_redirect = client.get(
            f"/preview/services/{service_id}/absolute-redirect",
            follow_redirects=False,
        )
        assert absolute_redirect.status_code == 302
        assert absolute_redirect.headers["location"] == f"{prefix}/path?next=1"
    finally:
        server.shutdown()
        server.server_close()


def test_preview_proxy_request_body_limit_returns_413(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(
        hub_root,
        {"preview_services": {"proxy_max_body_bytes": 4}},
    )
    server, port = _start_loopback_capture_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/", "name": "Limit"},
        )
        service_id = created.json()["service"]["service_id"]

        response = client.post(
            f"/preview/services/{service_id}/too-large",
            content=b"payload",
        )

        assert response.status_code == 413
    finally:
        server.shutdown()
        server.server_close()


def test_preview_proxy_timeout_config_builds_finite_httpx_timeout(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _update_hub_config(
        hub_root,
        {
            "preview_services": {
                "proxy_connect_timeout_seconds": 0.5,
                "proxy_read_timeout_seconds": 1.5,
                "proxy_write_timeout_seconds": 2.5,
                "proxy_pool_timeout_seconds": 3.5,
            }
        },
    )
    app = create_hub_app(hub_root)

    context = SimpleNamespace(config=app.state.config)

    timeout = services_routes._proxy_timeout(context)

    assert timeout.connect == 0.5
    assert timeout.read == 1.5
    assert timeout.write == 2.5
    assert timeout.pool == 3.5


def test_preview_loopback_http_streaming_response_stays_incremental(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    server, port = _start_loopback_streaming_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/", "name": "Stream"},
        )
        service_id = created.json()["service"]["service_id"]

        with client.stream("GET", f"/preview/services/{service_id}/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert list(response.iter_lines()) == ["data: one", "", "data: two", ""]
    finally:
        server.shutdown()
        server.server_close()


def test_preview_loopback_websocket_echo_preserves_path_and_query(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    upstream, port = _start_websocket_echo_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://127.0.0.1:{port}/base/", "name": "WS"},
        )
        assert created.status_code == 200
        service_id = created.json()["service"]["service_id"]

        with client.websocket_connect(
            f"/preview/services/{service_id}/nested/socket?client=abc&token=hub-secret",
            headers={
                "Authorization": "Bearer hub-secret",
                "Cookie": "car_session=hub-secret",
            },
        ) as websocket:
            websocket.send_text("hello")
            assert (
                websocket.receive_text()
                == "path=/base/nested/socket?client=abc&token=hub-secret"
            )
            assert websocket.receive_text() == "echo:hello"
            websocket.send_bytes(b"raw")
            assert websocket.receive_bytes() == b"echo:raw"
    finally:
        upstream.shutdown()


def test_preview_loopback_websocket_same_origin_hmr_path_connects(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    upstream, port = _start_websocket_echo_server()
    client = TestClient(create_hub_app(hub_root))
    try:
        created = client.post(
            "/hub/services/loopback-url",
            json={"url": f"http://localhost:{port}/", "name": "HMR"},
        )
        service_id = created.json()["service"]["service_id"]

        with client.websocket_connect(
            f"/preview/services/{service_id}/@vite/client"
        ) as websocket:
            websocket.send_text("vite-hmr")
            assert websocket.receive_text() == "path=/@vite/client"
            assert websocket.receive_text() == "echo:vite-hmr"
    finally:
        upstream.shutdown()


def test_preview_websocket_connect_kwargs_omit_unsupported_proxy_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_connect(uri: str, *, additional_headers=None):  # type: ignore[no-untyped-def]
        raise AssertionError("signature probe only")

    monkeypatch.setattr(services_routes.websockets, "connect", fake_connect)

    kwargs = services_routes._websocket_connect_kwargs({"x-test": "kept"})

    assert kwargs == {"additional_headers": {"x-test": "kept"}}


def test_preview_websocket_connect_kwargs_support_legacy_extra_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_connect(uri: str, *, extra_headers=None):  # type: ignore[no-untyped-def]
        raise AssertionError("signature probe only")

    monkeypatch.setattr(services_routes.websockets, "connect", fake_connect)

    kwargs = services_routes._websocket_connect_kwargs({"x-test": "kept"})

    assert kwargs == {"extra_headers": {"x-test": "kept"}}


def test_preview_websocket_rejects_unregistered_and_non_loopback_targets(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    registry = PreviewServiceRegistry(hub_root)
    record = registry.create_from_parts(
        name="Remote",
        kind="loopback_url",
        target={
            "host": "example.com",
            "port": 80,
            "scheme": "http",
            "direct_url": "http://example.com/",
        },
    )
    client = TestClient(create_hub_app(hub_root))

    with pytest.raises(WebSocketDisconnect) as missing:
        with client.websocket_connect("/preview/services/svc_missing000/"):
            pass
    assert missing.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(f"/preview/services/{record.service_id}/"):
            pass
    assert denied.value.code == 1008


def test_preview_managed_command_proxies_after_start(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    port = _find_available_port()
    created = client.post(
        "/hub/services/managed",
        json={
            "name": "Managed preview",
            "argv": _server_command(),
            "cwd": str(tmp_path),
            "port_policy": {"mode": "exact", "port": port},
            "health_check": {"type": "tcp", "path": None},
            "start": True,
        },
    )
    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    pid = created.json()["service"]["process"]["pid"]
    try:
        assert _wait_for_route_health(client, service_id)
        opened = client.get(f"/preview/services/{service_id}/")
        assert opened.status_code == 200
        assert opened.text == "ok"
    finally:
        if process_is_active(pid):
            client.post(f"/hub/services/{service_id}/teardown")


def test_preview_managed_command_receives_preview_env(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    port = _find_available_port()
    created = client.post(
        "/hub/services/managed",
        json={
            "name": "Managed preview env",
            "argv": _env_server_command(),
            "cwd": str(tmp_path),
            "port_policy": {"mode": "exact", "port": port},
            "health_check": {"type": "tcp", "path": None},
            "start": True,
        },
    )
    assert created.status_code == 200
    service = created.json()["service"]
    service_id = service["service_id"]
    pid = service["process"]["pid"]
    try:
        assert _wait_for_route_health(client, service_id)
        opened = client.get(f"/preview/services/{service_id}/")
        assert opened.status_code == 200
        assert opened.json() == {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "CAR_PREVIEW_SERVICE_ID": service_id,
            "CAR_PREVIEW_BASE_PATH": f"/preview/services/{service_id}",
            "CAR_PREVIEW_PUBLIC_URL": f"/preview/services/{service_id}/",
        }
    finally:
        if process_is_active(pid):
            client.post(f"/hub/services/{service_id}/teardown")


def test_preview_proxy_rejects_non_loopback_targets_by_default(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    registry = PreviewServiceRegistry(hub_root)
    record = registry.create_from_parts(
        name="Remote",
        kind="loopback_url",
        target={
            "host": "example.com",
            "port": 80,
            "scheme": "http",
            "direct_url": "http://example.com/",
        },
    )
    client = TestClient(create_hub_app(hub_root))

    opened = client.get(f"/preview/services/{record.service_id}/")

    assert opened.status_code == 403


def test_preview_services_reject_invalid_and_missing_resources(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    missing_static = client.post(
        "/hub/services/static",
        json={"path": str(tmp_path / "missing-dir"), "kind": "static_dir"},
    )
    assert missing_static.status_code == 400

    remote_loopback = client.post(
        "/hub/services/loopback-url",
        json={"url": "https://example.com/"},
    )
    assert remote_loopback.status_code == 400

    missing = client.get("/hub/services/svc_missing000")
    assert missing.status_code == 404


def test_preview_services_managed_lifecycle_logs_and_force_semantics(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    port = _find_available_port()

    created = client.post(
        "/hub/services/managed",
        json={
            "name": "Managed preview",
            "argv": _server_command(),
            "cwd": str(tmp_path),
            "port_policy": {"mode": "exact", "port": port},
            "health_check": {"type": "tcp", "path": None},
        },
    )

    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    assert created.json()["service"]["status"] == "stopped"

    force_without_attestation = client.post(
        f"/hub/services/{service_id}/kill",
        json={"force": True},
    )
    assert force_without_attestation.status_code == 400
    assert "--force requires --force-attestation" in force_without_attestation.text

    started = client.post(f"/hub/services/{service_id}/start")
    assert started.status_code == 200
    started_service = started.json()["service"]
    assert started_service["status"] == "healthy"
    assert started_service["target"]["port"] == port
    pid = started_service["process"]["pid"]
    try:
        assert _wait_for_process(pid)

        unlink_running = client.post(f"/hub/services/{service_id}/unlink")
        assert unlink_running.status_code == 400
        assert "Cannot unlink a running managed preview service" in unlink_running.text

        assert _wait_for_route_health(client, service_id)

        logs = client.get(f"/hub/services/{service_id}/logs", params={"tail": 20})
        assert logs.status_code == 200
        assert logs.json()["service_id"] == service_id
        assert isinstance(logs.json()["events"], list)

        stopped = client.post(f"/hub/services/{service_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["service"]["status"] == "stopped"
    finally:
        if process_is_active(pid):
            client.post(f"/hub/services/{service_id}/stop")

    teardown = client.post(f"/hub/services/{service_id}/teardown")
    assert teardown.status_code == 200
    assert teardown.json()["deleted"] is True


def test_preview_services_preview_token_revoke_route(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    html = hub_root / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Static preview"},
    )
    service_id = created.json()["service"]["service_id"]

    issued = client.post(
        f"/hub/services/{service_id}/preview-token", params={"ttl": 60}
    )
    assert issued.status_code == 200
    preview_url = issued.json()["preview_url"]
    assert preview_url.startswith("/preview/p/")
    opened = client.get(preview_url)
    assert opened.status_code == 200

    revoked = client.post(f"/hub/services/{service_id}/preview-token/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["service_id"] == service_id
    assert revoked.json()["revoked"] >= 1
    denied = client.get(preview_url)
    assert denied.status_code == 403


def test_preview_services_rejects_running_managed_runtime_config_edit(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/managed",
        json={
            "name": "Managed preview",
            "argv": ["python", "-m", "http.server"],
            "cwd": str(tmp_path),
            "port_policy": {"mode": "auto"},
            "health_check": {"type": "tcp", "path": None},
        },
    )
    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]
    PreviewServiceRegistry(hub_root).update(
        service_id,
        {"status": "running"},
    )

    rejected = client.patch(
        f"/hub/services/{service_id}",
        json={"command": {"argv": ["python", "-V"], "cwd": str(tmp_path)}},
    )
    assert rejected.status_code == 400
    assert "stop it before changing command" in rejected.text

    autostart = client.patch(
        f"/hub/services/{service_id}",
        json={
            "restart_policy": {
                "auto_start_on_hub_start": True,
                "restart_on_exit": "never",
            }
        },
    )
    assert autostart.status_code == 200
    assert (
        autostart.json()["service"]["restart_policy"]["auto_start_on_hub_start"] is True
    )


def test_preview_services_forced_unlink_requires_attestation_and_logs_orphan(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    port = _find_available_port()
    created = client.post(
        "/hub/services/managed",
        json={
            "name": "Managed preview",
            "argv": _server_command(),
            "cwd": str(tmp_path),
            "port_policy": {"mode": "exact", "port": port},
            "health_check": {"type": "tcp", "path": None},
            "start": True,
        },
    )
    assert created.status_code == 200
    service = created.json()["service"]
    service_id = service["service_id"]
    process = service["process"]
    pid = process["pid"]
    pgid = process["pgid"]
    try:
        assert _wait_for_process(pid)

        force_without_attestation = client.post(
            f"/hub/services/{service_id}/unlink",
            json={"force": True},
        )
        assert force_without_attestation.status_code == 400
        assert "--force requires --force-attestation" in force_without_attestation.text
        assert client.get(f"/hub/services/{service_id}").status_code == 200
        assert process_is_active(pid)

        with caplog.at_level(
            logging.WARNING,
            logger="codex_autorunner.preview_services.routes",
        ):
            forced = client.post(
                f"/hub/services/{service_id}/unlink",
                json={
                    "force": True,
                    "force_attestation": "test intentionally leaves process running",
                },
            )
        assert forced.status_code == 200
        assert forced.json()["deleted"] is True
        assert client.get(f"/hub/services/{service_id}").status_code == 404
        assert process_is_active(pid)
        assert read_process_record(hub_root, PROCESS_KIND, service_id) is not None
        assert "preview_services.unlink_running_orphans_process" in caplog.text
    finally:
        if process_is_active(pid):
            terminate_record(
                pid,
                pgid,
                grace_seconds=0,
                kill_seconds=0.2,
                logger=logging.getLogger("test.preview_services.unlink"),
                event_prefix="test.preview_services.unlink.cleanup",
            )


def test_preview_services_port_conflict_returns_409(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    port = _find_available_port()

    first = client.post(
        "/hub/services/managed",
        json={
            "name": "First",
            "argv": _server_command(),
            "cwd": str(tmp_path),
            "port_policy": {"mode": "exact", "port": port},
        },
    )
    assert first.status_code == 200
    first_id = first.json()["service"]["service_id"]

    started = client.post(f"/hub/services/{first_id}/start")
    assert started.status_code == 200
    try:
        second = client.post(
            "/hub/services/managed",
            json={
                "name": "Second",
                "argv": _server_command(),
                "cwd": str(tmp_path),
                "port_policy": {"mode": "exact", "port": port},
                "start": True,
            },
        )
        assert second.status_code == 409
    finally:
        client.post(f"/hub/services/{first_id}/teardown")


def _server_command() -> list[str]:
    script = (
        "import http.server, os\n"
        "port=int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "print('preview route server ready', port, flush=True)\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    return [sys.executable, "-u", "-c", script]


def _env_server_command() -> list[str]:
    script = (
        "import http.server, json, os\n"
        "port=int(os.environ['PORT'])\n"
        "keys = [\n"
        "    'PORT',\n"
        "    'HOST',\n"
        "    'CAR_PREVIEW_SERVICE_ID',\n"
        "    'CAR_PREVIEW_BASE_PATH',\n"
        "    'CAR_PREVIEW_PUBLIC_URL',\n"
        "]\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body=json.dumps({key: os.environ.get(key) for key in keys}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "print('preview route env server ready', port, flush=True)\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    return [sys.executable, "-u", "-c", script]


def _start_loopback_capture_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    import json

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or "0"))
            payload: dict[str, Any] = {
                "method": self.command,
                "path": self.path,
                "body": body.decode("utf-8"),
                "header": self.headers.get("X-Test-Header"),
                "authorization": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
                "accept_encoding": self.headers.get("Accept-Encoding"),
                "forwarded_host": self.headers.get("X-Forwarded-Host"),
                "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
                "forwarded_port": self.headers.get("X-Forwarded-Port"),
                "forwarded_prefix": self.headers.get("X-Forwarded-Prefix"),
                "real_ip": self.headers.get("X-Real-IP"),
            }
            content = json.dumps(payload).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Upstream", "seen")
            self.send_header("Set-Cookie", "car_session=upstream; Path=/")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _start_loopback_gzip_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = gzip.compress(b"compressed payload")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _start_loopback_redirect_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.endswith("/root-redirect"):
                location = "/login"
            else:
                location = (
                    f"http://127.0.0.1:{self.server.server_address[1]}/base/path?next=1"
                )
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _start_loopback_streaming_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in (b"data: one\n\n", b"data: two\n\n"):
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.01)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _start_websocket_echo_server() -> tuple[Server, int]:
    def handler(connection: ServerConnection) -> None:
        connection.send(f"path={connection.request.path}")
        for message in connection:
            if isinstance(message, bytes):
                connection.send(b"echo:" + message)
            else:
                connection.send(f"echo:{message}")

    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.socket.getsockname()[1])


def _find_available_port(start: int = 42000) -> int:
    import socket

    for port in range(start, 55000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no available test port")


def _update_hub_config(hub_root: Path, updates: dict[str, Any]) -> None:
    config_path = hub_root / ".codex-autorunner" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    _deep_update(config, updates)
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_update(existing, value)
            continue
        target[key] = value


def _wait_for_process(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_is_active(pid):
            return True
        time.sleep(0.05)
    return process_is_active(pid)


def _wait_for_route_health(
    client: TestClient,
    service_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = client.post(f"/hub/services/{service_id}/health")
        if health.status_code == 200 and health.json()["health"]["ok"] is True:
            return True
        time.sleep(0.05)
    return False
