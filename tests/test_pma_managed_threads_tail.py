from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _seed_managed_thread_with_events(hub_env, app) -> tuple[str, str]:
    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread(
        agent="codex",
        workspace_root=hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        name="tail-test",
    )
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="tail prompt")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")

    events = AppServerEventBuffer(max_events_per_turn=10)
    app.state.app_server_events = events
    return managed_thread_id, managed_turn_id


def test_managed_thread_tail_snapshot_redacts_and_supports_cursor(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "echo sk-abcdefghijklmnopqrstuvwxyz123456",
                    },
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "echo done",
                        "exitCode": 0,
                    },
                },
            }
        )

    import asyncio

    asyncio.run(_seed())

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["turn_status"] == "running"
        assert payload["activity"] in {"running", "stalled"}
        assert isinstance(payload["events"], list)
        assert len(payload["events"]) == 2
        first = payload["events"][0]
        assert first["event_id"] == 1
        rendered_first = json.dumps(first, ensure_ascii=True)
        assert "sk-[REDACTED]" in rendered_first

        debug_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail",
            params={"level": "debug"},
        )
        assert debug_resp.status_code == 200
        debug_payload = debug_resp.json()
        debug_first = debug_payload["events"][0]
        raw = json.dumps(debug_first.get("raw", {}), ensure_ascii=True)
        assert "sk-[REDACTED]" in raw
        assert "abcdefghijklmnopqrstuvwxyz123456" not in raw

        cursor_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail",
            params={"since_event_id": 1},
        )
        assert cursor_resp.status_code == 200
        cursor_payload = cursor_resp.json()
        assert [event["event_id"] for event in cursor_payload["events"]] == [2]


def test_managed_thread_status_aggregates_thread_turn_and_progress(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, managed_turn_id = _seed_managed_thread_with_events(hub_env, app)
    store = PmaThreadStore(hub_env.hub_root)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "status-check"},
                },
            }
        )

    import asyncio

    asyncio.run(_seed())
    store.mark_turn_finished(
        managed_turn_id,
        status="ok",
        assistant_text="completed assistant output",
        backend_turn_id="backend-turn-1",
    )

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["managed_thread_id"] == managed_thread_id
        assert isinstance(payload.get("thread"), dict)
        assert isinstance(payload.get("turn"), dict)
        assert payload["turn"]["status"] == "ok"
        assert payload["is_alive"] is False
        assert isinstance(payload.get("recent_progress"), list)
        assert "completed assistant output" in payload.get("latest_output_excerpt", "")


def test_managed_thread_tail_stream_resumes_with_last_event_id(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, managed_turn_id = _seed_managed_thread_with_events(hub_env, app)
    store = PmaThreadStore(hub_env.hub_root)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "first"},
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "second"},
                },
            }
        )

    import asyncio

    asyncio.run(_seed())
    store.mark_turn_finished(
        managed_turn_id,
        status="ok",
        assistant_text="done",
        backend_turn_id="backend-turn-1",
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail/events",
            headers={"Last-Event-ID": "1"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: tail" in body
        assert "\nid: 2\n" in body
        assert "\nid: 1\n" not in body


def test_managed_thread_tail_stream_preserves_since_filter_for_live_events(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    import time

    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 10_000
    new_ms = now_ms

    class FakeEvents:
        async def list_events(
            self,
            thread_id: str,
            turn_id: str,
            *,
            after_id: int = 0,
            limit: int | None = None,
        ):
            _ = thread_id, turn_id, after_id, limit
            return []

        async def stream_entries(
            self,
            thread_id: str,
            turn_id: str,
            *,
            after_id: int = 0,
            heartbeat_interval: float = 15.0,
        ):
            _ = thread_id, turn_id, after_id, heartbeat_interval
            yield {
                "id": 1,
                "received_at": old_ms,
                "message": {
                    "method": "item/completed",
                    "params": {"item": {"type": "tool", "name": "old"}},
                },
            }
            yield {
                "id": 2,
                "received_at": new_ms,
                "message": {
                    "method": "item/completed",
                    "params": {"item": {"type": "tool", "name": "new"}},
                },
            }

    app.state.app_server_events = FakeEvents()

    with TestClient(app) as client:
        resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail/events",
            params={"since": "1s"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: tail" in body
        assert "\nid: 2\n" in body
        assert "\nid: 1\n" not in body
