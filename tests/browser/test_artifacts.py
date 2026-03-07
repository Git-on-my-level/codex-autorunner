from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.browser.artifacts import (
    deterministic_artifact_name,
    reserve_artifact_path,
    write_json_artifact,
)


def test_deterministic_artifact_name_is_stable() -> None:
    first = deterministic_artifact_name(
        kind="screenshot",
        extension="png",
        url="http://127.0.0.1:3000/dashboard",
    )
    second = deterministic_artifact_name(
        kind="screenshot",
        extension="png",
        url="http://127.0.0.1:3000/dashboard",
    )
    assert first == second
    assert first == "screenshot-dashboard.png"


def test_deterministic_artifact_name_rejects_invalid_output_name() -> None:
    with pytest.raises(ValueError):
        deterministic_artifact_name(
            kind="observe-a11y",
            extension="json",
            output_name="../escape.json",
        )


def test_reserve_artifact_path_handles_collisions(tmp_path: Path) -> None:
    out_dir = tmp_path / "outbox"
    first, first_index = reserve_artifact_path(out_dir, "capture.png")
    first.write_bytes(b"first")
    second, second_index = reserve_artifact_path(out_dir, "capture.png")

    assert first_index == 1
    assert second_index == 2
    assert first.name == "capture.png"
    assert second.name == "capture-2.png"


def test_write_json_artifact_emits_parseable_json(tmp_path: Path) -> None:
    out_dir = tmp_path / "outbox"
    result = write_json_artifact(
        out_dir=out_dir,
        filename="observe-meta.json",
        payload={"title": "Home", "captured_url": "http://127.0.0.1:3000"},
    )
    parsed = json.loads(result.path.read_text(encoding="utf-8"))
    assert parsed["title"] == "Home"
    assert parsed["captured_url"] == "http://127.0.0.1:3000"
