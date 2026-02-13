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


def _write_dispatch_history(repo_root: Path, run_id: str, seq: int) -> None:
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
        "---\nmode: pause\ntitle: Needs input\n---\n\nPlease review.\n",
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
        assert "unreadable dispatch metadata" in (item.get("reason") or "").lower()
        assert item.get("dispatch") is None
        run_state = item.get("run_state") or {}
        assert run_state.get("state") == "blocked"


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
