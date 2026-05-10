import json
import sqlite3

from codex_autorunner.core.state import (
    RunnerState,
    SessionRecord,
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
