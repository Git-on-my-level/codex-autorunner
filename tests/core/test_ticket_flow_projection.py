from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_projection import build_canonical_state_v1


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
        "recommended_action": "car flow ticket_flow resume --run-id c222",
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
