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

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
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

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_static1",
                    "name": "Site",
                    "kind": "static_file",
                    "status": "registered",
                    "preview_url": "/preview/p/tok_static1/",
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
        "path": str(static_file.resolve()),
        "name": "Site",
        "kind": "static_file",
        "scope_links": [
            {"kind": "repo", "id": "car"},
            {"kind": "workspace", "path": str(tmp_path)},
        ],
        "created_by": "cli",
    }
    assert "registered: svc_static1 registered" in result.output
    assert "/preview/p/tok_static1/" in result.output


def test_services_start_managed_payload_includes_command_port_env_and_start(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
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
        "env_policy": "minimal",
        "port_policy": {"mode": "preferred", "port": 5173},
        "health_check": {"type": "http", "path": "/"},
        "scope_links": [{"kind": "ticket", "id": "tkt_1"}],
        "created_by": "cli",
        "auto_start_on_hub_start": False,
        "start": True,
    }


def test_services_register_managed_payload_does_not_start(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_managed1",
                    "name": "Dev",
                    "kind": "managed_command",
                    "status": "registered",
                    "exposure": {"car_url": "/preview/services/svc_managed1/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "register-managed",
            "--path",
            str(hub_root),
            "--name",
            "Dev",
            "--cwd",
            str(tmp_path),
            "--auto-port",
            "--",
            "npm",
            "run",
            "dev",
        ],
    )

    assert result.exit_code == 0
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services/managed"
    assert calls[0]["json"]["start"] is False
    assert calls[0]["json"]["port_policy"] == {"mode": "auto"}


def test_services_resolves_relative_static_and_cwd_paths_client_side(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    shell_cwd = tmp_path / "shell"
    static_dir = shell_cwd / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("ok", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service": {
                    "service_id": "svc_static1",
                    "name": "Site",
                    "kind": "static_dir",
                    "status": "registered",
                    "exposure": {"car_url": "/preview/services/svc_static1/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)
    monkeypatch.chdir(shell_cwd)

    result = runner.invoke(
        app,
        [
            "services",
            "register-static",
            "./dist",
            "--path",
            str(hub_root),
            "--name",
            "Site",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["json"]["path"] == str(static_dir.resolve())

    calls.clear()
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
            ".",
            "--",
            "python",
            "-m",
            "http.server",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["json"]["cwd"] == str(shell_cwd.resolve())


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

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
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


def test_services_issue_and_revoke_link_use_capability_routes(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        parsed = urlsplit(str(url))
        if parsed.path.endswith("/revoke"):
            return _mock_response({"service_id": "svc_abc123", "revoked": 2})
        return _mock_response(
            {
                "service_id": "svc_abc123",
                "preview_url": "/preview/p/tok_123/",
                "expires_at": 123.0,
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    issued = runner.invoke(
        app,
        [
            "services",
            "issue-link",
            "svc_abc123",
            "--path",
            str(hub_root),
            "--ttl",
            "24h",
            "--json",
        ],
    )
    revoked = runner.invoke(
        app,
        [
            "services",
            "revoke-link",
            "svc_abc123",
            "--all",
            "--path",
            str(hub_root),
        ],
    )

    assert issued.exit_code == 0
    assert json.loads(issued.output)["preview_url"] == "/preview/p/tok_123/"
    assert (
        urlsplit(str(calls[0]["url"])).path == "/hub/services/svc_abc123/preview-token"
    )
    assert urlsplit(str(calls[0]["url"])).query == "ttl=86400"
    assert revoked.exit_code == 0
    assert "revoked: 2" in revoked.output
    assert (
        urlsplit(str(calls[1]["url"])).path
        == "/hub/services/svc_abc123/preview-token/revoke"
    )


def test_services_open_json_uses_absolute_public_base_url(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "service_id": "svc_abc123",
                "preview_url": "/preview/p/tok_123/",
                "expires_at": 123.0,
            }
        )

    monkeypatch.setenv("CAR_PREVIEW_PUBLIC_BASE_URL", "https://car.example.test/base")
    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        ["services", "open", "svc_abc123", "--path", str(hub_root), "--json"],
    )

    assert result.exit_code == 0
    assert (
        json.loads(result.output)["preview_url"]
        == "https://car.example.test/base/preview/p/tok_123/"
    )
    assert calls[0]["method"] == "POST"
    assert (
        urlsplit(str(calls[0]["url"])).path == "/hub/services/svc_abc123/preview-token"
    )


def test_services_open_direct_uses_diagnostic_service_url(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response(
            {
                "read_model": {
                    "service_id": "svc_abc123",
                    "exposure": {"car_url": "/preview/services/svc_abc123/"},
                }
            }
        )

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "open",
            "svc_abc123",
            "--path",
            str(hub_root),
            "--direct",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["preview_url"] == "/preview/services/svc_abc123/"
    assert calls[0]["method"] == "GET"
    assert urlsplit(str(calls[0]["url"])).path == "/hub/services/svc_abc123"


def test_services_logs_sends_tail_only(tmp_path: Path, monkeypatch) -> None:
    hub_root = _hub_root(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "json": json})
        return _mock_response({"service_id": "svc_abc123", "tail": 50, "text": "ok\n"})

    monkeypatch.setattr("httpx.request", _fake_request)

    result = runner.invoke(
        app,
        [
            "services",
            "logs",
            "svc_abc123",
            "--path",
            str(hub_root),
            "--tail",
            "50",
        ],
    )

    assert result.exit_code == 0
    assert urlsplit(str(calls[0]["url"])).query == "tail=50"


def test_services_health_prints_hub_health_schema(tmp_path: Path, monkeypatch) -> None:
    hub_root = _hub_root(tmp_path)

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
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

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=True
    ):  # type: ignore[no-untyped-def]
        parsed = urlsplit(str(url))
        response = client.request(method, f"{parsed.path}?{parsed.query}", json=json)
        return httpx.Response(
            response.status_code,
            json=response.json(),
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("httpx.request", _fake_request)
    monkeypatch.setenv("CAR_PREVIEW_PUBLIC_BASE_URL", "https://car.example.test/base")

    listed = runner.invoke(app, ["services", "list", "--path", str(hub_root), "--json"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.output)
    assert listed_payload["services"][0]["service_id"] == service_id
    assert listed_payload["read_model"]["services"][0]["preview_url"] is None
    assert (
        listed_payload["read_model"]["services"][0]["preview_url_status"]
        == "not_issued"
    )

    detail = runner.invoke(
        app, ["services", "get", service_id, "--path", str(hub_root), "--json"]
    )
    assert detail.exit_code == 0
    detail_payload = json.loads(detail.output)
    assert detail_payload["service"]["name"] == "Static preview"
    assert detail_payload["read_model"]["preview_url"] is None
    assert detail_payload["read_model"]["preview_url_status"] == "not_issued"
