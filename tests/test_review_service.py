from __future__ import annotations

from typing import Any

import pytest

from codex_autorunner.core.runtime import RuntimeContext
from codex_autorunner.flows.review import ReviewError, ReviewService
from codex_autorunner.flows.review import service as review_service_module
from codex_autorunner.flows.review.lifecycle import (
    InvalidReviewTransition,
    ReviewTrigger,
    ReviewTriggerKind,
    reduce_review_lifecycle,
)
from codex_autorunner.flows.review.models import ReviewState, ReviewStatus


class _ThreadStub:
    def __init__(
        self, target: Any = None, args: tuple[Any, ...] = (), daemon: bool = False
    ):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.alive = False

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


def _build_service(repo) -> ReviewService:
    return ReviewService(RuntimeContext(repo))


def test_review_module_imports() -> None:
    from codex_autorunner.flows.review import ReviewError, ReviewState, ReviewStatus

    assert ReviewError is not None
    assert ReviewService is not None
    assert ReviewState is not None
    assert ReviewStatus is not None


def test_review_status_recovers_interrupted_state(repo) -> None:
    service = _build_service(repo)
    service._save_state(
        ReviewState(
            id="run-123",
            status=ReviewStatus.RUNNING,
            stop_requested=True,
            started_at="2026-04-09T00:00:00Z",
            updated_at="2026-04-09T00:00:01Z",
        )
    )

    snapshot = service.status()

    assert snapshot.status is ReviewStatus.INTERRUPTED
    assert snapshot.running is False
    assert snapshot.stop_requested is False
    assert snapshot.last_error == "Recovered from restart"

    persisted = service._load_state()
    assert persisted.status is ReviewStatus.INTERRUPTED
    assert persisted.last_error == "Recovered from restart"


def test_review_status_rejects_unknown_persisted_status(repo) -> None:
    service = _build_service(repo)
    service._state_path.parent.mkdir(parents=True, exist_ok=True)
    service._state_path.write_text(
        '{"version": 1, "id": "run-123", "status": "mystery"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ReviewError, match="Review state is corrupted"):
        service.status()


def test_review_start_stop_and_reset_lifecycle(
    repo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = _build_service(repo)
    caplog.set_level("INFO", logger="codex_autorunner.review")

    monkeypatch.setattr(review_service_module.threading, "Thread", _ThreadStub)
    monkeypatch.setattr(service, "_acquire_lock", lambda: None)
    monkeypatch.setattr(service, "_release_lock", lambda: None)

    started = service.start(payload={"agent": "opencode"})

    assert started.status is ReviewStatus.RUNNING
    assert started.id is not None
    assert started.worker_id is not None
    assert started.run_dir is not None
    assert started.scratchpad_dir is not None
    assert started.final_output_path is not None
    assert any(
        record.message == "Review lifecycle transition"
        and record.run_id == started.id
        and record.from_status == "idle"
        and record.to_status == "running"
        and record.trigger == "start"
        for record in caplog.records
    )

    snapshot = service.status()
    assert snapshot.status is ReviewStatus.RUNNING
    assert snapshot.running is True

    stopped = service.stop()
    assert stopped.status is ReviewStatus.STOPPING
    assert stopped.stop_requested is True
    assert any(
        record.message == "Review lifecycle transition"
        and record.run_id == started.id
        and record.from_status == "running"
        and record.to_status == "stopping"
        and record.trigger == "request_stop"
        for record in caplog.records
    )

    assert service._thread is not None
    service._thread.alive = False

    reset = service.reset()
    assert reset.status is ReviewStatus.IDLE
    assert reset.id is None
    assert reset.stop_requested is False

    persisted = service._load_state()
    assert persisted.status is ReviewStatus.IDLE
    assert persisted.id is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"agent": "missing-agent"}, "Invalid agent 'missing-agent'"),
        ({"agent": "hermes"}, "does not support review"),
    ],
)
def test_review_start_rejects_invalid_or_unsupported_agent(
    repo, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], message: str
) -> None:
    service = _build_service(repo)

    monkeypatch.setattr(service, "_acquire_lock", lambda: None)
    monkeypatch.setattr(service, "_release_lock", lambda: None)

    with pytest.raises(ReviewError, match=message) as excinfo:
        service.start(payload=payload)

    assert excinfo.value.status_code == 400


def test_review_lifecycle_reducer_rejects_illegal_completion() -> None:
    state = ReviewState(status=ReviewStatus.IDLE)

    with pytest.raises(InvalidReviewTransition, match="mark_completed"):
        reduce_review_lifecycle(
            state,
            ReviewTrigger(
                kind=ReviewTriggerKind.MARK_COMPLETED,
                reason="cannot complete an idle review",
            ),
        )


def test_review_lifecycle_reducer_terminal_transitions() -> None:
    running = ReviewState(id="run-123", status=ReviewStatus.RUNNING)

    completed = reduce_review_lifecycle(
        running,
        ReviewTrigger(
            kind=ReviewTriggerKind.MARK_COMPLETED,
            reason="report written",
            scratchpad_bundle_path="/tmp/bundle.zip",
        ),
    )
    assert completed.from_status is ReviewStatus.RUNNING
    assert completed.to_status is ReviewStatus.COMPLETED
    assert completed.state.finished_at is not None
    assert completed.state.scratchpad_bundle_path == "/tmp/bundle.zip"

    failed = reduce_review_lifecycle(
        running,
        ReviewTrigger(
            kind=ReviewTriggerKind.MARK_FAILED,
            reason="agent error",
            error_message="boom",
        ),
    )
    assert failed.to_status is ReviewStatus.FAILED
    assert failed.state.last_error == "boom"

    stopped = reduce_review_lifecycle(
        ReviewState(id="run-123", status=ReviewStatus.STOPPING),
        ReviewTrigger(
            kind=ReviewTriggerKind.MARK_STOPPED,
            reason="interrupt completed",
        ),
    )
    assert stopped.to_status is ReviewStatus.STOPPED
