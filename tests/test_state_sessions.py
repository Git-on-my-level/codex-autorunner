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
