from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.locks import process_is_active
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
    assert started_service["status"] == "running"
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
