from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from codex_autorunner.browser.models import Viewport
from codex_autorunner.browser.runtime import BrowserRuntime


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><head><title>Fixture</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs) -> None:
        return


@pytest.fixture()
def fixture_server_url() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _FakeAccessibility:
    def snapshot(self):  # type: ignore[no-untyped-def]
        return {"role": "WebArea", "name": "Fixture"}


class _FakeKeyboard:
    def press(self, _key: str) -> None:
        return


class _FakeLocator:
    def click(self, timeout: int) -> None:
        _ = timeout

    def fill(self, value: str, timeout: int) -> None:
        _ = value, timeout

    def press(self, key: str, timeout: int) -> None:
        _ = key, timeout

    def wait_for(self, *, state: str, timeout: int) -> None:
        _ = state, timeout


class _FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.url = ""
        self.accessibility = _FakeAccessibility()
        self.keyboard = _FakeKeyboard()

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        with urllib.request.urlopen(url, timeout=2):
            pass
        self.url = url

    def title(self) -> str:
        return "Fixture"

    def content(self) -> str:
        return '<html><body data-testid="root">ok</body></html>'

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        Path(path).write_bytes(b"png")

    def get_by_role(self, role: str, **kwargs):  # type: ignore[no-untyped-def]
        _ = role, kwargs
        return _FakeLocator()

    def get_by_label(self, label: str, exact: bool):  # type: ignore[no-untyped-def]
        _ = label, exact
        return _FakeLocator()

    def get_by_text(self, text: str, exact: bool):  # type: ignore[no-untyped-def]
        _ = text, exact
        return _FakeLocator()

    def get_by_test_id(self, test_id: str):  # type: ignore[no-untyped-def]
        _ = test_id
        return _FakeLocator()

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        _ = selector
        return _FakeLocator()

    def wait_for_url(self, _url: str, timeout: int) -> None:
        _ = timeout

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.context = _FakeContext(page)
        self.closed = False

    def new_context(self, *, viewport):  # type: ignore[no-untyped-def]
        assert viewport["width"] > 0
        assert viewport["height"] > 0
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def launch(self, *, headless: bool) -> _FakeBrowser:
        assert headless is True
        return self._browser


class _FakePlaywright:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.browser = _FakeBrowser(self.page)
        self.chromium = _FakeChromium(self.browser)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_observe_then_demo_against_same_fixture_target(
    tmp_path: Path, fixture_server_url: str
) -> None:
    runtime = BrowserRuntime(playwright_loader=lambda: _FakePlaywright())

    observe_result = runtime.capture_observe(
        base_url=fixture_server_url,
        path="/",
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1200, height=800),
    )
    assert observe_result.ok is True
    assert observe_result.artifacts["snapshot"].exists()
    assert observe_result.artifacts["run_manifest"].exists()

    script = tmp_path / "demo.yaml"
    script.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: goto",
                "    url: /",
                "  - action: wait_for_text",
                "    text: ok",
                "  - action: screenshot",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    demo_result = runtime.capture_demo(
        base_url=fixture_server_url,
        path="/",
        script_path=script,
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1200, height=800),
    )

    assert demo_result.ok is True
    assert demo_result.artifacts["summary"].exists()
    summary_payload = json.loads(demo_result.artifacts["summary"].read_text("utf-8"))
    assert summary_payload["target_url"].startswith(fixture_server_url)
    assert observe_result.target_url == demo_result.target_url
