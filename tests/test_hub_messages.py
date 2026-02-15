from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.server import create_hub_app


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


def _seed_failed_run(repo_root: Path, run_id: str) -> None:
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
        store.update_flow_run_status(run_id, FlowRunStatus.FAILED)


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


def test_hub_messages_reconciles_replied_dispatches(hub_env) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)
    _write_reply_history(hub_env.repo_root, run_id, seq=1)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_keeps_unreplied_newer_dispatches(hub_env) -> None:
    run_id = "22222222-2222-2222-2222-222222222222"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=2)
    _write_reply_history(hub_env.repo_root, run_id, seq=1)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_paused_without_dispatch_emits_attention_item(hub_env) -> None:
    run_id = "44444444-4444-4444-4444-444444444444"
    _seed_paused_run(hub_env.repo_root, run_id)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_dead_worker_includes_crash_summary_and_open_url(hub_env) -> None:
    run_id = "99999999-9999-9999-9999-999999999999"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dead_worker_artifacts(hub_env.repo_root, run_id)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_surfaces_unreadable_latest_dispatch(hub_env) -> None:
    run_id = "55555555-5555-5555-5555-555555555555"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)
    _write_dispatch_history_raw(
        hub_env.repo_root,
        run_id,
        seq=2,
        content="---\nmode: invalid_mode\ntitle: Corrupt latest\n---\n\nbad dispatch\n",
    )

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_treats_turn_summary_as_non_actionable(hub_env) -> None:
    run_id = "12121212-1212-1212-1212-121212121212"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1, mode="turn_summary")

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_hides_older_paused_run_when_newer_run_exists(hub_env) -> None:
    older_run_id = "13131313-1313-1313-1313-131313131313"
    newer_run_id = "14141414-1414-1414-1414-141414141414"
    _seed_paused_run(hub_env.repo_root, older_run_id)
    _write_dispatch_history(hub_env.repo_root, older_run_id, seq=1)
    _seed_completed_run(hub_env.repo_root, newer_run_id)

    app = create_hub_app(hub_env.hub_root)
    with TestClient(app) as client:
        res = client.get("/hub/messages")
        assert res.status_code == 200
        assert res.json()["items"] == []


def test_hub_messages_dismiss_filters_and_persists(hub_env) -> None:
    run_id = "33333333-3333-3333-3333-333333333333"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_failed_run_appears_in_inbox(hub_env) -> None:
    run_id = "66666666-6666-6666-6666-666666666666"
    _seed_failed_run(hub_env.repo_root, run_id)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_dispatch_includes_lifecycle_metadata(hub_env) -> None:
    run_id = "77777777-7777-7777-7777-777777777777"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    app = create_hub_app(hub_env.hub_root)
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


def test_hub_messages_resolve_dismisses_non_dispatch_item(hub_env) -> None:
    run_id = "88888888-8888-8888-8888-888888888888"
    _seed_failed_run(hub_env.repo_root, run_id)

    app = create_hub_app(hub_env.hub_root)
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
            },
        )
        assert resolved.status_code == 200
        payload = resolved.json()
        assert payload["status"] == "ok"
        assert payload["resolved"]["run_id"] == run_id
        assert payload["resolved"]["item_type"] == "run_failed"
        assert payload["resolved"]["action"] == "dismiss"
        assert payload["resolved"]["reason"] == "cleanup"

        after = client.get("/hub/messages").json()["items"]
        assert after == []
