from __future__ import annotations

import http.server
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import Server, ServerConnection, serve

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.locks import process_is_active
from codex_autorunner.core.managed_processes.registry import read_process_record
from codex_autorunner.core.preview_services import PROCESS_KIND, PreviewServiceRegistry
from codex_autorunner.core.process_termination import terminate_record
from codex_autorunner.server import create_hub_app


def test_preview_services_static_crud_and_read_model(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    html = tmp_path / "index.html"
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
    assert service["exposure"]["car_url"] == f"/preview/services/{service_id}/"
    assert created.json()["read_model"]["scope"] == "repo:repo-1"

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
    }
    assert payload["services"][0]["car_url"] == f"/preview/services/{service_id}/"

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
    by_name = client.get(f"/preview/services/{service_id}/index.html")
    assert by_name.status_code == 200
    assert by_name.text == opened.text


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
    assert client.get(f"/preview/services/{service_id}/").text == "home"
    asset = client.get(f"/preview/services/{service_id}/assets/app.txt")
    assert asset.status_code == 200
    assert asset.text == "asset"


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
    service_id = created.json()["service"]["service_id"]

    opened = client.get(f"/preview/services/{service_id}/")

    assert opened.status_code == 403


def test_preview_static_workspace_scope_does_not_expand_allowed_roots(
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
    assert created.status_code == 200
    service_id = created.json()["service"]["service_id"]

    opened = client.get(f"/preview/services/{service_id}/")

    assert opened.status_code == 403


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
            "path": "/base/nested/path?q=one",
            "body": "payload",
            "header": "kept",
            "authorization": None,
            "cookie": None,
        }
    finally:
        server.shutdown()
        server.server_close()


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
            assert websocket.receive_text() == "path=/base/nested/socket?client=abc"
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

        stopped = client.post(f"/hub/services/{service_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["service"]["status"] == "stopped"
    finally:
        if process_is_active(pid):
            client.post(f"/hub/services/{service_id}/stop")

    teardown = client.post(f"/hub/services/{service_id}/teardown")
    assert teardown.status_code == 200
    assert teardown.json()["deleted"] is True


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
