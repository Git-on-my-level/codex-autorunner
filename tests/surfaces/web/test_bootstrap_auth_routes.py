from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.services.browser_auth import (
    BOOTSTRAP_TOKEN_RELATIVE_PATH,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    BrowserAuthStore,
)


def _read_bootstrap_token(hub_root: Path) -> str:
    return (
        (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).read_text(encoding="utf-8").strip()
    )


def test_remote_hub_bootstrap_claim_creates_http_only_session(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))

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


def test_bootstrap_token_is_single_use(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))
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
        f'{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - "*"\n',
        encoding="utf-8",
    )
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))

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
    assert (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()


def test_base_path_remote_hub_bootstrap_claim_reaches_token_validation_from_proxy_origin(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    config_path = hub_root / ".codex-autorunner/config.yml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f'{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n  allowed_hosts:\n    - "*"\n',
        encoding="utf-8",
    )
    client = TestClient(
        create_hub_app(hub_root, base_path="/car", endpoint_host="0.0.0.0")
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
    config_path.write_text(f"{text}\nserver:\n  auth_token_env: CAR_TEST_TOKEN\n")
    monkeypatch.setenv("CAR_TEST_TOKEN", "api-secret")
    client = TestClient(create_hub_app(hub_root, endpoint_host="0.0.0.0"))

    denied = client.get("/hub/repos")
    allowed = client.get("/hub/repos", headers={"Authorization": "Bearer api-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert (hub_root / BOOTSTRAP_TOKEN_RELATIVE_PATH).exists()


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
