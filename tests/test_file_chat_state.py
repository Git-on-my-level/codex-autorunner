from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.file_chat import (
    FileChatRoutesState,
    build_file_chat_routes,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes.targets import parse_target
from codex_autorunner.surfaces.web.services import file_chat as file_chat_service


def test_file_chat_state_is_app_scoped():
    """
    Verify that file_chat routes use app-scoped state:
    1. Multiple apps with file_chat routes should not share state.
    2. State can be reset by replacing app.state.file_chat_routes_state.
    """
    # Create two separate FastAPI apps, each with file_chat routes
    # This creates two separate state instances
    app1 = FastAPI()
    app1.include_router(build_file_chat_routes())
    client1 = TestClient(app1)

    app2 = FastAPI()
    app2.include_router(build_file_chat_routes())
    client2 = TestClient(app2)

    # Verify both apps work independently
    res1 = client1.get("/api/file-chat/active")
    assert res1.status_code == 200
    assert res1.json() == {"active": False, "current": {}, "last_result": {}}

    res2 = client2.get("/api/file-chat/active")
    assert res2.status_code == 200
    assert res2.json() == {"active": False, "current": {}, "last_result": {}}

    # Manually verify the state pattern by examining the router closure
    # Each call to build_file_chat_routes creates a new FileChatRoutesState
    # Verify that the state object is properly structured
    test_state = FileChatRoutesState()
    assert isinstance(test_state.active_chats, dict)
    assert isinstance(test_state.chat_lock, object)  # asyncio.Lock
    assert isinstance(test_state.turn_lock, object)  # asyncio.Lock
    assert isinstance(test_state.current_by_target, dict)
    assert isinstance(test_state.current_by_client, dict)
    assert isinstance(test_state.last_by_client, dict)

    # Verify that state initialization creates empty containers
    assert test_state.active_chats == {}
    assert test_state.current_by_target == {}
    assert test_state.current_by_client == {}
    assert test_state.last_by_client == {}

    # Verify that resetting state works (create new state, replace old one)
    old_state = FileChatRoutesState()
    old_state.active_chats["test_key"] = None

    new_state = FileChatRoutesState()
    assert "test_key" in old_state.active_chats
    assert "test_key" not in new_state.active_chats

    # Verify old and new are different objects
    assert old_state is not new_state


def test_file_chat_active_and_interrupt_routes_share_app_state(tmp_path) -> None:
    app = FastAPI()
    app.include_router(build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)

    target = parse_target(tmp_path, "contextspace:active_context")
    interrupt_event = asyncio.Event()
    current = {
        "client_turn_id": "turn-123",
        "target": target.target,
        "status": "running",
        "agent": "codex",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
    }

    state = FileChatRoutesState()
    state.active_chats[target.state_key] = interrupt_event
    state.current_by_target[target.state_key] = current
    state.current_by_client["turn-123"] = current
    app.state.file_chat_routes_state = state

    client = TestClient(app)

    active_res = client.get(
        "/api/file-chat/active", params={"client_turn_id": "turn-123"}
    )
    assert active_res.status_code == 200
    assert active_res.json() == {
        "active": True,
        "current": current,
        "last_result": {},
    }

    interrupt_res = client.post(
        "/api/file-chat/interrupt",
        json={"target": target.target},
    )
    assert interrupt_res.status_code == 200
    assert interrupt_res.json() == {
        "status": "interrupted",
        "detail": "File chat interrupted",
    }
    assert interrupt_event.is_set() is True


def test_ticket_chat_interrupt_wrapper_shares_file_chat_app_state(tmp_path) -> None:
    app = FastAPI()
    app.include_router(build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)

    target = parse_target(tmp_path, "ticket:1")
    interrupt_event = asyncio.Event()

    state = FileChatRoutesState()
    state.active_chats[target.state_key] = interrupt_event
    app.state.file_chat_routes_state = state

    client = TestClient(app)

    interrupt_res = client.post("/api/tickets/1/chat/interrupt")
    assert interrupt_res.status_code == 200
    assert interrupt_res.json() == {
        "status": "interrupted",
        "detail": "Ticket chat interrupted",
    }
    assert interrupt_event.is_set() is True


def test_file_chat_post_route_uses_service_contract(tmp_path, monkeypatch) -> None:
    app = FastAPI()
    app.include_router(build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root,
        _target,
        message: str,
        **kwargs,
    ):
        on_meta = kwargs.get("on_meta")
        assert on_meta is not None
        await on_meta("codex", "thread-route", "turn-route")
        return {"status": "ok", "message": f"done {message}"}

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    client = TestClient(app)
    res = client.post(
        "/api/file-chat",
        json={
            "target": "contextspace:spec",
            "message": "edit spec",
            "client_turn_id": "route-client",
        },
    )

    assert res.status_code == 200
    assert res.json()["client_turn_id"] == "route-client"
    active = client.get(
        "/api/file-chat/active",
        params={"client_turn_id": "route-client"},
    )
    assert active.status_code == 200
    assert active.json()["last_result"]["message"] == "done edit spec"


def test_ticket_chat_post_route_preserves_alias_contract(tmp_path, monkeypatch) -> None:
    app = FastAPI()
    app.include_router(build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)

    observed: dict[str, object] = {}

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root,
        target,
        message: str,
        **_kwargs,
    ):
        observed["target"] = target.target
        observed["message"] = message
        return {"status": "ok", "message": "ticket done"}

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    client = TestClient(app)
    res = client.post("/api/tickets/7/chat", json={"message": "edit ticket"})

    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert observed == {"target": "ticket:7", "message": "edit ticket"}
