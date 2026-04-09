from __future__ import annotations

from typing import Any

import pytest

from codex_autorunner.core.runtime import RuntimeContext
from codex_autorunner.flows.review import ReviewError, ReviewService
from codex_autorunner.flows.review import service as review_service_module
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


def test_review_start_stop_and_reset_lifecycle(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _build_service(repo)

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

    snapshot = service.status()
    assert snapshot.status is ReviewStatus.RUNNING
    assert snapshot.running is True

    stopped = service.stop()
    assert stopped.status is ReviewStatus.STOPPING
    assert stopped.stop_requested is True

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
