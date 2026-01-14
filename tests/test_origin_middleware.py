import anyio

from codex_autorunner.web.middleware import HostOriginMiddleware


async def _http_call(app, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def test_origin_rejects_mismatched_post():
    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = HostOriginMiddleware(dummy_app, ["localhost"], [])
    scope = {
        "type": "http",
        "path": "/api/run",
        "root_path": "",
        "headers": [
            (b"host", b"localhost:4173"),
            (b"origin", b"https://evil.example"),
        ],
        "method": "POST",
        "scheme": "http",
        "http_version": "1.1",
        "query_string": b"",
    }

    messages = anyio.run(_http_call, middleware, scope)
    assert messages[0]["status"] == 403


def test_origin_allows_no_origin_post():
    called = False

    async def dummy_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = HostOriginMiddleware(dummy_app, ["localhost"], [])
    scope = {
        "type": "http",
        "path": "/api/run",
        "root_path": "",
        "headers": [(b"host", b"localhost:4173")],
        "method": "POST",
        "scheme": "http",
        "http_version": "1.1",
        "query_string": b"",
    }

    messages = anyio.run(_http_call, middleware, scope)
    assert called is True
    assert messages[0]["status"] == 200


def test_websocket_rejects_mismatched_origin():
    called = False

    async def dummy_app(scope, receive, send):
        nonlocal called
        called = True

    middleware = HostOriginMiddleware(dummy_app, ["localhost"], [])

    async def _run():
        scope = {
            "type": "websocket",
            "path": "/api/terminal",
            "root_path": "",
            "headers": [
                (b"host", b"localhost:4173"),
                (b"origin", b"https://evil.example"),
            ],
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
    assert called is False
    assert messages[0]["type"] == "websocket.http.response.start"
    assert messages[0]["status"] == 403


def test_host_rejects_invalid_host():
    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = HostOriginMiddleware(dummy_app, ["localhost"], [])
    scope = {
        "type": "http",
        "path": "/api/run",
        "root_path": "",
        "headers": [(b"host", b"evil.example")],
        "method": "GET",
        "scheme": "http",
        "http_version": "1.1",
        "query_string": b"",
    }

    messages = anyio.run(_http_call, middleware, scope)
    assert messages[0]["status"] == 400
