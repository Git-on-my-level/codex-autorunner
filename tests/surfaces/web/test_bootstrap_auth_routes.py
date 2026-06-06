from __future__ import annotations

import json
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.services.browser_auth import (
    BOOTSTRAP_TOKEN_MAX_AGE_SECONDS,
    BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH,
    BOOTSTRAP_TOKEN_RELATIVE_PATH,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    BrowserAuthStore,
)


def _read_bootstrap_token(hub_root: Path) -> str:
    return (
        (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).read_text(encoding="utf-8").strip()
    )


def _set_browser_auth_cookie_secure(hub_root: Path, value: str) -> None:
    config_path = hub_root / CONFIG_FILENAME
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f"{text}\nbrowser_auth:\n  cookie_secure: {value}\n",
        encoding="utf-8",
    )


def _set_server_allowed_hosts(hub_root: Path, *hosts: str) -> None:
    config_path = hub_root / CONFIG_FILENAME
    text = config_path.read_text(encoding="utf-8")
    rendered_hosts = "\n".join(f"    - {host}" for host in hosts)
    config_path.write_text(
        f"{text}\nserver:\n  allowed_hosts:\n{rendered_hosts}\n",
        encoding="utf-8",
    )


def test_remote_hub_bootstrap_claim_creates_http_only_session(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )

    assert client.get("/health").status_code == 200
    assert client.get("/hub/repos").status_code == 401

    page = client.get("/auth/bootstrap")
    assert page.status_code == 200
    assert "/auth/bootstrap#token=..." in page.text

    token = _read_bootstrap_token(hub_root)
    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert claim.status_code == 200
    cookie = claim.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert not (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()
    session_token = cookie.split(f"{SESSION_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    assert (
        client.get(
            "/hub/repos", headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_token}"}
        ).status_code
        == 200
    )


def test_remote_hub_bootstrap_claim_rejects_plain_http(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))
    token = _read_bootstrap_token(hub_root)

    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert claim.status_code == 400
    assert "set-cookie" not in claim.headers
    assert _read_bootstrap_token(hub_root) == token


def test_remote_hub_bootstrap_claim_rejects_forged_https_origin_over_http(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(hub_root, "public.example")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="http://public.example",
    )
    token = _read_bootstrap_token(hub_root)

    claim = client.post(
        "/auth/bootstrap/claim",
        json={"token": token},
        headers={"origin": "https://public.example"},
    )

    assert claim.status_code == 400
    assert "set-cookie" not in claim.headers
    assert _read_bootstrap_token(hub_root) == token


def test_bootstrap_cookie_secure_auto_uses_https_request_scheme(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )
    token = _read_bootstrap_token(hub_root)

    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert claim.status_code == 200
    assert "Secure" in claim.headers["set-cookie"]


def test_bootstrap_cookie_secure_auto_uses_https_proxy_origin(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(
        hub_root,
        "agent-dev-01.tailnet-name.ts.net:4173",
    )
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://agent-dev-01.tailnet-name.ts.net:4173",
    )
    token = _read_bootstrap_token(hub_root)

    claim = client.post(
        "/auth/bootstrap/claim",
        json={"token": token},
        headers={
            "host": "agent-dev-01.tailnet-name.ts.net:4173",
            "origin": "https://agent-dev-01.tailnet-name.ts.net:4173",
        },
    )

    assert claim.status_code == 200
    assert "Secure" in claim.headers["set-cookie"]


def test_bootstrap_cookie_secure_auto_ignores_mismatched_https_origin(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(
        hub_root,
        "agent-dev-01.tailnet-name.ts.net:4173",
    )
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))
    token = _read_bootstrap_token(hub_root)

    claim = client.post(
        "/auth/bootstrap/claim",
        json={"token": token},
        headers={
            "host": "agent-dev-01.tailnet-name.ts.net:4173",
            "origin": "https://different.example",
        },
    )

    assert claim.status_code == 403
    assert "set-cookie" not in claim.headers
    assert _read_bootstrap_token(hub_root) == token


def test_bootstrap_cookie_secure_true_sets_secure_cookie(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_browser_auth_cookie_secure(hub_root, "true")
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )
    token = _read_bootstrap_token(hub_root)

    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert claim.status_code == 200
    assert "Secure" in claim.headers["set-cookie"]


def test_remote_bootstrap_cookie_secure_false_is_rejected(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_browser_auth_cookie_secure(hub_root, "false")
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )
    token = _read_bootstrap_token(hub_root)

    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert claim.status_code == 400
    assert "set-cookie" not in claim.headers
    assert _read_bootstrap_token(hub_root) == token


def test_health_reports_control_plane_schema_and_compatibility(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    control_plane = payload["orchestration"]["control_plane"]
    assert control_plane["db_exists"] is True
    assert (
        control_plane["schema_generation"] == control_plane["target_schema_generation"]
    )
    assert control_plane["compatibility"]["status"] == "compatible"
    assert (
        control_plane["metadata"]["schema_generation"]
        == control_plane["schema_generation"]
    )


def test_bootstrap_token_is_single_use(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    _set_server_allowed_hosts(hub_root, "testserver")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )
    token = _read_bootstrap_token(hub_root)

    first = client.post("/auth/bootstrap/claim", json={"token": token})
    second = client.post("/auth/bootstrap/claim", json={"token": token})

    assert first.status_code == 200
    assert second.status_code == 401


def test_remote_hub_bootstrap_claim_reaches_token_validation_from_proxy_origin(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    config_path = hub_root / ".codex-autorunner/config.yml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f"{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - 4173-glad-arch-jvr2.pad.dev\n",
        encoding="utf-8",
    )
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://4173-glad-arch-jvr2.pad.dev",
    )

    claim = client.post(
        "/auth/bootstrap/claim",
        json={"token": "deliberately-wrong"},
        headers={
            "host": "4173-glad-arch-jvr2.pad.dev",
            "origin": "https://4173-glad-arch-jvr2.pad.dev",
        },
    )

    assert claim.status_code == 401
    assert claim.json() == {"detail": "Invalid bootstrap token"}
    assert "set-cookie" not in claim.headers
    assert (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()


def test_base_path_remote_hub_bootstrap_claim_reaches_token_validation_from_proxy_origin(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    config_path = hub_root / ".codex-autorunner/config.yml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f"{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - 4173-glad-arch-jvr2.pad.dev\n",
        encoding="utf-8",
    )
    client = TestClient(
        create_hub_app(hub_root, base_path="/car", endpoint_host="0.0.0.0"),
        base_url="https://4173-glad-arch-jvr2.pad.dev",
    )

    claim = client.post(
        "/car/auth/bootstrap/claim",
        json={"token": "deliberately-wrong"},
        headers={
            "host": "4173-glad-arch-jvr2.pad.dev",
            "origin": "https://4173-glad-arch-jvr2.pad.dev",
        },
    )

    assert claim.status_code == 401
    assert claim.json() == {"detail": "Invalid bootstrap token"}
    assert (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()


def test_bearer_token_auth_still_works_with_bootstrap_routes(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    config_path = hub_root / ".codex-autorunner/config.yml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f"{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - testserver\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAR_TEST_TOKEN", "api-secret")
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))

    denied = client.get("/hub/repos")
    allowed = client.get("/hub/repos", headers={"Authorization": "Bearer api-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()


def test_hosted_bearer_bootstrap_claim_returns_expiring_header_token(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    config_path = hub_root / ".codex-autorunner/config.yml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f"{text}\nauth:\n  mode: hosted_bearer\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - testserver\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAR_TEST_TOKEN", "api-secret")
    client = TestClient(
        create_hub_app(hub_root, endpoint_host="0.0.0.0"),
        base_url="https://testserver",
    )

    denied = client.get("/hub/repos")
    token = _read_bootstrap_token(hub_root)
    claim = client.post("/auth/bootstrap/claim", json={"token": token})

    assert denied.status_code == 401
    assert claim.status_code == 200
    assert "set-cookie" not in claim.headers
    payload = claim.json()
    access_token = payload["access_token"]
    assert payload["token_type"] == "bearer"
    assert payload["expires_in"] == SESSION_MAX_AGE_SECONDS
    assert isinstance(payload["expires_at"], int)
    assert not (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()
    assert (
        client.get(
            "/hub/repos", headers={"Authorization": f"Bearer {access_token}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/hub/repos",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={access_token}"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/hub/repos", headers={"Authorization": "Bearer api-secret"}
        ).status_code
        == 200
    )


def test_browser_session_validation_enforces_server_side_expiry(
    tmp_path: Path,
) -> None:
    now = 1_000.0
    store = BrowserAuthStore(tmp_path, now=lambda: now)
    _, bootstrap_token = store.ensure_bootstrap_token()

    claim = store.claim_bootstrap_token(bootstrap_token)
    assert claim is not None
    token = claim.session_token

    assert store.validate_session_token(token) is True

    now += SESSION_MAX_AGE_SECONDS + 1

    assert store.validate_session_token(token) is False
    data = store._read_store()
    assert data["sessions"] == []


def test_bootstrap_token_files_are_private(tmp_path: Path) -> None:
    store = BrowserAuthStore(tmp_path)

    token_path, _ = store.ensure_bootstrap_token()
    metadata_path = tmp_path / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600


def test_bootstrap_token_expires_and_rotates(tmp_path: Path) -> None:
    now = 1_000.0
    store = BrowserAuthStore(tmp_path, now=lambda: now)
    token_path, token = store.ensure_bootstrap_token()
    metadata_path = tmp_path / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH
    metadata_path.write_text(
        json.dumps({"issued_at": now - BOOTSTRAP_TOKEN_MAX_AGE_SECONDS - 1}),
        encoding="utf-8",
    )

    assert store.claim_bootstrap_token(token) is None

    _, rotated = store.ensure_bootstrap_token()
    assert rotated != token
    assert token_path.read_text(encoding="utf-8").strip() == rotated


def test_bootstrap_token_rejects_non_finite_metadata(tmp_path: Path) -> None:
    store = BrowserAuthStore(tmp_path)
    _, token = store.ensure_bootstrap_token()
    metadata_path = tmp_path / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH
    metadata_path.write_text('{"issued_at": NaN}', encoding="utf-8")

    assert store.claim_bootstrap_token(token) is None


def test_bootstrap_token_rejects_far_future_metadata(tmp_path: Path) -> None:
    now = 1_000.0
    store = BrowserAuthStore(tmp_path, now=lambda: now)
    _, token = store.ensure_bootstrap_token()
    metadata_path = tmp_path / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH
    metadata_path.write_text(json.dumps({"issued_at": now + 120}), encoding="utf-8")

    assert store.claim_bootstrap_token(token) is None


def test_auth_files_replace_symlinks_without_following(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do-not-overwrite", encoding="utf-8")
    token_path = tmp_path / BOOTSTRAP_TOKEN_RELATIVE_PATH
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.symlink_to(target)

    store = BrowserAuthStore(tmp_path)
    store.ensure_bootstrap_token()

    assert target.read_text(encoding="utf-8") == "do-not-overwrite"
    assert not token_path.is_symlink()


def test_browser_session_can_be_revoked(tmp_path: Path) -> None:
    store = BrowserAuthStore(tmp_path)
    _, bootstrap_token = store.ensure_bootstrap_token()
    claim = store.claim_bootstrap_token(bootstrap_token)
    assert claim is not None

    assert store.validate_session_token(claim.session_token) is True
    assert store.revoke_session_token(claim.session_token) is True
    assert store.validate_session_token(claim.session_token) is False
