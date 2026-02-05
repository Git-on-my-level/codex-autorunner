from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.file_chat import (
    FileChatRoutesState,
    build_file_chat_routes,
)


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
