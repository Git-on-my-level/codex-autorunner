from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.scm_events import ScmEventStore


def test_record_event_and_list_filtered_events(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    first = store.record_event(
        provider="github",
        event_type="pull_request",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        delivery_id="delivery-1",
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:00:01Z",
        payload={"action": "opened"},
        raw_payload={"pull_request": {"number": 17}},
    )
    second = store.record_event(
        provider="github",
        event_type="issue_comment",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        delivery_id="delivery-2",
        occurred_at="2026-03-25T00:05:00Z",
        received_at="2026-03-25T00:05:01Z",
        payload={"action": "created"},
    )

    listed = store.list_events(provider="github", repo_slug="acme/widgets", limit=10)

    assert [event.event_id for event in listed] == [second.event_id, first.event_id]
    assert listed[0].payload == {"action": "created"}
    assert listed[0].raw_payload is None

    by_delivery = store.list_events(delivery_id="delivery-1", limit=10)
    assert [event.event_id for event in by_delivery] == [first.event_id]

    by_pr = store.list_events(
        provider="github",
        event_type="pull_request",
        pr_number=17,
        occurred_after="2026-03-24T23:59:59Z",
        occurred_before="2026-03-25T00:00:00Z",
        limit=10,
    )
    assert [event.event_id for event in by_pr] == [first.event_id]
    assert by_pr[0].raw_payload == {"pull_request": {"number": 17}}


def test_record_event_rejects_oversized_raw_payload(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    try:
        store.record_event(
            provider="github",
            event_type="pull_request",
            raw_payload={"blob": "x" * 128},
            max_raw_payload_bytes=32,
        )
    except ValueError as exc:
        assert str(exc) == "raw_payload exceeds max_raw_payload_bytes"
    else:  # pragma: no cover
        raise AssertionError("expected oversized raw payload to be rejected")


def test_record_event_canonicalizes_timestamps_and_filters_in_utc_order(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)

    first = store.record_event(
        provider="github",
        event_type="pull_request",
        delivery_id="delivery-1",
        occurred_at="2026-03-25T01:30:00+01:00",
        received_at="2026-03-25T01:31:00+01:00",
        payload={"action": "opened"},
    )
    second = store.record_event(
        provider="github",
        event_type="pull_request",
        delivery_id="delivery-2",
        occurred_at="2026-03-25T00:45:00Z",
        received_at="2026-03-25T00:46:00Z",
        payload={"action": "synchronize"},
    )

    assert first.occurred_at == "2026-03-25T00:30:00Z"
    assert first.received_at == "2026-03-25T00:31:00Z"

    listed = store.list_events(
        provider="github",
        occurred_after="2026-03-25T01:15:00+01:00",
        occurred_before="2026-03-25T00:45:00Z",
        limit=10,
    )

    assert [event.event_id for event in listed] == [second.event_id, first.event_id]
    assert [event.occurred_at for event in listed] == [
        "2026-03-25T00:45:00Z",
        "2026-03-25T00:30:00Z",
    ]
