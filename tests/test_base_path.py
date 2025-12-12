import anyio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_autorunner.server import BasePathRouterMiddleware


def _build_app():
    app = FastAPI()

    @app.get("/api/ping")
    def ping(request: Request):
        return {
            "path": request.scope.get("path"),
            "root_path": request.scope.get("root_path"),
        }

    return app


def test_redirects_requests_missing_base_prefix():
    app = BasePathRouterMiddleware(_build_app(), "/car")
    client = TestClient(app)
    resp = client.get("/api/ping", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/car/api/ping"


def test_redirects_preserve_query_string():
    app = BasePathRouterMiddleware(_build_app(), "/car")
    client = TestClient(app)
    resp = client.get("/api/ping?foo=bar&nested=1", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/car/api/ping?foo=bar&nested=1"


def test_strips_base_and_sets_root_path():
    app = BasePathRouterMiddleware(_build_app(), "/car")
    client = TestClient(app)
    resp = client.get("/car/api/ping")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["path"] == "/api/ping"
    assert payload["root_path"] == "/car"


def test_respects_existing_root_path_with_base_prefix():
    captured = {}

    async def dummy_app(scope, receive, send):
        captured["path"] = scope.get("path")
        captured["root_path"] = scope.get("root_path")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    app = BasePathRouterMiddleware(dummy_app, "/car")

    async def _run():
        scope = {
            "type": "http",
            "path": "/api/ping",
            "root_path": "/car/repos/demo",
            "headers": [],
            "method": "GET",
            "scheme": "http",
            "http_version": "1.1",
            "query_string": b"",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return messages

    messages = anyio.run(_run)
    assert captured["path"] == "/api/ping"
    assert captured["root_path"] == "/car/repos/demo"
    assert messages and messages[0]["status"] == 200


def test_websocket_redirects_to_canonical_base():
    called = False

    async def dummy_app(scope, receive, send):
        nonlocal called
        called = True

    middleware = BasePathRouterMiddleware(dummy_app, "/car")

    async def _run():
        scope = {
            "type": "websocket",
            "path": "/api/terminal",
            "root_path": "",
            "headers": [],
            "query_string": b"",
            "scheme": "ws",
        }

        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    messages = anyio.run(_run)
    assert not called
    assert messages
    assert messages[0]["status"] == 308
    headers = dict(messages[0].get("headers") or [])
    assert headers.get(b"location") == b"/car/api/terminal"
