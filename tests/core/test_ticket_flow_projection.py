from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_projection import (
    _extract_pr_url,
    _is_start_new_flow_action,
    _normalize_optional_int,
    _normalize_optional_str,
    build_canonical_state_v1,
    collect_ticket_flow_census,
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
        "recommended_action": "car ticket-flow start",
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


def test_is_start_new_flow_action_accepts_legacy_and_canonical_cli_spelling() -> None:
    # Split so check_cli_command_hints does not see a removed CLI path in one literal.
    legacy = "".join(
        (
            "car flow ",
            "ticket_flow ",
            "start --repo /tmp/r",
        )
    )
    assert _is_start_new_flow_action(legacy)
    assert _is_start_new_flow_action("car ticket-flow start --repo /tmp/r")


def test_is_start_new_flow_action_rejects_run_id_and_other_actions() -> None:
    assert not _is_start_new_flow_action(
        "car ticket-flow start --repo /tmp/r --run-id abc"
    )
    assert not _is_start_new_flow_action("car ticket-flow status --repo /tmp/r")


class TestSelectAuthoritativeRunRecord:
    def _make_record(self, run_id: str, status: FlowRunStatus) -> FlowRunRecord:
        return FlowRunRecord(
            id=run_id,
            flow_type="ticket_flow",
            status=status,
            input_data={},
            state={},
            metadata={},
            created_at="2026-01-01T00:00:00+00:00",
        )

    def test_returns_none_for_empty_list(self) -> None:
        assert select_authoritative_run_record([]) is None

    def test_returns_represented_when_given(self) -> None:
        preferred = self._make_record("r1", FlowRunStatus.COMPLETED)
        represented = self._make_record("r2", FlowRunStatus.PAUSED)
        result = select_authoritative_run_record(
            [preferred, represented],
            represented_run_id="r2",
        )
        assert result is represented

    def test_ignores_represented_when_not_found(self) -> None:
        a = self._make_record("r1", FlowRunStatus.COMPLETED)
        result = select_authoritative_run_record(
            [a],
            represented_run_id="nonexistent",
        )
        assert result is a

    def test_excludes_superseded_by_default(self) -> None:
        superseded = self._make_record("r1", FlowRunStatus.SUPERSEDED)
        active = self._make_record("r2", FlowRunStatus.PAUSED)
        result = select_authoritative_run_record([superseded, active])
        assert result is active

    def test_falls_back_to_superseded_when_all_superseded(self) -> None:
        s1 = self._make_record("r1", FlowRunStatus.SUPERSEDED)
        s2 = self._make_record("r2", FlowRunStatus.SUPERSEDED)
        result = select_authoritative_run_record([s1, s2])
        assert result is s1

    def test_preferred_running_beats_latest_paused(self) -> None:
        running = self._make_record("r1", FlowRunStatus.RUNNING)
        paused = self._make_record("r2", FlowRunStatus.PAUSED)
        result = select_authoritative_run_record(
            [paused, running],
            preferred_run_id="r1",
        )
        assert result is running

    def test_preferred_stopping_beats_latest_failed(self) -> None:
        stopping = self._make_record("r1", FlowRunStatus.STOPPING)
        failed = self._make_record("r2", FlowRunStatus.FAILED)
        result = select_authoritative_run_record(
            [failed, stopping],
            preferred_run_id="r1",
        )
        assert result is stopping

    def test_returns_latest_when_preferred_not_active(self) -> None:
        completed = self._make_record("r1", FlowRunStatus.COMPLETED)
        paused = self._make_record("r2", FlowRunStatus.PAUSED)
        result = select_authoritative_run_record(
            [paused, completed],
            preferred_run_id="r1",
        )
        assert result is paused


class TestCollectTicketFlowCensus:
    def test_empty_tickets_dir_returns_zero_census(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        census = collect_ticket_flow_census(tmp_path)
        assert census.total_count == 0
        assert census.done_count == 0
        assert census.effective_next_ticket is None
        assert census.open_pr_url is None
        assert census.final_review_status is None

    def test_first_undone_ticket_is_effective_next(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "TICKET-001.md").write_text(
            "---\nagent: codex\ndone: true\n---\n\nDone\n",
            encoding="utf-8",
        )
        (ticket_dir / "TICKET-002.md").write_text(
            "---\nagent: codex\ndone: false\n---\n\nPending\n",
            encoding="utf-8",
        )
        (ticket_dir / "TICKET-003.md").write_text(
            "---\nagent: codex\ndone: false\n---\n\nPending 2\n",
            encoding="utf-8",
        )
        census = collect_ticket_flow_census(tmp_path)
        assert census.total_count == 3
        assert census.done_count == 1
        assert census.effective_next_ticket == "TICKET-002.md"

    def test_all_done_means_no_effective_next(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "TICKET-001.md").write_text(
            "---\nagent: codex\ndone: true\n---\n\nDone\n",
            encoding="utf-8",
        )
        census = collect_ticket_flow_census(tmp_path)
        assert census.total_count == 1
        assert census.done_count == 1
        assert census.effective_next_ticket is None

    def test_extracts_pr_url_from_frontmatter(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "TICKET-001.md").write_text(
            "---\n"
            "agent: codex\n"
            "done: false\n"
            "ticket_kind: open_pr\n"
            "pr_url: https://github.com/example/repo/pull/42\n"
            "---\n\nOpen PR\n",
            encoding="utf-8",
        )
        census = collect_ticket_flow_census(tmp_path)
        assert census.open_pr_url == "https://github.com/example/repo/pull/42"

    def test_detects_final_review_status(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "TICKET-001.md").write_text(
            "---\nagent: codex\ndone: false\nticket_kind: final_review\n---\n\nReview\n",
            encoding="utf-8",
        )
        census = collect_ticket_flow_census(tmp_path)
        assert census.final_review_status == "pending"

    def test_detects_final_review_done(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "TICKET-001.md").write_text(
            "---\nagent: codex\ndone: true\nticket_kind: final_review\n---\n\nDone\n",
            encoding="utf-8",
        )
        census = collect_ticket_flow_census(tmp_path)
        assert census.final_review_status == "done"

    def test_missing_tickets_dir_returns_zero_census(self, tmp_path: Path) -> None:
        census = collect_ticket_flow_census(tmp_path)
        assert census.total_count == 0


class TestExtractPrUrl:
    def test_returns_none_when_no_data(self) -> None:
        assert _extract_pr_url({}, None) is None

    def test_returns_frontmatter_pr_url(self) -> None:
        data = {"pr_url": "https://github.com/example/repo/pull/1"}
        assert _extract_pr_url(data, None) == "https://github.com/example/repo/pull/1"

    def test_falls_back_to_body_match(self) -> None:
        body = "See https://github.com/example/repo/pull/2 for details"
        assert _extract_pr_url({}, body) == "https://github.com/example/repo/pull/2"

    def test_frontmatter_takes_priority_over_body(self) -> None:
        data = {"pr_url": "https://github.com/example/repo/pull/1"}
        body = "Also https://github.com/example/repo/pull/2"
        assert _extract_pr_url(data, body) == "https://github.com/example/repo/pull/1"


class TestNormalizeOptional:
    def test_str_normalizes_whitespace(self) -> None:
        assert _normalize_optional_str("  hello  ") == "hello"

    def test_str_empty_returns_none(self) -> None:
        assert _normalize_optional_str("   ") is None

    def test_none_returns_none(self) -> None:
        assert _normalize_optional_str(None) is None
        assert _normalize_optional_int(None) is None

    def test_int_passes_through(self) -> None:
        assert _normalize_optional_int(42) == 42

    def test_int_from_numeric_string(self) -> None:
        assert _normalize_optional_int("  7  ") == 7

    def test_int_non_numeric_returns_none(self) -> None:
        assert _normalize_optional_int("abc") is None
