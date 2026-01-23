import base64
from typing import Optional

import pytest

from codex_autorunner.web.middleware import AuthTokenMiddleware


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
        ("/static/app.js", False),
        ("/health", False),
        ("/cat", False),
        ("/hub/repos", True),
        ("/repos/demo", True),
        ("/repos/demo/", True),
        ("/repos/demo/static/app.js", False),
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
