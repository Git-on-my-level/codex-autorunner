from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.flows.review.models import ReviewState, ReviewStatus
from codex_autorunner.surfaces.web.routes.review import build_review_routes


class _ReviewServiceStub:
    def __init__(self) -> None:
        self._running_state = ReviewState(
            id="run-123",
            status=ReviewStatus.RUNNING,
            agent="opencode",
            run_dir="/tmp/review/run-123",
            scratchpad_dir="/tmp/review/run-123/scratchpad",
            final_output_path="/tmp/review/run-123/final_report.md",
            prompt_kind="code",
        )

    def status(self):
        return self._running_state.with_runtime(running=True)

    def start(self, *, payload):
        _ = payload
        return self._running_state

    def stop(self):
        return self._running_state.request_stop()

    def reset(self):
        return ReviewState()


def test_review_routes_serialize_typed_state() -> None:
    app = FastAPI()
    app.state.review_manager = _ReviewServiceStub()
    app.include_router(build_review_routes())

    with TestClient(app) as client:
        status_res = client.get("/api/review/status")
        assert status_res.status_code == 200
        assert status_res.json()["review"]["status"] == "running"
        assert status_res.json()["review"]["running"] is True
        assert status_res.json()["review"]["prompt_kind"] == "code"

        start_res = client.post("/api/review/start", json={"agent": "opencode"})
        assert start_res.status_code == 200
        assert start_res.json() == {"status": "running", "detail": "Review started"}

        stop_res = client.post("/api/review/stop")
        assert stop_res.status_code == 200
        assert stop_res.json() == {"status": "stopping", "detail": "Review stopped"}

        reset_res = client.post("/api/review/reset")
        assert reset_res.status_code == 200
        assert reset_res.json() == {"status": "idle", "detail": "Review state reset"}
