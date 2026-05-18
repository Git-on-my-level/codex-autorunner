from __future__ import annotations

import pytest

from codex_autorunner.core.state import RunnerState, load_state, save_state
from codex_autorunner.surfaces.web.services.session_settings import (
    SessionSettingsError,
    normalize_session_settings_update,
    update_session_settings,
)


def _seed_state(path, **overrides) -> None:
    save_state(
        path,
        RunnerState(
            last_run_id=7,
            status="idle",
            last_exit_code=0,
            last_run_started_at="2026-05-18T01:00:00Z",
            last_run_finished_at="2026-05-18T01:01:00Z",
            **overrides,
        ),
    )


def test_session_settings_service_normalizes_runtime_preferences(tmp_path) -> None:
    state = RunnerState(None, "idle", None, None, None)

    settings = normalize_session_settings_update(
        {
            "autorunner_model_overrides": {
                " Codex ": " gpt-5.5 ",
                "HERMES": "",
            },
            "autorunner_effort_override": "",
            "autorunner_approval_policy": "unlessTrusted",
            "autorunner_sandbox_mode": "workspaceWrite",
            "autorunner_workspace_write_network": True,
            "ticket_flow_require_commit": False,
            "runner_stop_after_runs": 3,
        },
        state,
    )

    assert settings.to_response() == {
        "autorunner_model_overrides": {"codex": "gpt-5.5"},
        "autorunner_effort_override": None,
        "autorunner_approval_policy": "unlessTrusted",
        "autorunner_sandbox_mode": "workspaceWrite",
        "autorunner_workspace_write_network": True,
        "ticket_flow_require_commit": False,
        "runner_stop_after_runs": 3,
    }


def test_session_settings_service_rejects_invalid_modes() -> None:
    state = RunnerState(None, "idle", None, None, None)

    with pytest.raises(SessionSettingsError, match="approval policy"):
        normalize_session_settings_update(
            {"autorunner_approval_policy": "sometimes"}, state
        )

    with pytest.raises(SessionSettingsError, match="sandbox mode"):
        normalize_session_settings_update(
            {"autorunner_sandbox_mode": "readOnly"}, state
        )


def test_session_settings_service_allows_noop_updates_while_active(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed_state(
        state_path,
        autorunner_model_overrides={"codex": "gpt-5.5"},
        autorunner_approval_policy="never",
    )
    reset_calls: list[str] = []

    result = update_session_settings(
        state_path=state_path,
        updates={
            "autorunner_model_overrides": {"codex": "gpt-5.5"},
            "autorunner_approval_policy": "never",
        },
        is_run_active=lambda: True,
        reset_thread=reset_calls.append,
    )

    assert result.thread_reset_required is False
    assert result.thread_reset is False
    assert reset_calls == []
    assert load_state(state_path).autorunner_model_overrides == {"codex": "gpt-5.5"}


def test_session_settings_service_rejects_reset_required_update_while_active(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed_state(state_path, autorunner_model_overrides={"codex": "gpt-5.4"})

    with pytest.raises(SessionSettingsError) as exc_info:
        update_session_settings(
            state_path=state_path,
            updates={"autorunner_model_overrides": {"codex": "gpt-5.5"}},
            is_run_active=lambda: True,
            reset_thread=lambda _key: True,
        )

    assert exc_info.value.status_code == 409
    assert load_state(state_path).autorunner_model_overrides == {"codex": "gpt-5.4"}


def test_session_settings_service_persists_and_resets_thread_on_change(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed_state(
        state_path,
        autorunner_model_overrides={"codex": "gpt-5.4"},
        runner_pid=1234,
    )
    reset_calls: list[str] = []

    result = update_session_settings(
        state_path=state_path,
        updates={
            "autorunner_model_overrides": {"OPENCODE": " zai-coding-plan/glm-5.1 "},
            "runner_stop_after_runs": 5,
        },
        is_run_active=lambda: False,
        reset_thread=lambda key: not reset_calls.append(key),
    )

    state = load_state(state_path)
    assert result.thread_reset_required is True
    assert result.thread_reset is True
    assert reset_calls == ["autorunner"]
    assert state.last_run_id == 7
    assert state.runner_pid == 1234
    assert state.autorunner_model_overrides == {"opencode": "zai-coding-plan/glm-5.1"}
    assert state.runner_stop_after_runs == 5
