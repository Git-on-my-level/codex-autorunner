from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.chat_surface_lab.acp_conformance import (
    ACPConformanceReport,
    ACPConformanceTarget,
    format_acp_conformance_report,
    run_acp_conformance_report,
)
from tests.chat_surface_lab.backend_runtime import fake_acp_command


@pytest.mark.anyio
async def test_acp_conformance_runner_passes_fixture_target(tmp_path: Path) -> None:
    report = await run_acp_conformance_report(
        tmp_path,
        targets=[
            ACPConformanceTarget(
                id="fixture.official",
                label="Fixture ACP (official)",
                command=tuple(fake_acp_command("official")),
                source="fixture",
                scenario="official",
            )
        ],
        artifact_dir=tmp_path / "artifacts",
    )

    assert report.passed is True
    assert len(report.results) == 1
    target_result = report.results[0]
    assert target_result.passed is True
    assert {case.case_id for case in target_result.cases} >= {
        "initialize",
        "session_roundtrip",
        "prompt_completion",
        "prompt_reuse",
        "fixture.permission_flow",
        "fixture.interrupt_flow",
        "fixture.prompt_hang_timeout",
        "fixture.empty_load_result",
        "fixture.null_load_result",
    }

    latest = json.loads((tmp_path / "artifacts" / "latest.json").read_text())
    assert latest["passed"] is True
    assert latest["results"][0]["target"]["id"] == "fixture.official"


def test_acp_conformance_report_formatter_mentions_targets_and_case_ids(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    artifact_dir = tmp_path / "report"
    report = ACPConformanceReport(
        run_id="run-1",
        repo_root=str(repo_root),
        created_at="2026-01-01T00:00:00+00:00",
        portable_case_ids=("initialize",),
        fixture_case_ids=(),
        results=(),
        artifact_dir=str(artifact_dir),
    )

    formatted = format_acp_conformance_report(report)

    assert "ACP conformance run run-1" in formatted
    assert f"repo_root={repo_root}" in formatted
