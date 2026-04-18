from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from codex_autorunner.browser.runtime import BrowserRuntime
from tests.chat_surface_integration.harness import patch_hermes_runtime
from tests.chat_surface_lab.artifact_manifests import ArtifactKind
from tests.chat_surface_lab.discord_simulator import DiscordSurfaceSimulator
from tests.chat_surface_lab.scenario_models import SurfaceKind
from tests.chat_surface_lab.scenario_runner import (
    ChatSurfaceScenarioRunner,
    TranscriptInvariantSpec,
    load_scenario_by_id,
)


class _FakeAccessibility:
    def snapshot(self):  # type: ignore[no-untyped-def]
        return {"role": "WebArea", "name": "Transcript"}


class _FakePage:
    def __init__(self) -> None:
        self.url = ""
        self.origin = "http://127.0.0.1"
        self.accessibility = _FakeAccessibility()
        self.closed = False

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.url = url
        parsed = urlsplit(url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

    def title(self) -> str:
        return "Transcript"

    def content(self) -> str:
        return "<html><body><main>Transcript</main></body></html>"

    def screenshot(self, *, path: str, full_page: bool) -> None:
        _ = full_page
        Path(path).write_bytes(b"png")

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def new_page(self) -> _FakePage:
        return _FakePage()

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def new_context(self, **kwargs):  # type: ignore[no-untyped-def]
        viewport = kwargs.get("viewport", {})
        assert viewport["width"] > 0
        assert viewport["height"] > 0
        return _FakeContext()

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def launch(self, *, headless: bool) -> _FakeBrowser:
        assert headless is True
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _fake_browser_runtime() -> BrowserRuntime:
    return BrowserRuntime(playwright_loader=lambda: _FakePlaywright())


@pytest.mark.anyio
async def test_manifest_includes_required_evidence_files_with_stable_names(
    tmp_path: Path,
) -> None:
    simulator = DiscordSurfaceSimulator()
    await simulator.create_interaction_response(
        interaction_id="inter-1",
        interaction_token="token-1",
        payload={"type": 5, "data": {"flags": 64}},
    )
    await simulator.create_followup_message(
        application_id="app-1",
        interaction_token="token-1",
        payload={"content": "Done", "flags": 64},
    )

    manifest = simulator.write_artifacts(
        output_dir=tmp_path / "artifacts",
        scenario_id="artifact-schema",
        run_id="artifact-schema-discord",
        browser_runtime=_fake_browser_runtime(),
    )

    by_kind = {record.kind: record for record in manifest.artifacts}
    assert (
        by_kind[ArtifactKind.NORMALIZED_TRANSCRIPT_JSON].path.name == "transcript.json"
    )
    assert by_kind[ArtifactKind.SURFACE_TIMELINE_JSON].path.name == "timeline.json"
    assert by_kind[ArtifactKind.RENDERED_HTML].path.name == "transcript.html"
    assert by_kind[ArtifactKind.SCREENSHOT_PNG].path.name == "screenshot.png"
    assert (
        by_kind[ArtifactKind.ACCESSIBILITY_SNAPSHOT_JSON].path.name
        == "a11y_snapshot.json"
    )
    assert by_kind[ArtifactKind.ARTIFACT_MANIFEST_JSON].path.name == "manifest.json"

    required_paths = [
        "transcript.json",
        "timeline.json",
        "transcript.html",
        "screenshot.png",
        "a11y_snapshot.json",
        "manifest.json",
        "logs.json",
        "timing_report.json",
    ]
    for name in required_paths:
        assert (tmp_path / "artifacts" / name).exists()

    manifest_payload = json.loads(
        (tmp_path / "artifacts" / "manifest.json").read_text(encoding="utf-8")
    )
    indexed_kinds = {item["kind"] for item in manifest_payload["artifacts"]}
    assert "normalized_transcript_json" in indexed_kinds
    assert "surface_timeline_json" in indexed_kinds
    assert "rendered_html" in indexed_kinds
    assert "screenshot_png" in indexed_kinds
    assert "accessibility_snapshot_json" in indexed_kinds
    assert manifest_payload["capture_status"]["screenshot"]["ok"] is True
    assert manifest_payload["capture_status"]["observe"]["ok"] is True


@pytest.mark.anyio
async def test_failed_scenario_keeps_stable_artifact_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_scenario = load_scenario_by_id("first_visible_feedback")
    failing_scenario = replace(
        base_scenario,
        scenario_id="first_visible_feedback_failure_keeps_artifacts",
        transcript_invariants=base_scenario.transcript_invariants
        + (
            TranscriptInvariantSpec(
                kind="contains_text",
                params={"text": "__missing_expected_text__"},
                surfaces=(SurfaceKind.DISCORD,),
            ),
        ),
    )
    runner = ChatSurfaceScenarioRunner(
        output_root=tmp_path / "scenario-runs",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
        browser_runtime=_fake_browser_runtime(),
    )

    with pytest.raises(AssertionError):
        await runner.run_scenario(failing_scenario)

    artifacts_dir = (
        tmp_path
        / "scenario-runs"
        / failing_scenario.scenario_id
        / SurfaceKind.DISCORD.value
        / "artifacts"
    )
    assert (artifacts_dir / "manifest.json").exists()
    assert (artifacts_dir / "transcript.json").exists()
    assert (artifacts_dir / "timeline.json").exists()
    assert (artifacts_dir / "transcript.html").exists()

    manifest_payload = json.loads(
        (artifacts_dir / "manifest.json").read_text(encoding="utf-8")
    )
    indexed_paths = {item["path"] for item in manifest_payload["artifacts"]}
    assert "transcript.json" in indexed_paths
    assert "timeline.json" in indexed_paths
    assert "transcript.html" in indexed_paths
    assert "logs.json" in indexed_paths
    assert "timing_report.json" in indexed_paths
