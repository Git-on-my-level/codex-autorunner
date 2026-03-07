from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from codex_autorunner.browser.models import Viewport
from codex_autorunner.browser.runtime import BrowserRuntime


class _FakeAccessibility:
    def snapshot(self):  # type: ignore[no-untyped-def]
        return {"role": "WebArea", "name": "Demo"}


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakeVideo:
    def __init__(self, path: Path) -> None:
        self._path = path

    def path(self) -> str:
        return str(self._path)


class _FakeLocator:
    def __init__(self, page: "_DemoPage", identity: str) -> None:
        self._page = page
        self._identity = identity

    def click(self, timeout: int) -> None:
        _ = timeout
        if self._identity == "text:fail-click":
            raise RuntimeError("click failed")

    def fill(self, value: str, timeout: int) -> None:
        _ = value, timeout

    def press(self, key: str, timeout: int) -> None:
        _ = timeout
        self._page.keyboard.pressed.append(key)

    def wait_for(self, *, state: str, timeout: int) -> None:
        _ = state, timeout
        if self._identity == "text:missing-target":
            raise RuntimeError("locator not actionable")


class _DemoPage:
    def __init__(self, *, video_path: Optional[Path] = None) -> None:
        self.closed = False
        self.url = ""
        self.origin = "http://127.0.0.1"
        self.keyboard = _FakeKeyboard()
        self.accessibility = _FakeAccessibility()
        self.video = _FakeVideo(video_path) if video_path is not None else None

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
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
            raise RuntimeError(f"expected {url!r}, got {self.url!r}")

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        Path(path).write_bytes(b"png")

    def close(self) -> None:
        self.closed = True


class _FakeTracing:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.stop_with_path = False

    def start(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.started = True

    def stop(self, path: Optional[str] = None) -> None:
        self.stopped = True
        if path:
            self.stop_with_path = True
            Path(path).write_bytes(b"trace")


class _FakeContext:
    def __init__(self, page: _DemoPage, tracing: _FakeTracing) -> None:
        self._page = page
        self.tracing = tracing
        self.closed = False

    def new_page(self) -> _DemoPage:
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.last_context: Optional[_FakeContext] = None
        self.last_page: Optional[_DemoPage] = None

    def new_context(self, **kwargs):  # type: ignore[no-untyped-def]
        viewport = kwargs.get("viewport")
        assert viewport["width"] > 0
        assert viewport["height"] > 0
        video_dir_raw = kwargs.get("record_video_dir")
        video_path: Optional[Path] = None
        if isinstance(video_dir_raw, str):
            video_dir = Path(video_dir_raw)
            video_dir.mkdir(parents=True, exist_ok=True)
            video_path = video_dir / "context-video.webm"
            video_path.write_bytes(b"video")
        page = _DemoPage(video_path=video_path)
        tracing = _FakeTracing()
        context = _FakeContext(page, tracing)
        self.last_page = page
        self.last_context = context
        return context

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
        self.browser = _FakeBrowser()
        self.chromium = _FakeChromium(self.browser)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_capture_demo_records_video_trace_and_summary(tmp_path: Path) -> None:
    script = tmp_path / "demo.yaml"
    script.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: goto",
                "    url: /login",
                "  - action: screenshot",
                "  - action: snapshot_a11y",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake = _FakePlaywright()
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    result = runtime.capture_demo(
        base_url="http://127.0.0.1:5000",
        path="/",
        script_path=script,
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1280, height=720),
        record_video=True,
        trace_mode="on",
    )

    assert result.ok is True
    assert result.error_type is None
    assert result.artifacts["summary"].exists()
    assert result.artifacts["video"].suffix == ".webm"
    assert result.artifacts["trace"].suffix == ".zip"
    assert result.artifacts["step_2.screenshot"].exists()
    assert result.artifacts["step_3.a11y_snapshot"].exists()

    summary = json.loads(result.artifacts["summary"].read_text(encoding="utf-8"))
    assert summary["status"] == "ok"
    assert len(summary["steps"]) == 3

    assert fake.browser.last_page is not None and fake.browser.last_page.closed is True
    assert (
        fake.browser.last_context is not None
        and fake.browser.last_context.closed is True
    )
    assert fake.browser.closed is True
    assert fake.stopped is True


def test_capture_demo_failure_keeps_trace_and_failure_screenshot(
    tmp_path: Path,
) -> None:
    script = tmp_path / "demo-fail.yaml"
    script.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: click",
                "    text: fail-click",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake = _FakePlaywright()
    runtime = BrowserRuntime(playwright_loader=lambda: fake)
    result = runtime.capture_demo(
        base_url="http://127.0.0.1:5000",
        path="/",
        script_path=script,
        out_dir=tmp_path / "outbox",
        viewport=Viewport(width=1200, height=800),
        trace_mode="retain-on-failure",
    )

    assert result.ok is False
    assert result.error_type == "DemoStepError"
    assert "step_1" not in result.artifacts
    assert result.artifacts["trace"].exists()
    assert result.artifacts["failure_screenshot"].exists()
    assert result.artifacts["summary"].exists()

    summary = json.loads(result.artifacts["summary"].read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["steps"][0]["ok"] is False

    assert fake.browser.last_page is not None and fake.browser.last_page.closed is True
    assert (
        fake.browser.last_context is not None
        and fake.browser.last_context.closed is True
    )
    assert fake.browser.closed is True
    assert fake.stopped is True


def test_capture_demo_manifest_validation_error(tmp_path: Path) -> None:
    script = tmp_path / "invalid.yaml"
    script.write_text("steps: []\n", encoding="utf-8")

    runtime = BrowserRuntime(playwright_loader=lambda: _FakePlaywright())
    result = runtime.capture_demo(
        base_url="http://127.0.0.1:5000",
        script_path=script,
        out_dir=tmp_path / "outbox",
    )

    assert result.ok is False
    assert result.error_type == "ManifestValidationError"
    assert "Unsupported demo manifest version" in (result.error_message or "")


def test_preflight_demo_reports_actionable_step_diagnostics(tmp_path: Path) -> None:
    script = tmp_path / "preflight-ok.yaml"
    script.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: goto",
                "    url: /login",
                "  - action: click",
                "    role: button",
                "    name: Submit",
                "  - action: wait_ms",
                "    ms: 5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    runtime = BrowserRuntime(playwright_loader=lambda: _FakePlaywright())
    result = runtime.preflight_demo(
        base_url="http://127.0.0.1:5400",
        path="/",
        script_path=script,
        viewport=Viewport(width=1280, height=720),
    )

    assert result.ok is True
    assert result.error_type is None
    assert result.diagnostics.ok is True
    assert len(result.diagnostics.steps) == 3
    assert result.diagnostics.steps[0].detail.startswith("goto reachable:")
    assert "locator is actionable" in result.diagnostics.steps[1].detail
    assert "no locator assumptions" in result.diagnostics.steps[2].detail


def test_preflight_demo_failure_reports_failed_step_details(tmp_path: Path) -> None:
    script = tmp_path / "preflight-fail.yaml"
    script.write_text(
        "\n".join(
            [
                "version: 1",
                "steps:",
                "  - action: click",
                "    text: missing-target",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    runtime = BrowserRuntime(playwright_loader=lambda: _FakePlaywright())
    result = runtime.preflight_demo(
        base_url="http://127.0.0.1:5500",
        path="/",
        script_path=script,
        viewport=Viewport(width=1200, height=800),
    )

    assert result.ok is False
    assert result.error_type == "DemoStepError"
    assert result.diagnostics.ok is False
    assert len(result.diagnostics.steps) == 1
    failed = result.diagnostics.steps[0]
    assert failed.ok is False
    assert failed.index == 1
    assert failed.action == "click"
    assert "locator not actionable" in failed.detail
    assert "Preflight failed for 1 step(s): step 1 (click)" in (
        result.error_message or ""
    )
