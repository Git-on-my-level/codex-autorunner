from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.server import create_hub_app

runner = CliRunner()


def _hub_root(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    return hub_root


def _mock_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "http://hub"))


def test_services_list_json_requests_hub_services_with_scope(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "services": [
                    {
                        "service_id": "svc_abc123",
                        "name": "Frontend",
                        "kind": "loopback_url",
                        "status": "healthy",
                        "exposure": {"car_url": "/preview/services/svc_abc123/"},
                    }
                ],
                "read_model": {"services": [], "counts": {"total": 1}},
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "list",
            "--path",
            str(hub_root),
            "--scope",
            "repo:car",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["services"][0]["service_id"] == "svc_abc123"
    assert calls[0]["method"] == "GET"
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services"
    assert urlsplit(str(calls[0]["url"])).query == "scope=repo%3Acar"
    assert calls[0]["json"] is None


def test_services_register_static_payload_normalizes_kind_and_scope(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    static_file = tmp_path / "index.html"
    static_file.write_text("<h1>hi</h1>", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_static1",
                    "name": "Site",
                    "kind": "static_file",
                    "status": "registered",
                    "exposure": {"car_url": "/preview/services/svc_static1/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "register-static",
            str(static_file),
            "--path",
            str(hub_root),
            "--name",
            "Site",
            "--kind",
            "static-file",
            "--scope",
            "repo:car",
            "--scope",
            f"workspace:{tmp_path}",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["method"] == "POST"
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services/static"
    assert calls[0]["json"] == {
        "path": str(static_file),
        "name": "Site",
        "kind": "static_file",
        "scope_links": [
            {"kind": "repo", "id": "car"},
            {"kind": "workspace", "path": str(tmp_path)},
        ],
        "created_by": "cli",
    }
    assert "registered: svc_static1 registered" in result.output


def test_services_start_managed_payload_includes_command_port_env_and_start(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_managed1",
                    "name": "Dev",
                    "kind": "managed_command",
                    "status": "healthy",
                    "exposure": {"car_url": "/preview/services/svc_managed1/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "start-managed",
            "--path",
            str(hub_root),
            "--name",
            "Dev",
            "--cwd",
            str(tmp_path),
            "--port",
            "5173",
            "--env",
            "HOST=127.0.0.1",
            "--scope",
            "ticket:tkt_1",
            "--",
            "npm",
            "run",
            "dev",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["method"] == "POST"
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services/managed"
    assert calls[0]["json"] == {
        "name": "Dev",
        "argv": ["npm", "run", "dev"],
        "cwd": str(tmp_path),
        "env": {"HOST": "127.0.0.1"},
        "port_policy": {"mode": "preferred", "port": 5173},
        "health_check": {"type": "http", "path": "/"},
        "scope_links": [{"kind": "ticket", "id": "tkt_1"}],
        "created_by": "cli",
        "auto_start_on_hub_start": False,
        "start": True,
    }


def test_services_kill_requires_force_attestation(tmp_path: Path) -> None:
    hub_root = _hub_root(tmp_path)

    result = runner.invoke(
        app,
        ["services", "kill", "svc_abc123", "--path", str(hub_root), "--force"],
    )

    assert result.exit_code == 1
    assert "requires --force-attestation" in result.output


def test_services_kill_sends_force_payload(tmp_path: Path, monkeypatch) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_abc123",
                    "name": "Dev",
                    "kind": "managed_command",
                    "status": "stopped",
                    "exposure": {"car_url": "/preview/services/svc_abc123/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "kill",
            "svc_abc123",
            "--path",
            str(hub_root),
            "--force",
            "--force-attestation",
            "user asked to terminate preview",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["method"] == "POST"
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services/svc_abc123/kill"
    assert calls[0]["json"] == {
        "force": True,
        "force_attestation": "user asked to terminate preview",
    }


def test_services_health_prints_hub_health_schema(tmp_path: Path, monkeypatch) -> None:
    hub_root = _hub_root(tmp_path)

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_abc123",
                    "name": "Dev",
                    "kind": "managed_command",
                    "status": "healthy",
                },
                "health": {"ok": True, "type": "http", "status_code": 200},
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        ["services", "health", "svc_abc123", "--path", str(hub_root)],
    )

    assert result.exit_code == 0
    assert (
        result.output.strip() == "svc_abc123 healthy ok=True type=http status_code=200"
    )


def test_services_list_and_get_against_test_hub_app(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    html = hub_root / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/services/static",
        json={"path": str(html), "name": "Static preview"},
    )
    service_id = created.json()["service"]["service_id"]

    def _fake_request(method, url, json=None, timeout=None, headers=None, follow_redirects=True):  # type: ignore[no-untyped-def]
        parsed = urlsplit(str(url))
        response = client.request(method, f"{parsed.path}?{parsed.query}", json=json)
        return httpx.Response(
            response.status_code,
            json=response.json(),
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    listed = runner.invoke(app, ["services", "list", "--path", str(hub_root), "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)["services"][0]["service_id"] == service_id

    detail = runner.invoke(
        app, ["services", "get", service_id, "--path", str(hub_root), "--json"]
    )
    assert detail.exit_code == 0
    assert json.loads(detail.output)["service"]["name"] == "Static preview"
