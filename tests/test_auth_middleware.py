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


@pytest.mark.parametrize(
    ("path", "requires_auth"),
    [
        ("/", False),
        ("/static/app.js", False),
        ("/health", False),
        ("/cat", False),
        ("/api/state", True),
        ("/hub/repos", True),
        ("/repos/demo", True),
        ("/repos/demo/", True),
        ("/repos/demo/static/app.js", False),
        ("/repos/demo/api/state", True),
        ("/repos/demo/ws", True),
    ],
)
def test_auth_middleware_public_allowlist(path: str, requires_auth: bool) -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token")
    assert middleware._requires_auth(_scope(path)) is requires_auth


def test_auth_middleware_respects_base_path() -> None:
    middleware = AuthTokenMiddleware(lambda *_: None, token="token", base_path="/car")
    assert middleware._requires_auth(_scope("/car/health")) is False
    assert middleware._requires_auth(_scope("/car/api/state")) is True
