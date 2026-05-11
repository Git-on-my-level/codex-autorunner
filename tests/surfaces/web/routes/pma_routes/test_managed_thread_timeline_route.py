from __future__ import annotations

from fastapi.testclient import TestClient
from tests.pma_support import _enable_pma, _repo_owner

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.server import create_hub_app


def test_managed_thread_timeline_endpoint_returns_canonical_items(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        store = ManagedThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="hello timeline")
        assert store.mark_turn_finished(
            str(turn["managed_turn_id"]),
            status="ok",
            assistant_text="hello from assistant",
        )

        timeline_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/timeline")

    assert timeline_resp.status_code == 200
    payload = timeline_resp.json()
    assert payload["contract_version"] == "managed_thread_timeline.v1"
    assert [item["kind"] for item in payload["items"]] == [
        "user_message",
        "assistant_message",
        "status",
    ]
    assert payload["items"][0]["payload"]["text"] == "hello timeline"
    assert payload["items"][1]["payload"]["text"] == "hello from assistant"


def test_managed_thread_chat_events_endpoint_returns_snapshot(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        events_resp = client.get("/hub/pma/events?once=true")

    assert events_resp.status_code == 200
    assert events_resp.headers["content-type"].startswith("text/event-stream")
    body = events_resp.text
    assert "event: chat_snapshot" in body
    assert f'"managed_thread_id": "{managed_thread_id}"' in body
    assert '"contract_version": "pma_chat_events.v1"' in body
