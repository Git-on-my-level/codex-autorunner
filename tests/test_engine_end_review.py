import pytest

import codex_autorunner.core.engine as engine_module
import codex_autorunner.flows.review as review_module
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.state import RunnerState, save_state


@pytest.mark.anyio
async def test_end_review_runs_and_attaches_artifacts(repo, monkeypatch) -> None:
    engine = Engine(repo)
    runner_review = engine.config.raw["runner"]["review"]
    runner_review["enabled"] = True
    runner_review["trigger"] = {
        "on_todos_complete": True,
        "on_no_progress_stop": False,
        "on_max_runs_stop": False,
        "on_stop_requested": False,
        "on_error_exit": False,
    }

    called: dict = {}
    monkeypatch.setattr(
        engine_module,
        "build_spec_progress_review_context",
        lambda *args, **kwargs: "context-md",
    )
    monkeypatch.setattr(engine, "_ensure_opencode_supervisor", lambda: object())

    class FakeReviewService:
        def __init__(self, *args, **kwargs) -> None:
            called["init_kwargs"] = kwargs

        async def run_blocking_async(self, **kwargs):
            called["kwargs"] = kwargs
            return {
                "id": "rvw123",
                "final_output_path": str(repo / "review.md"),
                "scratchpad_bundle_path": str(repo / "scratch.zip"),
            }

    monkeypatch.setattr(review_module, "ReviewService", FakeReviewService)

    save_state(
        engine.state_path,
        RunnerState(
            last_run_id=1,
            status="idle",
            last_exit_code=0,
            last_run_started_at=None,
            last_run_finished_at=None,
        ),
    )

    await engine._maybe_run_end_review(exit_reason="todos_complete", last_exit_code=0)

    assert called["kwargs"]["prompt_kind"] == "spec_progress"
    assert (
        called["kwargs"]["seed_context_files"]["AUTORUNNER_CONTEXT.md"] == "context-md"
    )
    entry = engine._load_run_index().get("1") or {}
    artifacts = entry.get("artifacts") or {}
    assert artifacts.get("final_review_report_path") == str(repo / "review.md")
    assert artifacts.get("final_review_scratchpad_bundle_path") == str(
        repo / "scratch.zip"
    )


@pytest.mark.anyio
async def test_end_review_skips_when_trigger_disabled(repo, monkeypatch) -> None:
    engine = Engine(repo)
    runner_review = engine.config.raw["runner"]["review"]
    runner_review["enabled"] = True
    runner_review["trigger"]["on_stop_requested"] = False

    called: dict = {}

    class FailReviewService:
        def __init__(self, *args, **kwargs) -> None:
            called["init_kwargs"] = kwargs

        async def run_blocking_async(self, **kwargs):
            called["kwargs"] = kwargs
            raise AssertionError("Should not be called")

    monkeypatch.setattr(review_module, "ReviewService", FailReviewService)
    monkeypatch.setattr(engine, "_ensure_opencode_supervisor", lambda: object())

    save_state(
        engine.state_path,
        RunnerState(
            last_run_id=2,
            status="idle",
            last_exit_code=0,
            last_run_started_at=None,
            last_run_finished_at=None,
        ),
    )

    await engine._maybe_run_end_review(exit_reason="stop_requested", last_exit_code=0)

    assert "kwargs" not in called
