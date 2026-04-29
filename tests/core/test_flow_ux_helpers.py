from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core import ticket_flow_operator
from codex_autorunner.core.flows import ux_helpers
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.integrations.github.service import RepoInfo


def test_bootstrap_check_ready_when_tickets_exist(tmp_path):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "--\nagent: codex\ndone: false\n--\n", encoding="utf-8"
    )

    result = ux_helpers.bootstrap_check(tmp_path)

    assert result.status == "ready"


def test_bootstrap_check_needs_issue_with_github(tmp_path):
    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    result = ux_helpers.bootstrap_check(tmp_path, github_service_factory=DummyGH)

    assert result.status == "needs_issue"
    assert result.github_available is True
    assert result.repo_slug == "org/repo"


def test_seed_issue_from_plan():
    content = ux_helpers.seed_issue_from_text("do things")
    assert "do things" in content


def test_seed_issue_from_github(tmp_path):
    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def validate_issue_same_repo(self, issue_ref):
            assert issue_ref == "#5"
            return 5

        def issue_view(self, number: int):
            return {
                "number": number,
                "title": "Example",
                "url": "https://github.com/org/repo/issues/5",
                "state": "open",
                "author": {"login": "alice"},
                "body": "Body text",
            }

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    result = ux_helpers.seed_issue_from_github(
        tmp_path, "#5", github_service_factory=DummyGH
    )

    assert result.issue_number == 5
    assert result.repo_slug == "org/repo"
    assert "# Issue #5" in result.content
    assert "alice" in result.content


def test_ensure_worker_closes_spawned_stream_handles(monkeypatch, tmp_path: Path):
    class DummyStream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    proc = SimpleNamespace(pid=123)
    stdout = DummyStream()
    stderr = DummyStream()

    health = FlowWorkerHealth(
        status="dead",
        pid=None,
        cmdline=[],
        artifact_path=tmp_path / ".codex-autorunner" / "flows" / "run",
    )
    health.artifact_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        ux_helpers, "check_worker_health", lambda *_args, **_kwargs: health
    )
    monkeypatch.setattr(
        ux_helpers, "clear_worker_metadata", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        ux_helpers,
        "spawn_flow_worker",
        lambda *_args, **_kwargs: (proc, stdout, stderr),
    )

    result = ux_helpers.ensure_worker(tmp_path, "3022db08-82b8-40dd-8cfa-d04eb0fcded2")

    assert result["status"] == "spawned"
    assert result["proc"] is proc
    assert result["stdout"] is None
    assert result["stderr"] is None
    assert stdout.closed is True
    assert stderr.closed is True


def _run(run_id: str, status: FlowRunStatus) -> FlowRunRecord:
    return FlowRunRecord(
        id=run_id,
        flow_type="ticket_flow",
        status=status,
        created_at="2026-01-01T00:00:00Z",
    )


def test_select_ticket_flow_run_record_supports_shared_selection_modes() -> None:
    records = [
        _run("paused-run", FlowRunStatus.PAUSED),
        _run("running-run", FlowRunStatus.RUNNING),
        _run("stopped-run", FlowRunStatus.STOPPED),
    ]

    assert (
        ux_helpers.select_ticket_flow_run_record(records, selection="paused").id
        == "paused-run"
    )
    assert (
        ux_helpers.select_ticket_flow_run_record(records, selection="active").id
        == "running-run"
    )
    assert (
        ux_helpers.select_ticket_flow_run_record(records, selection="non_terminal").id
        == "paused-run"
    )


def test_select_ticket_flow_run_record_authoritative_matches_existing_policy() -> None:
    records = [
        _run("paused-run", FlowRunStatus.PAUSED),
        _run("running-run", FlowRunStatus.RUNNING),
        _run("completed-run", FlowRunStatus.COMPLETED),
    ]

    result = ux_helpers.select_ticket_flow_run_record(
        records, selection="authoritative"
    )

    assert result is not None
    assert result.id == "paused-run"


def test_flow_status_snapshot_includes_current_ticket_app_metadata(
    tmp_path: Path,
) -> None:
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\n"
        'ticket_id: "tkt_appstatus001"\n'
        "agent: codex\n"
        "done: false\n"
        "app: local.my-workflow\n"
        "app_version: 1.2.3\n"
        "app_source: local:apps/my-workflow@main\n"
        "---\n\n"
        "App ticket\n",
        encoding="utf-8",
    )
    record = FlowRunRecord(
        id="app-run",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        created_at="2026-01-01T00:00:00Z",
        state={"ticket_engine": {"current_ticket": "TICKET-001.md"}},
    )

    snapshot = ux_helpers.build_flow_status_snapshot(
        tmp_path, record, store=None, lite=True
    )

    assert snapshot["app"] == {
        "id": "local.my-workflow",
        "version": "1.2.3",
        "source": "local:apps/my-workflow@main",
    }


def test_flow_status_lines_include_app_only_when_present() -> None:
    record = _run("app-run", FlowRunStatus.RUNNING)
    base_snapshot = {
        "worker_health": None,
        "last_event_seq": None,
        "last_event_at": None,
        "effective_current_ticket": "TICKET-001.md",
        "ticket_progress": None,
    }

    lines = ux_helpers.format_ticket_flow_status_lines(
        record,
        {
            **base_snapshot,
            "app": {"id": "local.my-workflow", "version": "1.2.3"},
        },
    )
    no_app_lines = ux_helpers.format_ticket_flow_status_lines(record, base_snapshot)

    assert "App: local.my-workflow v1.2.3" in lines
    assert all(not line.startswith("App:") for line in no_app_lines)


def test_flow_status_lines_include_active_tool() -> None:
    record = _run("tool-run", FlowRunStatus.RUNNING)
    lines = ux_helpers.format_ticket_flow_status_lines(
        record,
        {
            "worker_health": None,
            "last_event_seq": 7,
            "last_event_at": "2026-03-11T00:00:00Z",
            "effective_current_ticket": "TICKET-001.md",
            "ticket_progress": None,
            "active_tool": {
                "command": ".venv/bin/python -m pytest -q",
                "elapsed_seconds": 245,
                "last_activity_at": "2026-03-11T01:00:00+00:00",
                "output_updated_at": "2026-03-11T01:00:00+00:00",
            },
            "freshness": {
                "status": "fresh",
                "recency_basis": "effective_last_activity_at",
                "basis_at": "2026-03-11T01:00:00+00:00",
                "age_seconds": 20,
            },
        },
    )

    assert (
        "Active tool: .venv/bin/python -m pytest -q (running 4m, output updated 20s ago)"
        in lines
    )
    assert "Freshness: fresh · activity 20s ago" in lines


def test_flow_status_snapshot_uses_active_tool_as_effective_activity(
    monkeypatch, tmp_path: Path
) -> None:
    record = _run("tool-run", FlowRunStatus.RUNNING)
    health = FlowWorkerHealth(
        status="alive",
        pid=4242,
        cmdline=["worker"],
        artifact_path=tmp_path / ".codex-autorunner" / "flows" / "tool-run",
    )
    health.active_tool = SimpleNamespace(
        to_dict=lambda: {
            "command": ".venv/bin/python -m pytest -q",
            "elapsed_seconds": 245,
            "last_activity_at": "2026-03-11T01:00:00+00:00",
            "output_updated_at": "2026-03-11T01:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        ticket_flow_operator, "check_worker_health", lambda *_args, **_kwargs: health
    )
    monkeypatch.setattr(
        ticket_flow_operator,
        "_canonical_flow_status_state",
        lambda *_args, **_kwargs: {
            "freshness": {
                "generated_at": "2026-03-11T01:00:20+00:00",
                "stale_threshold_seconds": 1800,
                "basis_at": "2026-03-11T00:00:00+00:00",
                "status": "stale",
            }
        },
    )

    snapshot = ticket_flow_operator.build_ticket_flow_status_snapshot(
        tmp_path, record, store=None
    )

    assert snapshot["agent_status"] == "busy"
    assert snapshot["active_tool"]["command"] == ".venv/bin/python -m pytest -q"
    assert snapshot["effective_last_activity_at"] == "2026-03-11T01:00:00+00:00"
    assert snapshot["freshness"]["status"] == "fresh"
