import json
import sqlite3

from codex_autorunner.core.state import (
    RunnerState,
    SessionRecord,
    TerminalSessionStore,
    load_state,
    save_state,
)


def test_state_session_registry_roundtrip(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    record = SessionRecord(
        repo_path="/tmp/example",
        created_at="2025-01-01T00:00:00Z",
        last_seen_at="2025-01-01T00:01:00Z",
        status="active",
    )
    state = RunnerState(
        last_run_id=3,
        status="running",
        last_exit_code=None,
        last_run_started_at="2025-01-01T00:00:00Z",
        last_run_finished_at=None,
        runner_pid=1234,
        sessions={"abc": record},
        repo_to_session={"/tmp/example": "abc"},
    )
    save_state(state_path, state)
    loaded = load_state(state_path)

    assert loaded.sessions["abc"].repo_path == "/tmp/example"
    assert loaded.sessions["abc"].status == "active"
    assert loaded.repo_to_session["/tmp/example"] == "abc"


def test_terminal_session_store_agent_scoped_lookup_and_prune(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    store = TerminalSessionStore(state_path)

    store.create_or_touch(
        session_id="codex-1",
        repo_path="/tmp/example",
        agent="codex",
        now="2026-01-01T00:00:00Z",
    )
    store.create_or_touch(
        session_id="opencode-1",
        repo_path="/tmp/example",
        agent="opencode",
        now="2026-01-01T00:00:01Z",
    )

    assert store.lookup_for_repo("/tmp/example", agent="codex") == "codex-1"
    assert store.lookup_for_repo("/tmp/example", agent="opencode") == "opencode-1"

    assert store.close("opencode-1", now="2026-01-01T00:00:02Z") is True
    assert store.lookup_for_repo("/tmp/example", agent="opencode") is None

    state = load_state(state_path)
    assert state.sessions["opencode-1"].status == "closed"
    assert "/tmp/example:opencode" not in state.repo_to_session

    state.repo_to_session["missing"] = "does-not-exist"
    save_state(state_path, state)
    assert store.prune() == 1
    assert "missing" not in load_state(state_path).repo_to_session


def test_state_migrates_legacy_singular_model_override(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    state = RunnerState(
        last_run_id=None,
        status="idle",
        last_exit_code=None,
        last_run_started_at=None,
        last_run_finished_at=None,
    )
    save_state(state_path, state)

    conn = sqlite3.connect(state_path)
    try:
        conn.execute(
            "UPDATE runner_state SET overrides_json = ? WHERE id = 1",
            (
                json.dumps(
                    {
                        "autorunner_agent_override": "opencode",
                        "autorunner_model_override": "zai/legacy",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    loaded = load_state(state_path)
    assert loaded.autorunner_agent_override == "opencode"
    assert loaded.autorunner_model_overrides == {"opencode": "zai/legacy"}

    save_state(state_path, loaded)
    conn = sqlite3.connect(state_path)
    try:
        row = conn.execute(
            "SELECT overrides_json FROM runner_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    encoded = json.loads(row[0])
    assert "autorunner_model_override" not in encoded
    assert encoded["autorunner_model_overrides"] == {"opencode": "zai/legacy"}
