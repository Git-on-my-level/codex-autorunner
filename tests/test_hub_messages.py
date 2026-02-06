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
        assert res.json()["items"] == []


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
