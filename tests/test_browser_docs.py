from __future__ import annotations

from pathlib import Path

import yaml


def test_browser_render_workflow_doc_covers_core_topics() -> None:
    doc_path = Path("docs/ops/browser-render-workflows.md")
    text = doc_path.read_text(encoding="utf-8")

    required_snippets = [
        'pip install "codex-autorunner[browser]"',
        "car render screenshot",
        "car render observe",
        "car render demo",
        "--serve-cmd",
        "--ready-url",
        "tears down the spawned serve process tree",
        ".codex-autorunner/filebox/outbox/",
        "render_cli_available",
        "browser_automation_available",
    ]
    for snippet in required_snippets:
        assert snippet in text


def test_browser_demo_manifest_fixture_is_valid_v1() -> None:
    manifest_path = Path("tests/fixtures/browser_demo_manifest.yaml")
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    assert payload.get("version") == 1
    steps = payload.get("steps")
    assert isinstance(steps, list)
    assert len(steps) > 0
