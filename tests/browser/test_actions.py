from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from codex_autorunner.browser.actions import execute_demo_manifest, load_demo_manifest


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><body><button>Sign in</button></body></html>"
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
        return {"role": "WebArea", "name": "Demo Page"}


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakeLocator:
    def __init__(self, page: "_FakePage", identity: str) -> None:
        self._page = page
        self._identity = identity

    def click(self, timeout: int) -> None:
        _ = timeout
        self._page.clicked.append(self._identity)
        if "fail-click" in self._identity:
            raise RuntimeError("click failed")
        if self._identity == "role:button:Sign in":
            self._page.url = f"{self._page.origin}/dashboard"

    def fill(self, value: str, timeout: int) -> None:
        _ = timeout
        self._page.filled[self._identity] = value

    def press(self, key: str, timeout: int) -> None:
        _ = timeout
        self._page.locator_presses.append((self._identity, key))

    def wait_for(self, *, state: str, timeout: int) -> None:
        _ = timeout
        if state != "visible":
            raise RuntimeError("unexpected state")
        self._page.waited_for.append(self._identity)


class _FakePage:
    def __init__(self, *, verify_http: bool = False) -> None:
        self._verify_http = verify_http
        self.url = ""
        self.origin = "http://127.0.0.1"
        self.clicked: list[str] = []
        self.filled: dict[str, str] = {}
        self.waited_for: list[str] = []
        self.locator_presses: list[tuple[str, str]] = []
        self.keyboard = _FakeKeyboard()
        self.accessibility = _FakeAccessibility()

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        if self._verify_http:
            with urllib.request.urlopen(url, timeout=2):
                pass
        self.url = url
        parsed = urlsplit(url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

    def get_by_role(self, role: str, **kwargs):  # type: ignore[no-untyped-def]
        name = kwargs.get("name") or ""
        return _FakeLocator(self, f"role:{role}:{name}")

    def get_by_label(self, label: str, exact: bool):  # type: ignore[no-untyped-def]
        _ = exact
        return _FakeLocator(self, f"label:{label}")

    def get_by_text(self, text: str, exact: bool):  # type: ignore[no-untyped-def]
        _ = exact
        return _FakeLocator(self, f"text:{text}")

    def get_by_test_id(self, test_id: str):  # type: ignore[no-untyped-def]
        return _FakeLocator(self, f"test_id:{test_id}")

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        return _FakeLocator(self, f"selector:{selector}")

    def wait_for_url(self, url: str, timeout: int) -> None:
        _ = timeout
        if self.url != url:
            raise RuntimeError(f"expected current URL {url!r}, got {self.url!r}")

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        Path(path).write_bytes(b"png")


def test_load_demo_manifest_rejects_unsupported_step(tmp_path: Path) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text(
        "version: 1\nsteps:\n  - action: nope\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported step action"):
        load_demo_manifest(manifest)


def test_execute_manifest_runs_click_fill_wait_and_artifacts(
    tmp_path: Path, fixture_server_url: str
) -> None:
    manifest_path = tmp_path / "demo.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: goto",
                "    url: /login",
                "  - action: fill",
                "    label: Email",
                "    value: user@example.com",
                "  - action: fill",
                "    label: Password",
                "    value: secret",
                "  - action: click",
                "    role: button",
                "    name: Sign in",
                "  - action: wait_for_url",
                "    url: /dashboard",
                "  - action: wait_for_text",
                "    text: Dashboard",
                "  - action: press",
                "    key: Enter",
                "  - action: wait_ms",
                "    ms: 1",
                "  - action: screenshot",
                "    output: step-capture.png",
                "  - action: snapshot_a11y",
                "    output: step-a11y.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    page = _FakePage(verify_http=True)
    manifest = load_demo_manifest(manifest_path)
    result = execute_demo_manifest(
        page=page,
        manifest=manifest,
        base_url=fixture_server_url,
        initial_path="/",
        out_dir=tmp_path / "outbox",
    )

    assert result.ok is True
    assert len(result.steps) == 10
    assert page.filled["label:Email"] == "user@example.com"
    assert page.filled["label:Password"] == "secret"
    assert "role:button:Sign in" in page.clicked
    assert page.waited_for == ["text:Dashboard"]
    assert page.keyboard.pressed == ["Enter"]

    screenshot = result.artifacts["step_9.screenshot"]
    a11y = result.artifacts["step_10.a11y_snapshot"]
    assert screenshot.exists()
    assert screenshot.suffix == ".png"
    assert json.loads(a11y.read_text(encoding="utf-8"))["role"] == "WebArea"


def test_execute_manifest_stops_on_step_failure(tmp_path: Path) -> None:
    manifest_path = tmp_path / "demo.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: goto",
                "    url: /login",
                "  - action: click",
                "    text: fail-click",
                "  - action: screenshot",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    page = _FakePage(verify_http=False)
    manifest = load_demo_manifest(manifest_path)
    result = execute_demo_manifest(
        page=page,
        manifest=manifest,
        base_url="http://127.0.0.1:9999",
        initial_path="/",
        out_dir=tmp_path / "outbox",
    )

    assert result.ok is False
    assert result.failed_step_index == 2
    assert len(result.steps) == 2
    assert result.steps[-1].ok is False
    assert result.error_message is not None
    assert "Step 2 (click) failed" in result.error_message
