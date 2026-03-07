from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.browser.models import Viewport
from codex_autorunner.browser.runtime import BrowserRuntime, build_navigation_url


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><head><title>Fixture Page</title></head><body>ok</body></html>"
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
        return {"role": "WebArea", "name": "Fixture Page"}


class _FakePage:
    def __init__(self, *, fail_on: Optional[str] = None, verify_http: bool = False):
        self._fail_on = fail_on
        self._verify_http = verify_http
        self.closed = False
        self.url = ""
        self.accessibility = _FakeAccessibility()

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        if self._fail_on == "goto":
            raise RuntimeError("goto failed")
        if self._verify_http:
            with urllib.request.urlopen(url, timeout=2):
                pass
        self.url = url

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        if self._fail_on == "screenshot":
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"png-bytes")

    def pdf(self, *, path: str) -> None:
        if self._fail_on == "pdf":
            raise RuntimeError("pdf failed")
        Path(path).write_bytes(b"%PDF-1.4")

    def title(self) -> str:
        return "Fixture Page"

    def content(self) -> str:
        return "<html><body>fixture</body></html>"

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext):
        self._context = context
        self.closed = False

    def new_context(self, *, viewport):  # type: ignore[no-untyped-def]
        assert viewport["width"] > 0
        assert viewport["height"] > 0
        return self._context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser

    def launch(self, *, headless: bool) -> _FakeBrowser:
        assert headless is True
        return self._browser


class _FakePlaywright:
    def __init__(self, page: _FakePage):
        self.page = page
        self.context = _FakeContext(page)
        self.browser = _FakeBrowser(self.context)
        self.chromium = _FakeChromium(self.browser)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_runtime_screenshot_png_writes_artifact(
    tmp_path: Path, fixture_server_url: str
) -> None:
    fake = _FakePlaywright(_FakePage(verify_http=True))
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    out_dir = tmp_path / "outbox"
    result = runtime.capture_screenshot(
        base_url=fixture_server_url,
        out_dir=out_dir,
        viewport=Viewport(width=1024, height=768),
        output_format="png",
    )

    assert result.ok is True
    capture_path = result.artifacts["capture"]
    assert capture_path.exists()
    assert capture_path.suffix == ".png"
    assert capture_path.read_bytes() == b"png-bytes"


def test_runtime_screenshot_pdf_writes_artifact(
    tmp_path: Path, fixture_server_url: str
) -> None:
    fake = _FakePlaywright(_FakePage(verify_http=True))
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    result = runtime.capture_screenshot(
        base_url=fixture_server_url,
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1280, height=720),
        output_format="pdf",
    )

    assert result.ok is True
    capture_path = result.artifacts["capture"]
    assert capture_path.suffix == ".pdf"
    assert capture_path.read_bytes().startswith(b"%PDF")


def test_runtime_observe_writes_snapshot_and_metadata(
    tmp_path: Path, fixture_server_url: str
) -> None:
    fake = _FakePlaywright(_FakePage(verify_http=True))
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    result = runtime.capture_observe(
        base_url=fixture_server_url,
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1200, height=800),
    )

    assert result.ok is True
    snapshot_path = result.artifacts["snapshot"]
    metadata_path = result.artifacts["metadata"]
    locator_refs_path = result.artifacts["locator_refs"]
    run_manifest_path = result.artifacts["run_manifest"]
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    locator_refs_payload = json.loads(locator_refs_path.read_text(encoding="utf-8"))
    run_manifest_payload = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    assert isinstance(snapshot_payload, dict)
    assert metadata_payload["schema_version"] == 1
    assert metadata_payload["title"] == "Fixture Page"
    assert metadata_payload["captured_url"].startswith(fixture_server_url)
    assert metadata_payload["viewport"] == {"width": 1200, "height": 800}
    assert locator_refs_payload["schema_version"] == 1
    assert isinstance(locator_refs_payload["refs"], list)
    assert run_manifest_payload["schema_version"] == 1
    assert run_manifest_payload["mode"] == "observe"
    assert run_manifest_payload["runtime_profile"]["deterministic"] is True
    assert run_manifest_payload["runtime_profile"]["accessibility_first"] is True
    assert run_manifest_payload["runtime_profile"]["prompt_driven"] is False


def test_runtime_failure_still_closes_page_context_browser(tmp_path: Path) -> None:
    fake = _FakePlaywright(_FakePage(fail_on="goto"))
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    result = runtime.capture_screenshot(
        base_url="http://127.0.0.1:1",
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1280, height=720),
    )

    assert result.ok is False
    assert result.error_type == "BrowserNavigationError"
    assert fake.page.closed is True
    assert fake.context.closed is True
    assert fake.browser.closed is True
    assert fake.stopped is True


def test_build_navigation_url_preserves_base_path_and_query_when_path_omitted() -> None:
    assert (
        build_navigation_url("http://127.0.0.1:3000/settings?tab=profile")
        == "http://127.0.0.1:3000/settings?tab=profile"
    )


def test_build_navigation_url_overrides_path_and_query_when_path_provided() -> None:
    assert (
        build_navigation_url(
            "http://127.0.0.1:3000/settings?tab=profile", "/dashboard?tab=overview"
        )
        == "http://127.0.0.1:3000/dashboard?tab=overview"
    )
