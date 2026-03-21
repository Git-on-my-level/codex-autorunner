from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.state import RunnerState, save_state
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web import app as web_app_module
from codex_autorunner.surfaces.web import app_state as web_app_state_module


def _seed_paused_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def _seed_failed_run(
    repo_root: Path,
    run_id: str,
    *,
    state: dict | None = None,
    error_message: str | None = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state=state or {},
            metadata={},
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.FAILED,
            state=state or {},
            error_message=error_message,
        )


def _seed_failed_worker_dead_run(repo_root: Path, run_id: str) -> None:
    _seed_failed_run(
        repo_root,
        run_id,
        state={
            "failure": {
                "failed_at": "2026-03-21T00:00:00Z",
                "failure_reason_code": "worker_dead",
                "failure_class": "worker_dead",
            }
        },
        error_message=(
            "Worker died (status=dead, pid=38621, reason: worker PID not running, "
            "exit_code=-15)"
        ),
    )


def _seed_completed_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)


def _write_dispatch_history(
    repo_root: Path, run_id: str, seq: int, *, mode: str = "pause"
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
    (entry_dir / "DISPATCH.md").write_text(
        f"---\nmode: {mode}\ntitle: Needs input\n---\n\nPlease review.\n",
        encoding="utf-8",
    )


def _write_dispatch_history_raw(
    repo_root: Path, run_id: str, seq: int, content: str
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
    (entry_dir / "DISPATCH.md").write_text(content, encoding="utf-8")


def _write_reply_history(repo_root: Path, run_id: str, seq: int) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "reply_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "USER_REPLY.md").write_text("Reply\n", encoding="utf-8")


def _write_ticket(repo_root: Path, ticket_name: str) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / ticket_name).write_text(
        ("---\n" f"title: {ticket_name}\n" "done: false\n" "---\n\n" "Body\n"),
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
                "stderr_tail": "",
                "exception": "RepoNotFoundError: cwd mismatch",
                "stack_trace": "Traceback ...",
            }
        ),
        encoding="utf-8",
    )


def _build_hub_messages_app(hub_root: Path, monkeypatch) -> object:
    monkeypatch.setattr(
        web_app_module,
        "reap_managed_processes",
        lambda _root: SimpleNamespace(killed=0, signaled=0, removed=0, skipped=0),
    )
    monkeypatch.setattr(
        web_app_state_module,
        "build_opencode_supervisor_from_repo_config",
        lambda *args, **kwargs: object(),
    )
    return create_hub_app(hub_root)


def _assert_canonical_state_v1(
    item: dict,
    *,
    repo_id: str,
    repo_root: Path,
    run_id: str,
    run_status: str,
    state: str,
) -> None:
    canonical = item.get("canonical_state_v1") or {}
    assert canonical.get("schema_version") == 1
    assert canonical.get("repo_id") == repo_id
    assert canonical.get("repo_root") == str(repo_root)
    assert canonical.get("represented_run_id") == run_id
    assert canonical.get("latest_run_id") == run_id
    assert canonical.get("latest_run_status") == run_status
    assert canonical.get("state") == state
    assert canonical.get("ingest_source") == "ticket_files"
    assert isinstance(canonical.get("recommended_actions"), list)
    assert canonical.get("recommendation_confidence") in {"high", "medium", "low"}
    assert canonical.get("observed_at")
    assert canonical.get("recommendation_generated_at")
    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at")
    assert freshness.get("recency_basis")
    assert freshness.get("basis_at")
    assert isinstance(freshness.get("is_stale"), bool)


def test_hub_messages_reconciles_replied_dispatches(hub_env, monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)
    _write_reply_history(hub_env.repo_root, run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["item_type"] == "run_state_attention"
        assert items[0]["run_id"] == run_id
        assert "already replied" in (items[0].get("reason") or "").lower()
        run_state = items[0].get("run_state") or {}
        assert run_state.get("state") == "blocked"
        assert run_state.get("recommended_action")
        assert run_state.get("recommended_actions")
        assert isinstance(run_state.get("recommended_actions"), list)
        assert run_state.get("attention_required") is True
        _assert_canonical_state_v1(
            items[0],
            repo_id=hub_env.repo_id,
            repo_root=hub_env.repo_root,
            run_id=run_id,
            run_status="paused",
            state="blocked",
        )


def test_hub_messages_response_includes_top_level_freshness(
    hub_env, monkeypatch
) -> None:
    run_id = "90909090-1111-2222-3333-444444444444"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        payload = res.json()
        assert payload.get("generated_at")
        freshness = payload.get("freshness") or {}
        assert freshness.get("generated_at")
        inbox_section = (freshness.get("sections") or {}).get("inbox") or {}
        assert inbox_section.get("entity_count") == 1
        assert inbox_section.get("fresh_count") >= 1


def test_hub_messages_keeps_unreplied_newer_dispatches(hub_env, monkeypatch) -> None:
    run_id = "22222222-2222-2222-2222-222222222222"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=2)
    _write_reply_history(hub_env.repo_root, run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["run_id"] == run_id
        assert items[0]["seq"] == 2
        assert items[0]["item_type"] == "run_dispatch"
        assert items[0]["dispatch_actionable"] is True
        run_state = items[0].get("run_state") or {}
        assert run_state.get("state") == "paused"
        assert run_state.get("recommended_action")
        assert run_state.get("recommended_actions")
        assert isinstance(run_state.get("recommended_actions"), list)
        assert run_state.get("attention_required") is True
        _assert_canonical_state_v1(
            items[0],
            repo_id=hub_env.repo_id,
            repo_root=hub_env.repo_root,
            run_id=run_id,
            run_status="paused",
            state="paused",
        )


def test_hub_messages_hide_obsolete_failed_run_when_newer_completed_exists(
    hub_env,
    monkeypatch,
) -> None:
    _seed_failed_run(hub_env.repo_root, "older-failed")
    _seed_completed_run(hub_env.repo_root, "newer-completed")
    save_state(
        hub_env.repo_root / ".codex-autorunner" / "state.sqlite3",
        RunnerState(
            last_run_id="older-failed",
            status="running",
            last_exit_code=None,
            last_run_started_at="2026-03-10T00:00:00+00:00",
            last_run_finished_at=None,
        ),
    )

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        assert res.json()["items"] == []


def test_hub_messages_paused_without_dispatch_emits_attention_item(
    hub_env, monkeypatch
) -> None:
    run_id = "44444444-4444-4444-4444-444444444444"
    _seed_paused_run(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["item_type"] == "run_state_attention"
        assert item["run_id"] == run_id
        assert item["dispatch_actionable"] is False
        assert (
            "paused without an actionable dispatch"
            in (item.get("reason") or "").lower()
        )
        run_state = item.get("run_state") or {}
        assert run_state.get("state") == "blocked"
        assert run_state.get("recommended_action")
        assert run_state.get("recommended_actions")
        assert isinstance(run_state.get("recommended_actions"), list)
        assert run_state.get("attention_required") is True


def test_hub_messages_dead_worker_includes_crash_summary_and_open_url(
    hub_env, monkeypatch
) -> None:
    run_id = "99999999-9999-9999-9999-999999999999"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dead_worker_artifacts(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        run_state = items[0].get("run_state") or {}
        assert run_state.get("state") == "dead"
        crash = run_state.get("crash") or {}
        assert "RepoNotFoundError" in (crash.get("summary") or "")
        assert crash.get("open_url") == (
            f"/repos/{hub_env.repo_id}/api/flows/{run_id}/artifact?kind=worker_crash"
        )
        assert crash.get("path") == f".codex-autorunner/flows/{run_id}/crash.json"


def test_hub_messages_surfaces_unreadable_latest_dispatch(hub_env, monkeypatch) -> None:
    run_id = "55555555-5555-5555-5555-555555555555"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)
    _write_dispatch_history_raw(
        hub_env.repo_root,
        run_id,
        seq=2,
        content="---\nmode: invalid_mode\ntitle: Corrupt latest\n---\n\nbad dispatch\n",
    )

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["item_type"] == "run_state_attention"
        assert item["run_id"] == run_id
        assert item["seq"] == 2
        assert item["dispatch_actionable"] is False
        assert "unreadable dispatch metadata" in (item.get("reason") or "").lower()
        assert item.get("dispatch") is None
        run_state = item.get("run_state") or {}
        assert run_state.get("state") == "blocked"


def test_hub_messages_treats_turn_summary_as_non_actionable(
    hub_env, monkeypatch
) -> None:
    run_id = "12121212-1212-1212-1212-121212121212"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1, mode="turn_summary")

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["item_type"] == "run_state_attention"
        assert item["next_action"] == "inspect_and_resume"
        assert item["dispatch_actionable"] is False
        assert (item.get("dispatch") or {}).get("mode") == "turn_summary"
        assert "informational" in (item.get("reason") or "").lower()


def test_hub_messages_hides_older_paused_run_when_newer_run_exists(
    hub_env, monkeypatch
) -> None:
    older_run_id = "13131313-1313-1313-1313-131313131313"
    newer_run_id = "14141414-1414-1414-1414-141414141414"
    _seed_paused_run(hub_env.repo_root, older_run_id)
    _write_dispatch_history(hub_env.repo_root, older_run_id, seq=1)
    _seed_completed_run(hub_env.repo_root, newer_run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        assert res.json()["items"] == []


def test_hub_messages_dismiss_filters_and_persists(hub_env, monkeypatch) -> None:
    run_id = "33333333-3333-3333-3333-333333333333"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        before = client.get("/hub/messages").json()["items"]
        assert len(before) == 1
        assert before[0]["run_id"] == run_id

        dismiss = client.post(
            "/hub/messages/dismiss",
            json={
                "repo_id": hub_env.repo_id,
                "run_id": run_id,
                "seq": 1,
                "reason": "resolved elsewhere",
            },
        )
        assert dismiss.status_code == 200
        payload = dismiss.json()
        assert payload["status"] == "ok"
        assert payload["dismissed"]["reason"] == "resolved elsewhere"

        after = client.get("/hub/messages").json()["items"]
        assert after == []

    dismissals_path = (
        hub_env.repo_root / ".codex-autorunner" / "hub_inbox_dismissals.json"
    )
    data = json.loads(dismissals_path.read_text(encoding="utf-8"))
    assert data["items"][f"{run_id}:1"]["reason"] == "resolved elsewhere"


def test_hub_messages_failed_run_appears_in_inbox(hub_env, monkeypatch) -> None:
    run_id = "66666666-6666-6666-6666-666666666666"
    _seed_failed_run(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["run_id"] == run_id
        assert item["item_type"] == "run_failed"
        assert item["next_action"] == "diagnose_or_restart"
        run_state = item.get("run_state") or {}
        assert run_state.get("state") == "blocked"
        assert run_state.get("attention_required") is False
        assert run_state.get("worker_status") == "exited_expected"
        assert "available_actions" in item
        assert item.get("resolution_state") == "terminal_attention"
        assert "dismiss" in (item.get("resolvable_actions") or [])


def test_hub_messages_suppress_stale_failed_worker_dead_run_when_no_tickets_remain(
    hub_env, monkeypatch
) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()

    run_id = "6a6a6a6a-6a6a-6a6a-6a6a-6a6a6a6a6a6a"
    _seed_failed_worker_dead_run(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        assert res.json()["items"] == []


def test_hub_messages_keep_failed_worker_dead_run_when_tickets_remain(
    hub_env, monkeypatch
) -> None:
    _write_ticket(hub_env.repo_root, "TICKET-001.md")

    run_id = "6b6b6b6b-6b6b-6b6b-6b6b-6b6b6b6b6b6b"
    _seed_failed_worker_dead_run(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["run_id"] == run_id
        assert item["item_type"] == "run_failed"
        assert item["next_action"] == "diagnose_or_restart"


def test_hub_messages_multi_run_items_keep_canonical_run_identity(
    hub_env, monkeypatch
) -> None:
    older_failed_run_id = "aaaaaaa1-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    newer_paused_run_id = "bbbbbbb2-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
    _seed_failed_run(hub_env.repo_root, older_failed_run_id)
    _seed_paused_run(hub_env.repo_root, newer_paused_run_id)
    _write_dispatch_history(hub_env.repo_root, newer_paused_run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        paused_item = items[0]
        assert paused_item.get("run_id") == newer_paused_run_id
        assert paused_item.get("item_type") == "run_dispatch"
        paused_canonical = paused_item.get("canonical_state_v1") or {}
        assert paused_canonical.get("represented_run_id") == newer_paused_run_id
        assert paused_canonical.get("latest_run_id") == newer_paused_run_id


def test_hub_messages_dispatch_includes_lifecycle_metadata(
    hub_env, monkeypatch
) -> None:
    run_id = "77777777-7777-7777-7777-777777777777"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["item_type"] == "run_dispatch"
        assert item.get("resolution_state") == "pending_dispatch"
        assert "reply_resume" in (item.get("resolvable_actions") or [])
        assert "dismiss" in (item.get("resolvable_actions") or [])


def test_hub_messages_resolve_dismisses_non_dispatch_item(hub_env, monkeypatch) -> None:
    run_id = "88888888-8888-8888-8888-888888888888"
    _seed_failed_run(hub_env.repo_root, run_id)

    app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
    with TestClient(app) as client:
        before = client.get("/hub/messages").json()["items"]
        assert len(before) == 1
        assert before[0]["item_type"] == "run_failed"

        resolved = client.post(
            "/hub/messages/resolve",
            json={
                "repo_id": hub_env.repo_id,
                "run_id": run_id,
                "item_type": "run_failed",
                "action": "dismiss",
                "reason": "cleanup",
                "actor": "user-supplied-spoof",
            },
        )
        assert resolved.status_code == 200
        payload = resolved.json()
        assert payload["status"] == "ok"
        assert payload["resolved"]["run_id"] == run_id
        assert payload["resolved"]["item_type"] == "run_failed"
        assert payload["resolved"]["action"] == "dismiss"
        assert payload["resolved"]["reason"] == "cleanup"
        assert payload["resolved"]["resolved_by"] == "hub_messages_resolve"

        after = client.get("/hub/messages").json()["items"]
        assert after == []

    dismissals_path = (
        hub_env.repo_root / ".codex-autorunner" / "hub_inbox_dismissals.json"
    )
    data = json.loads(dismissals_path.read_text(encoding="utf-8"))
    stored = data["items"][f"{run_id}:run_failed"]
    assert stored["resolved_by"] == "hub_messages_resolve"


class TestIssue975CharacterizationHubMessageFreshness:
    """Characterization tests for hub-message freshness/supersession (issue #975).

    These tests document the current baseline for:
    - Freshness metadata attached to inbox items
    - Supersession rules when multiple runs exist for the same repo
    - canonical_state_v1 structure and fields

    Later tickets for issue #975 will build on this baseline to ensure
    stale/superseded state is clearly marked in PMA-facing surfaces.
    """

    def test_inbox_item_includes_canonical_state_v1_freshness(
        self, hub_env, monkeypatch
    ) -> None:
        """Document canonical_state_v1 freshness structure on inbox items."""
        run_id = "1a1a1a1a-1a1a-1a1a-1a1a-1a1a1a1a1a1a"
        _seed_paused_run(hub_env.repo_root, run_id)
        _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1

            item = items[0]
            canonical = item.get("canonical_state_v1") or {}
            freshness = canonical.get("freshness") or {}

            assert canonical.get("schema_version") == 1
            assert canonical.get("repo_id") == hub_env.repo_id
            assert canonical.get("repo_root") == str(hub_env.repo_root)
            assert canonical.get("ingest_source") == "ticket_files"
            assert canonical.get("latest_run_id") == run_id
            assert canonical.get("latest_run_status") == "paused"
            assert canonical.get("state") == "paused"

            assert freshness.get("generated_at")
            assert freshness.get("recency_basis")
            assert freshness.get("basis_at")
            assert isinstance(freshness.get("is_stale"), bool)

    def test_supersession_hides_older_paused_when_newer_completed_exists(
        self, hub_env, monkeypatch
    ) -> None:
        """Document that older paused runs are hidden when newer completed runs exist."""
        older_paused_id = "2b2b2b2b-2b2b-2b2b-2b2b-2b2b2b2b2b2b"
        newer_completed_id = "3c3c3c3c-3c3c-3c3c-3c3c-3c3c3c3c3c3c"
        _seed_paused_run(hub_env.repo_root, older_paused_id)
        _write_dispatch_history(hub_env.repo_root, older_paused_id, seq=1)
        _seed_completed_run(hub_env.repo_root, newer_completed_id)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]

            assert items == []

    def test_supersession_shows_only_newest_run_for_repo(
        self, hub_env, monkeypatch
    ) -> None:
        """Document that only the newest run is shown per repo."""
        run_1 = "4d4d4d4d-4d4d-4d4d-4d4d-4d4d4d4d4d4d"
        run_2 = "5e5e5e5e-5e5e-5e5e-5e5e-5e5e5e5e5e5e"
        run_3 = "6f6f6f6f-6f6f-6f6f-6f6f-6f6f6f6f6f6f"
        _seed_paused_run(hub_env.repo_root, run_1)
        _seed_failed_run(hub_env.repo_root, run_2)
        _seed_paused_run(hub_env.repo_root, run_3)
        _write_dispatch_history(hub_env.repo_root, run_3, seq=1)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]

            assert len(items) == 1
            assert items[0]["run_id"] == run_3
            assert items[0]["item_type"] == "run_dispatch"

    def test_top_level_freshness_includes_section_counts(
        self, hub_env, monkeypatch
    ) -> None:
        """Document top-level freshness payload with section counts."""
        run_id = "7g7g7g7g-7g7g-7g7g-7g7g-7g7g7g7g7g7g"
        _seed_paused_run(hub_env.repo_root, run_id)
        _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            payload = res.json()

            assert payload.get("generated_at")
            freshness = payload.get("freshness") or {}
            assert freshness.get("generated_at")

            inbox_section = (freshness.get("sections") or {}).get("inbox") or {}
            assert inbox_section.get("entity_count") == 1
            assert inbox_section.get("fresh_count") >= 1
            assert inbox_section.get("stale_count") is not None

    def test_canonical_state_v1_recommended_actions_structure(
        self, hub_env, monkeypatch
    ) -> None:
        """Document canonical_state_v1 recommended_actions and confidence."""
        run_id = "8h8h8h8h-8h8h-8h8h-8h8h-8h8h8h8h8h8h"
        _seed_paused_run(hub_env.repo_root, run_id)
        _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1

            item = items[0]
            canonical = item.get("canonical_state_v1") or {}

            assert isinstance(canonical.get("recommended_actions"), list)
            assert len(canonical.get("recommended_actions") or []) > 0
            assert canonical.get("recommended_action")
            assert canonical.get("recommendation_confidence") in {
                "high",
                "medium",
                "low",
            }
            assert canonical.get("observed_at")
            assert canonical.get("recommendation_generated_at")

    def test_run_state_fields_on_inbox_items(self, hub_env, monkeypatch) -> None:
        """Document run_state fields present on inbox items."""
        run_id = "9i9i9i9i-9i9i-9i9i-9i9i-9i9i9i9i9i9i"
        _seed_paused_run(hub_env.repo_root, run_id)
        _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1

            item = items[0]
            run_state = item.get("run_state") or {}

            assert run_state.get("state") == "paused"
            assert run_state.get("recommended_action")
            assert run_state.get("recommended_actions")
            assert isinstance(run_state.get("recommended_actions"), list)
            assert run_state.get("attention_required") is True

    def test_failed_run_item_type_and_next_action(self, hub_env, monkeypatch) -> None:
        """Document item_type and next_action for failed runs."""
        run_id = "0j0j0j0j-0j0j-0j0j-0j0j-0j0j0j0j0j0j"
        _seed_failed_run(hub_env.repo_root, run_id)

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1

            item = items[0]
            assert item["item_type"] == "run_failed"
            assert item["next_action"] == "diagnose_or_restart"
            assert item["resolution_state"] == "terminal_attention"
            assert "dismiss" in (item.get("resolvable_actions") or [])

    def test_hub_messages_item_carries_shared_action_queue_metadata(
        self, hub_env, monkeypatch
    ) -> None:
        run_id = "1k1k1k1k-1k1k-1k1k-1k1k-1k1k1k1k1k1k"
        _seed_paused_run(hub_env.repo_root, run_id)
        _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

        inbox_dir = hub_env.hub_root / ".codex-autorunner" / "filebox" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / "ticket-pack.md").write_text("ticket payload\n", encoding="utf-8")

        PmaThreadStore(hub_env.hub_root).create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="queue-metadata-thread",
        )

        supervisor = HubSupervisor.from_path(hub_env.hub_root)
        try:
            supervisor.get_pma_automation_store().enqueue_wakeup(
                source="lifecycle_subscription",
                repo_id=hub_env.repo_id,
                run_id=run_id,
                reason="flow_paused",
                timestamp="2026-03-16T12:30:00Z",
                idempotency_key="hub-message-queue-metadata",
            )
        finally:
            supervisor.shutdown()

        app = _build_hub_messages_app(hub_env.hub_root, monkeypatch)
        with TestClient(app) as client:
            res = client.get("/hub/messages")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1

            item = items[0]
            assert item["queue_source"] == "ticket_flow_inbox"
            assert item["action_queue_id"].startswith("ticket_flow_inbox:")
            assert item["precedence"]["rank"] == 10
            assert item["recommended_action"] == "reply_and_resume"
            assert item["supersession"]["status"] == "primary"
            assert item["supersession"]["is_primary"] is True
