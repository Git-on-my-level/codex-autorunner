from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.scm_events import ScmEvent, list_events
from codex_autorunner.surfaces.web.services import scm_webhooks
from codex_autorunner.surfaces.web.services.scm_webhooks import (
    ScmWebhookIngestRequest,
    ensure_managed_thread_queue_workers_for_scm_operations,
    ingest_scm_webhook_event,
)


def _event(delivery_id: str = "delivery-1") -> ScmEvent:
    return ScmEvent(
        event_id=f"github:{delivery_id}",
        provider="github",
        event_type="pull_request",
        source="webhook",
        occurred_at="2026-03-25T10:00:00Z",
        received_at="2026-03-25T10:00:01Z",
        created_at="2026-03-25T10:00:01Z",
        repo_slug="acme/widgets",
        repo_id="99",
        pr_number=42,
        delivery_id=delivery_id,
        payload={"action": "opened"},
        raw_payload={"action": "opened"},
    )


def _request(
    hub_root: Path,
    *,
    event: ScmEvent | None = None,
    drain_inline: bool = True,
    app_drain_callback=None,
    raw_config: object | None = None,
) -> ScmWebhookIngestRequest:
    return ScmWebhookIngestRequest(
        hub_root=hub_root,
        raw_config=raw_config or {"github": {"automation": {"enabled": True}}},
        event=event or _event(),
        headers={},
        store_raw_payload=True,
        max_raw_payload_bytes=65_536,
        drain_inline=drain_inline,
        request_context=SimpleNamespace(),
        app=SimpleNamespace(),
        app_drain_callback=app_drain_callback,
    )


@pytest.mark.asyncio
async def test_ingest_scm_webhook_event_accepts_and_runs_inline_drain(
    tmp_path: Path,
) -> None:
    drained: list[str] = []

    def _drain(_request_context, event: ScmEvent) -> None:
        drained.append(event.event_id)

    outcome = await ingest_scm_webhook_event(
        _request(tmp_path, app_drain_callback=_drain)
    )

    assert outcome.to_response_payload() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": True,
    }
    assert drained == ["github:delivery-1"]
    assert [event.event_id for event in list_events(tmp_path, provider="github")] == [
        "github:delivery-1"
    ]


@pytest.mark.asyncio
async def test_ingest_scm_webhook_event_skips_drain_when_inline_disabled(
    tmp_path: Path,
) -> None:
    drained: list[str] = []

    outcome = await ingest_scm_webhook_event(
        _request(
            tmp_path,
            drain_inline=False,
            app_drain_callback=lambda _request_context, event: drained.append(
                event.event_id
            ),
        )
    )

    assert outcome.status == "accepted"
    assert outcome.drained_inline is False
    assert drained == []


@pytest.mark.asyncio
async def test_ingest_scm_webhook_event_reports_publish_executor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ServiceStub:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        def ingest_event(self, event, *, execute_automation_jobs: bool = True):
            _ = event, execute_automation_jobs
            return SimpleNamespace(automation_jobs=("job-1",))

        def process_scm_automation_jobs(self, *, automation_jobs=()):
            _ = automation_jobs

        def process_now(self, limit: int = 10):
            _ = limit
            raise RuntimeError("publish executor unavailable")

    monkeypatch.setattr(scm_webhooks, "ScmAutomationService", _ServiceStub)

    outcome = await ingest_scm_webhook_event(_request(tmp_path))

    assert outcome.status == "accepted"
    assert outcome.drained_inline is False
    assert outcome.drain_error == "inline_drain_failed"


@pytest.mark.asyncio
async def test_ingest_scm_webhook_event_reports_repeated_delivery_idempotency(
    tmp_path: Path,
) -> None:
    first = await ingest_scm_webhook_event(_request(tmp_path, drain_inline=False))
    second = await ingest_scm_webhook_event(_request(tmp_path, drain_inline=False))

    assert first.deduped is False
    assert second.to_response_payload()["deduped"] is True
    assert len(list_events(tmp_path, provider="github")) == 1


def test_ensure_managed_thread_queue_workers_reports_enqueue_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _ensure(_app, thread_target_id: str) -> None:
        calls.append(thread_target_id)

    monkeypatch.setattr(scm_webhooks, "ensure_managed_thread_queue_worker", _ensure)

    outcome = ensure_managed_thread_queue_workers_for_scm_operations(
        SimpleNamespace(),
        [
            SimpleNamespace(
                operation_id="op-1",
                operation_kind="enqueue_managed_turn",
                state="succeeded",
                response={"thread_target_id": "thread-1"},
            ),
            SimpleNamespace(
                operation_id="op-2",
                operation_kind="enqueue_managed_turn",
                state="effect_applied",
                response={"thread_target_id": "thread-1"},
            ),
            SimpleNamespace(
                operation_id="op-3",
                operation_kind="github_comment",
                state="succeeded",
                response={},
            ),
        ],
    )

    assert calls == ["thread-1"]
    assert outcome.processed_operation_count == 3
    assert outcome.ensured_managed_thread_ids == ("thread-1",)
    assert outcome.ensure_worker_errors == ()
