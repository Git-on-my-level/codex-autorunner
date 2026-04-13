from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_projection import (
    build_canonical_state_v1,
    select_authoritative_run_record,
)


def _seed_run(repo_root: Path, run_id: str, status: FlowRunStatus) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={"workspace_root": str(repo_root)},
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, status)


def test_build_canonical_state_v1_uses_represented_run_when_preferred_is_stale(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    stale_run_id = "c1111111-1111-1111-1111-111111111111"
    represented_run_id = "c2222222-2222-2222-2222-222222222222"
    _seed_run(repo_root, stale_run_id, FlowRunStatus.COMPLETED)
    _seed_run(repo_root, represented_run_id, FlowRunStatus.PAUSED)

    run_state = {
        "run_id": represented_run_id,
        "state": "paused",
        "flow_status": "paused",
        "recommended_action": "car flow ticket_flow start",
    }
    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state=run_state,
        preferred_run_id=stale_run_id,
    )

    assert canonical.get("represented_run_id") == represented_run_id
    assert canonical.get("latest_run_id") == represented_run_id
    assert canonical.get("latest_run_status") == "paused"
    contradictions = canonical.get("contradictions") or []
    assert "represented_run_mismatch_latest" not in contradictions


def test_build_canonical_state_v1_prefers_ingest_receipt(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "ingest_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ingested": True,
                "ingested_at": "2026-02-28T12:00:00+00:00",
                "source": "import_pack",
            }
        ),
        encoding="utf-8",
    )

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={},
    )
    assert canonical.get("ingested") is True
    assert canonical.get("ingested_at") == "2026-02-28T12:00:00+00:00"
    assert canonical.get("ingest_source") == "import_pack"


def test_build_canonical_state_v1_falls_back_when_ingest_receipt_invalid(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "ingest_state.json").write_text(
        json.dumps(
            {
                "schema_version": 999,
                "ingested": True,
                "ingested_at": "2026-02-28T12:00:00+00:00",
                "source": "import_pack",
            }
        ),
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---\n\nBody\n",
        encoding="utf-8",
    )

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={},
    )
    assert canonical.get("ingested") is True
    assert canonical.get("ingested_at") is None
    assert canonical.get("ingest_source") == "ticket_files"


def test_build_canonical_state_v1_adds_freshness_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection._iso_now",
        lambda: "2026-03-01T01:00:00+00:00",
    )

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={
            "state": "paused",
            "last_progress_at": "2026-03-01T00:00:00+00:00",
        },
        stale_threshold_seconds=600,
    )

    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at") == "2026-03-01T01:00:00+00:00"
    assert freshness.get("recency_basis") == "run_state_last_progress_at"
    assert freshness.get("basis_at") == "2026-03-01T00:00:00+00:00"
    assert freshness.get("age_seconds") == 3600
    assert freshness.get("stale_threshold_seconds") == 600
    assert freshness.get("is_stale") is True


def test_select_authoritative_run_record_returns_none_for_empty() -> None:
    assert select_authoritative_run_record([]) is None


def test_select_authoritative_run_record_prefers_represented_over_latest() -> None:
    r_stale = FlowRunRecord(
        id="stale-run",
        flow_type="ticket_flow",
        status=FlowRunStatus.COMPLETED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-01T00:00:00+00:00",
    )
    r_represented = FlowRunRecord(
        id="represented-run",
        flow_type="ticket_flow",
        status=FlowRunStatus.PAUSED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-02T00:00:00+00:00",
    )
    records = [r_stale, r_represented]
    result = select_authoritative_run_record(
        records,
        represented_run_id="represented-run",
    )
    assert result is not None
    assert str(result.id) == "represented-run"


def test_select_authoritative_run_record_skips_superseded() -> None:
    r_super = FlowRunRecord(
        id="superseded-run",
        flow_type="ticket_flow",
        status=FlowRunStatus.SUPERSEDED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-01T00:00:00+00:00",
    )
    r_active = FlowRunRecord(
        id="active-run",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-02T00:00:00+00:00",
    )
    records = [r_active, r_super]
    result = select_authoritative_run_record(records)
    assert result is not None
    assert str(result.id) == "active-run"


def test_select_authoritative_run_record_returns_latest_when_all_superseded() -> None:
    r1 = FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=FlowRunStatus.SUPERSEDED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-01T00:00:00+00:00",
    )
    r2 = FlowRunRecord(
        id="run-2",
        flow_type="ticket_flow",
        status=FlowRunStatus.SUPERSEDED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-02T00:00:00+00:00",
    )
    records = [r2, r1]
    result = select_authoritative_run_record(records)
    assert result is not None
    assert str(result.id) == "run-2"


def test_select_authoritative_run_record_running_preferred_over_paused_latest() -> None:
    r_latest_paused = FlowRunRecord(
        id="latest-paused",
        flow_type="ticket_flow",
        status=FlowRunStatus.PAUSED,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-03T00:00:00+00:00",
    )
    r_preferred_running = FlowRunRecord(
        id="preferred-running",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        input_data={},
        state={},
        metadata={},
        created_at="2026-01-02T00:00:00+00:00",
    )
    records = [r_latest_paused, r_preferred_running]
    result = select_authoritative_run_record(
        records,
        preferred_run_id="preferred-running",
    )
    assert result is not None
    assert str(result.id) == "preferred-running"


def test_build_canonical_state_v1_ticket_census_counts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: true\n---\n\nDone.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nagent: codex\ndone: true\n---\n\nDone.\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nagent: codex\ndone: false\n---\n\nOpen.\n",
        encoding="utf-8",
    )

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={},
    )
    assert canonical["frontmatter_total_count"] == 3
    assert canonical["frontmatter_done_count"] == 2
    assert canonical["effective_next_ticket"] == "TICKET-003.md"


def test_build_canonical_state_v1_completed_with_remaining_tickets_contradiction(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---\n\nOpen.\n",
        encoding="utf-8",
    )

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={
            "state": "completed",
        },
    )
    contradictions = canonical.get("contradictions") or []
    assert "completed_flow_with_remaining_tickets" in contradictions


def test_build_canonical_state_v1_attention_without_recommendation_contradiction(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={
            "state": "blocked",
        },
    )
    contradictions = canonical.get("contradictions") or []
    assert "attention_required_without_recommendation" in contradictions


def test_build_canonical_state_v1_no_tickets_no_contradictions(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    canonical = build_canonical_state_v1(
        repo_root=repo_root,
        repo_id="repo-1",
        run_state={},
    )
    assert canonical["frontmatter_total_count"] == 0
    assert canonical["frontmatter_done_count"] == 0
    assert canonical["effective_next_ticket"] is None
    contradictions = canonical.get("contradictions") or []
    assert "completed_flow_with_remaining_tickets" not in contradictions
