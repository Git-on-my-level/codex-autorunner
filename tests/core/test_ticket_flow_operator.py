from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core import ticket_flow_operator as operator_module
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_operator import (
    build_ticket_flow_operator_service,
    build_ticket_flow_run_state,
    ticket_flow_preflight,
)


def _write_ticket(repo_root: Path, name: str, *, done: bool = False) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / name).write_text(
        (
            "---\n"
            'ticket_id: "tkt_operator001"\n'
            "agent: codex\n"
            f"done: {'true' if done else 'false'}\n"
            "---\n"
        ),
        encoding="utf-8",
    )


def _write_dispatch(
    repo_root: Path,
    run_id: str,
    seq: int,
    *,
    mode: str,
    handoff: bool = False,
) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = ["---", f"mode: {mode}", "title: Example"]
    if handoff:
        frontmatter.append("is_handoff: true")
    frontmatter.append("---")
    (entry_dir / "DISPATCH.md").write_text(
        "\n".join(frontmatter) + "\n\nBody\n",
        encoding="utf-8",
    )


def _write_dead_worker_artifacts(repo_root: Path, run_id: str) -> None:
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "worker.json").write_text(
        json.dumps({"pid": 999_999, "cmd": ["python"], "spawned_at": 1.0}),
        encoding="utf-8",
    )
    (artifacts_dir / "crash.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-02-13T14:00:00Z",
                "worker_pid": 999_999,
                "exit_code": 137,
                "signal": "SIGKILL",
                "last_event": "item/reasoning/summaryTextDelta",
                "exception": "RepoNotFoundError: cwd mismatch",
            }
        ),
        encoding="utf-8",
    )


def test_ticket_flow_operator_preflight_reports_no_tickets(tmp_path: Path) -> None:
    report = ticket_flow_preflight(tmp_path, config=None)
    failing = {check.check_id for check in report.checks if check.status == "error"}
    assert "tickets_present" in failing


def test_ticket_flow_operator_preflight_reports_codex_runtime_details(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = Path(tmp_path)
    _write_ticket(repo_root, "TICKET-001.md")
    monkeypatch.setenv("CAR_TELEGRAM_APP_SERVER_COMMAND", "/stale/codex app-server")
    config = SimpleNamespace(
        codex_model="gpt-5.5",
        app_server=SimpleNamespace(
            command=["echo", "app-server"],
            command_source="config",
            ignored_command_env=("CAR_TELEGRAM_APP_SERVER_COMMAND",),
        ),
    )

    report = ticket_flow_preflight(repo_root, config=config)
    agents = next(check for check in report.checks if check.check_id == "agents")

    assert agents.status == "ok"
    assert "command: echo app-server" in agents.details
    assert "source: config" in agents.details
    assert "model: gpt-5.5" in agents.details
    assert "ignored surface env: CAR_TELEGRAM_APP_SERVER_COMMAND" in agents.details


def test_codex_version_command_skips_empty_command() -> None:
    assert operator_module._codex_version_command([]) == []


def test_codex_runtime_preflight_decodes_non_utf8_version_output(
    monkeypatch,
) -> None:
    def fake_check_output(*args, **kwargs):
        return b"codex \xff\n"

    monkeypatch.setattr(operator_module.subprocess, "check_output", fake_check_output)
    config = SimpleNamespace(
        codex_model="gpt-5.5",
        app_server=SimpleNamespace(
            command=["echo", "app-server"],
            command_source="config",
            ignored_command_env=(),
        ),
    )

    details, resolved = operator_module._codex_runtime_preflight_details(
        config, ["echo", "app-server"]
    )

    assert resolved is not None
    assert any(detail.startswith("version: codex") for detail in details)


def test_ticket_flow_operator_latest_dispatch_prefers_handoff_and_turn_summary(
    tmp_path: Path,
) -> None:
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"
    _write_dispatch(repo_root, run_id, seq=2, mode="turn_summary")
    _write_dispatch(repo_root, run_id, seq=1, mode="pause", handoff=True)

    operator = build_ticket_flow_operator_service(repo_root)
    latest = operator.latest_dispatch(
        run_id,
        {"workspace_root": str(repo_root), "runs_dir": ".codex-autorunner/runs"},
        include_turn_summary=True,
    )

    assert latest is not None
    assert latest["seq"] == 1
    assert latest["dispatch"]["mode"] == "pause"
    assert latest["turn_summary_seq"] == 2
    assert latest["turn_summary"]["mode"] == "turn_summary"


@pytest.mark.parametrize(
    (
        "record_status",
        "latest_payload",
        "latest_reply_seq",
        "stale_resume_reason",
        "expected_has_dispatch",
        "expected_reason",
    ),
    [
        (
            FlowRunStatus.RUNNING,
            {"seq": 1, "latest_seq": 1, "dispatch": {"mode": "pause"}},
            0,
            None,
            True,
            None,
        ),
        (
            FlowRunStatus.PAUSED,
            {"seq": 1, "latest_seq": 1, "dispatch": {"mode": "pause"}},
            0,
            None,
            True,
            None,
        ),
        (
            FlowRunStatus.PAUSED,
            {"seq": 1, "latest_seq": 2, "dispatch": {"mode": "pause"}},
            0,
            "Latest dispatch is stale; ticket flow resume preflight would fail",
            False,
            "Latest dispatch is stale; ticket flow resume preflight would fail",
        ),
        (
            FlowRunStatus.PAUSED,
            {"seq": 1, "latest_seq": 1, "dispatch": {"mode": "pause"}},
            1,
            None,
            False,
            "Latest dispatch already replied; run is still paused",
        ),
        (
            FlowRunStatus.PAUSED,
            {"seq": 1, "latest_seq": 1, "dispatch": {"mode": "notify"}},
            0,
            None,
            False,
            "Latest dispatch is informational and does not require reply",
        ),
        (
            FlowRunStatus.PAUSED,
            {"seq": 1, "latest_seq": 1, "dispatch": None, "errors": ["bad yaml"]},
            0,
            None,
            False,
            "Paused run has unreadable dispatch metadata",
        ),
        (
            FlowRunStatus.PAUSED,
            {},
            0,
            None,
            False,
            "Run is paused without an actionable dispatch",
        ),
    ],
)
def test_paused_dispatch_decision_table(
    record_status: FlowRunStatus,
    latest_payload: dict[str, object],
    latest_reply_seq: int,
    stale_resume_reason: str | None,
    expected_has_dispatch: bool,
    expected_reason: str | None,
) -> None:
    assert operator_module._resolve_paused_dispatch_decision(
        record_status=record_status,
        latest_payload=latest_payload,
        latest_reply_seq=latest_reply_seq,
        stale_resume_reason=stale_resume_reason,
    ) == (expected_has_dispatch, expected_reason)


def test_paused_dispatch_state_runs_filesystem_preflight_only_for_stale_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_resume_invalid_reason(repo_root: Path) -> str:
        nonlocal calls
        calls += 1
        return f"stale in {repo_root.name}"

    monkeypatch.setattr(
        operator_module,
        "_paused_dispatch_resume_invalid_reason",
        fake_resume_invalid_reason,
    )

    fresh = operator_module.resolve_paused_dispatch_state(
        repo_root=tmp_path,
        record_status=FlowRunStatus.PAUSED,
        latest_payload={
            "seq": 1,
            "latest_seq": 1,
            "dispatch": {"mode": "pause", "is_handoff": True},
        },
        latest_reply_seq=0,
    )
    stale = operator_module.resolve_paused_dispatch_state(
        repo_root=tmp_path,
        record_status=FlowRunStatus.PAUSED,
        latest_payload={
            "seq": 1,
            "latest_seq": 2,
            "dispatch": {"mode": "pause", "is_handoff": True},
        },
        latest_reply_seq=0,
    )

    assert fresh == (True, None)
    assert stale == (False, f"stale in {tmp_path.name}")
    assert calls == 1


def test_ticket_flow_operator_marks_stale_paused_dispatch_when_no_tickets_remain(
    tmp_path: Path,
) -> None:
    repo_root = Path(tmp_path)
    (repo_root / ".codex-autorunner" / "tickets").mkdir(parents=True, exist_ok=True)
    operator = build_ticket_flow_operator_service(repo_root)

    has_dispatch, reason = operator.resolve_paused_dispatch_state(
        record_status=FlowRunStatus.PAUSED,
        latest_payload={
            "seq": 1,
            "latest_seq": 2,
            "dispatch": {"mode": "pause", "is_handoff": True},
        },
        latest_reply_seq=0,
    )

    assert has_dispatch is False
    assert reason is not None
    assert "stale" in reason.lower()
    assert "no tickets remain" in reason.lower()


def test_ticket_flow_operator_build_run_state_flags_dead_worker(tmp_path: Path) -> None:
    repo_root = Path(tmp_path)
    _write_ticket(repo_root, "TICKET-001.md")
    run_id = "22222222-2222-2222-2222-222222222222"

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            metadata={},
            state={},
            current_step="ticket_turn",
        )
        store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
        record = store.get_flow_run(run_id)
        assert record is not None

        _write_dead_worker_artifacts(repo_root, run_id)
        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id="repo",
            record=record,
            store=store,
            has_pending_dispatch=False,
        )

    assert run_state["state"] == "dead"
    assert run_state["worker_status"] == "dead_unexpected"
    assert "Worker not running" in (run_state.get("blocking_reason") or "")
    assert run_state["crash"]["summary"]
