from __future__ import annotations

import json
from pathlib import Path

from tests.chat_surface_lab.web_responsiveness_budgets import (
    run_web_responsiveness_budget_smoke,
)


def test_web_responsiveness_budget_smoke_writes_actionable_diagnostics(
    tmp_path: Path,
) -> None:
    result = run_web_responsiveness_budget_smoke(
        hub_root=tmp_path / "hub",
        artifact_dir=tmp_path / "diagnostics" / "web-responsiveness-budgets",
        seed_counts={
            "chat_count": 1200,
            "repo_count": 140,
            "worktree_count": 280,
            "ticket_run_group_count": 120,
            "timeline_event_count": 70,
            "journal_event_count": 180,
        },
    )

    assert result.passed is True
    assert result.latest_path.exists()
    assert result.run_report_path.exists()
    payload = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert payload["seed"]["chat_count"] >= 1000
    assert payload["seed"]["repo_count"] >= 100
    assert payload["seed"]["worktree_count"] >= 200
    assert payload["signoff"]["passed"] is True
    assert payload["failures"] == []

    diagnostics = payload["diagnostics"]
    assert diagnostics["backend_snapshot"]["index_rows_returned"] <= 50
    assert diagnostics["backend_snapshot"]["detail_timeline_items"] <= 50
    assert (
        diagnostics["backend_snapshot"]["snapshot_payload_bytes"]
        <= payload["budgets"]["snapshot_payload_bytes"]
    )
    assert (
        diagnostics["frontend"]["virtualized_dom_rows"]
        <= payload["budgets"]["virtualized_dom_rows"]
    )

    categories = {item["category"] for item in payload["observations"]}
    assert {
        "projection_lag",
        "backend_snapshot_latency",
        "stream_latency",
        "frontend_render_work",
    }.issubset(categories)


def test_web_responsiveness_budget_failure_names_the_slow_subsystem(
    tmp_path: Path,
) -> None:
    result = run_web_responsiveness_budget_smoke(
        hub_root=tmp_path / "hub",
        artifact_dir=tmp_path / "diagnostics" / "web-responsiveness-budgets",
        seed_counts={
            "chat_count": 100,
            "repo_count": 20,
            "worktree_count": 40,
            "ticket_run_group_count": 20,
            "timeline_event_count": 10,
            "journal_event_count": 30,
        },
        budgets={"snapshot_query_ms": 0.0},
    )

    assert result.passed is False
    failures = result.payload["failures"]
    assert failures
    assert any(
        item["category"] == "backend_snapshot_latency"
        and item["metric"] == "snapshot_query_ms"
        for item in failures
    ), failures
    assert "generic timeout" not in json.dumps(failures).lower()
