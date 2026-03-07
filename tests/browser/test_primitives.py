from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.browser.models import Viewport
from codex_autorunner.browser.primitives import act_step, capture_artifact, observe_page


class _FakeAccessibility:
    def snapshot(self):  # type: ignore[no-untyped-def]
        return {
            "role": "WebArea",
            "name": "Settings",
            "children": [
                {"role": "button", "name": "Save"},
                {"role": "textbox", "name": "Email"},
            ],
        }


class _NullAccessibility:
    def snapshot(self):  # type: ignore[no-untyped-def]
        return None


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    def press(self, key: str) -> None:
        self.presses.append(key)


class _FakeLocator:
    def __init__(self, page: "_FakePage", key: str) -> None:
        self.page = page
        self.key = key

    def click(self, timeout: int) -> None:
        _ = timeout
        self.page.events.append(("click", self.key))

    def fill(self, value: str, timeout: int) -> None:
        _ = timeout
        self.page.events.append(("fill", self.key, value))

    def press(self, key: str, timeout: int) -> None:
        _ = timeout
        self.page.events.append(("press", self.key, key))

    def wait_for(self, *, state: str, timeout: int) -> None:
        _ = timeout
        self.page.events.append(("wait_for", self.key, state))


class _FakePage:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:9000/settings"
        self.accessibility = _FakeAccessibility()
        self.keyboard = _FakeKeyboard()
        self.events: list[tuple] = []

    def title(self) -> str:
        return "Settings"

    def content(self) -> str:
        return (
            '<html><body data-testid="settings-page" '
            'aria-label="Settings Root">OK</body></html>'
        )

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.url = url

    def get_by_role(self, role: str, **kwargs):  # type: ignore[no-untyped-def]
        name = kwargs.get("name", "")
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
        self.events.append(("wait_for_url", url))

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        Path(path).write_bytes(b"png")


class _FakePageNullA11y(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.accessibility = _NullAccessibility()


def test_observe_page_schema_keys_are_stable(tmp_path: Path) -> None:
    page = _FakePage()
    result = observe_page(
        page=page,
        out_dir=tmp_path / "outbox",
        target_url="http://127.0.0.1:9000/settings",
        path_hint="/settings",
        viewport=Viewport(width=1280, height=720),
    )

    assert {"snapshot", "metadata", "locator_refs", "run_manifest"}.issubset(
        result.artifacts.keys()
    )
    metadata = json.loads(result.artifacts["metadata"].read_text(encoding="utf-8"))
    locator_refs = json.loads(
        result.artifacts["locator_refs"].read_text(encoding="utf-8")
    )
    run_manifest = json.loads(
        result.artifacts["run_manifest"].read_text(encoding="utf-8")
    )

    assert metadata["schema_version"] == 1
    assert metadata["viewport"] == {"width": 1280, "height": 720}
    assert metadata["captured_url"].endswith("/settings")
    assert locator_refs["schema_version"] == 1
    assert locator_refs["count"] >= 2
    assert run_manifest["runtime_profile"]["deterministic"] is True
    assert run_manifest["runtime_profile"]["accessibility_first"] is True
    assert run_manifest["runtime_profile"]["prompt_driven"] is False


def test_act_step_supports_deterministic_interactions_and_artifacts(
    tmp_path: Path,
) -> None:
    page = _FakePage()

    assert (
        act_step(
            page=page,
            action="click",
            step_data={"role": "button", "name": "Save"},
            step_index=1,
            base_url="http://127.0.0.1:9000",
            initial_path="/settings",
            out_dir=tmp_path / "outbox",
            timeout_ms=1000,
        )
        == {}
    )

    assert (
        act_step(
            page=page,
            action="fill",
            step_data={"label": "Email", "value": "dev@example.com"},
            step_index=2,
            base_url="http://127.0.0.1:9000",
            initial_path="/settings",
            out_dir=tmp_path / "outbox",
            timeout_ms=1000,
        )
        == {}
    )

    screenshot_artifacts = act_step(
        page=page,
        action="screenshot",
        step_data={},
        step_index=3,
        base_url="http://127.0.0.1:9000",
        initial_path="/settings",
        out_dir=tmp_path / "outbox",
        timeout_ms=1000,
    )
    a11y_artifacts = act_step(
        page=page,
        action="snapshot_a11y",
        step_data={},
        step_index=4,
        base_url="http://127.0.0.1:9000",
        initial_path="/settings",
        out_dir=tmp_path / "outbox",
        timeout_ms=1000,
    )

    assert screenshot_artifacts["screenshot"].exists()
    assert a11y_artifacts["a11y_snapshot"].exists()
    assert ("click", "role:button:Save") in page.events
    assert ("fill", "label:Email", "dev@example.com") in page.events


def test_capture_artifact_is_deterministic_with_collisions(tmp_path: Path) -> None:
    out_dir = tmp_path / "outbox"

    first = capture_artifact(
        out_dir=out_dir,
        kind="observe-run-manifest",
        extension="json",
        url="http://127.0.0.1:9000/settings",
        path_hint="/settings",
        writer=lambda p: p.write_text("{}", encoding="utf-8"),
    )
    second = capture_artifact(
        out_dir=out_dir,
        kind="observe-run-manifest",
        extension="json",
        url="http://127.0.0.1:9000/settings",
        path_hint="/settings",
        writer=lambda p: p.write_text("{}", encoding="utf-8"),
    )

    assert first.name == "observe-run-manifest-settings.json"
    assert second.name == "observe-run-manifest-settings-2.json"


def test_a11y_snapshot_fallback_is_structured_when_runtime_returns_null(
    tmp_path: Path,
) -> None:
    page = _FakePageNullA11y()

    observe_result = observe_page(
        page=page,
        out_dir=tmp_path / "outbox",
        target_url="http://127.0.0.1:9000/settings",
        path_hint="/settings",
        viewport=Viewport(width=1280, height=720),
    )
    observe_snapshot = json.loads(
        observe_result.artifacts["snapshot"].read_text(encoding="utf-8")
    )
    observe_metadata = json.loads(
        observe_result.artifacts["metadata"].read_text(encoding="utf-8")
    )
    observe_run_manifest = json.loads(
        observe_result.artifacts["run_manifest"].read_text(encoding="utf-8")
    )

    assert observe_snapshot["status"] == "unavailable"
    assert observe_metadata["snapshot_status"] == "unavailable"
    assert observe_run_manifest["snapshot_status"] == "unavailable"

    demo_a11y_artifacts = act_step(
        page=page,
        action="snapshot_a11y",
        step_data={},
        step_index=7,
        base_url="http://127.0.0.1:9000",
        initial_path="/settings",
        out_dir=tmp_path / "outbox",
        timeout_ms=1000,
    )
    demo_snapshot = json.loads(
        demo_a11y_artifacts["a11y_snapshot"].read_text(encoding="utf-8")
    )
    assert demo_snapshot["status"] == "unavailable"


def test_wait_ms_step_enforces_step_timeout(tmp_path: Path) -> None:
    page = _FakePage()
    try:
        act_step(
            page=page,
            action="wait_ms",
            step_data={"ms": 1500, "timeout_ms": 250},
            step_index=1,
            base_url="http://127.0.0.1:9000",
            initial_path="/settings",
            out_dir=tmp_path / "outbox",
            timeout_ms=1000,
        )
    except ValueError as exc:
        assert "wait_ms step requested 1500ms but timeout is 250ms" in str(exc)
    else:
        raise AssertionError("Expected wait_ms timeout validation to fail")
