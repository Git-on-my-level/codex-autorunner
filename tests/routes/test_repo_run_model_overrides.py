from types import SimpleNamespace

from codex_autorunner.core.state import RunnerState, load_state, save_state
from codex_autorunner.surfaces.web.routes.repos import _apply_run_overrides
from codex_autorunner.surfaces.web.schemas import RunControlRequest


def _request_for_state(path):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=SimpleNamespace(state_path=path))))


def _idle_state(**kwargs) -> RunnerState:
    return RunnerState(
        last_run_id=None,
        status="idle",
        last_exit_code=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        **kwargs,
    )


def test_run_model_override_is_scoped_to_selected_agent(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    save_state(state_path, _idle_state(autorunner_model_overrides={"codex": "gpt-5.5"}))

    _apply_run_overrides(
        _request_for_state(state_path),
        RunControlRequest(agent="opencode", model="zai/settings-default"),
    )

    state = load_state(state_path)
    assert state.autorunner_model_overrides == {
        "codex": "gpt-5.5",
        "opencode": "zai/settings-default",
    }
    assert state.autorunner_model_override == "gpt-5.5"


def test_run_model_override_clears_only_selected_agent(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    save_state(
        state_path,
        _idle_state(
            autorunner_agent_override="opencode",
            autorunner_model_overrides={
                "codex": "gpt-5.5",
                "opencode": "zai/settings-default",
            },
        ),
    )

    _apply_run_overrides(
        _request_for_state(state_path),
        RunControlRequest(model=""),
    )

    state = load_state(state_path)
    assert state.autorunner_model_overrides == {"codex": "gpt-5.5"}
    assert state.autorunner_model_override == "gpt-5.5"
