import base64
from typing import Optional

import pytest

from codex_autorunner.surfaces.web.middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
)


def _scope(path: str, root_path: str = "") -> dict:
    return {
        "type": "http",
        "path": path,
        "root_path": root_path,
        "headers": [],
        "query_string": b"",
    }


def _ws_scope(headers: Optional[list[tuple[bytes, bytes]]] = None) -> dict:
    return {
        "type": "websocket",
        "path": "/api/terminal",
        "root_path": "",
        "headers": headers or [],
        "query_string": b"",
    }


@pytest.mark.parametrize(
    ("path", "requires_auth"),
    [
        ("/", False),
        ("/_app/version.json", False),
        ("/health", False),
        ("/cat", False),
        ("/hub/repos", True),
        ("/repos/demo", True),
        ("/repos/demo/", True),
        ("/repos/demo/static/assets/app.js", True),
        ("/repos/demo/ws", True),
    ],
)
def test_auth_middleware_public_allowlist(path: str, requires_auth: bool) -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    assert middleware._requires_auth(_scope(path)) is requires_auth


def test_auth_middleware_respects_base_path() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token", base_path="/car")
    assert middleware._requires_auth(_scope("/car/health")) is False
    assert middleware._requires_auth(_scope("/car/hub/repos")) is True


@pytest.mark.parametrize("path", ["/services", "/preview/p/token/index.html"])
def test_base_path_router_redirects_preview_and_services_prefixes(path: str) -> None:
    middleware = BasePathRouterMiddleware(lambda *_: None, base_path="/car")

    assert middleware._should_redirect(path, "") is True
    assert middleware._should_redirect(f"/car{path}", "") is False


def test_auth_middleware_extracts_ws_protocol_token() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    scope = _ws_scope(
        headers=[(b"sec-websocket-protocol", b"chat, car-token.secret , v2")]
    )
    assert middleware._extract_ws_protocol_token(scope) == "secret"


def test_auth_middleware_extracts_ws_protocol_b64_token() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    raw = "token/with=chars"
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    scope = _ws_scope(
        headers=[
            (b"sec-websocket-protocol", f"car-token-b64.{encoded}".encode("ascii"))
        ]
    )
    assert middleware._extract_ws_protocol_token(scope) == raw


def test_auth_middleware_rejects_query_token_for_http_routes() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    scope = _scope("/api/repo/health")
    scope["query_string"] = b"token=secret"
    assert middleware._extract_query_token(scope) is None


def test_auth_middleware_keeps_query_token_for_legacy_websockets() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    scope = _ws_scope()
    scope["query_string"] = b"token=secret"
    assert middleware._extract_query_token(scope) == "secret"


def test_auth_middleware_accepts_browser_session_cookie() -> None:
    middleware = AuthTokenMiddleware(
        lambda *_: None,
        token=None,
        session_validator=lambda token: token == "session-secret",
    )
    scope = _scope("/hub/repos")
    scope["headers"] = [(b"cookie", b"car_session=session-secret")]
    assert middleware._extract_session_cookie(scope) == "session-secret"
    assert middleware.session_validator(middleware._extract_session_cookie(scope))


def test_auth_middleware_hosted_mode_ignores_session_cookie() -> None:
    middleware = AuthTokenMiddleware(
        lambda *_: None,
        token="hub-secret",
        session_validator=lambda token: token == "session-secret",
        allow_session_auth=False,
    )
    scope = _scope("/hub/repos")
    scope["headers"] = [(b"cookie", b"car_session=session-secret")]

    assert middleware._extract_header_token(scope) is None
    assert middleware._extract_session_cookie(scope) == "session-secret"
    assert middleware.allow_session_auth is False


def test_auth_middleware_preview_capability_route_is_public() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="hub-secret")

    assert middleware._requires_auth(_scope("/preview/p/token/index.html")) is False
    assert middleware._requires_auth(_scope("/preview/services/svc_base123/")) is True
