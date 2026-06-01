from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.scm_events import ScmEventStore


def test_record_event_and_list_filtered_events(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    first = store.record_event(
        provider="github",
        event_type="pull_request",
        source="webhook",
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
        source="webhook",
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
        source="webhook",
        pr_number=17,
        occurred_after="2026-03-24T23:59:59Z",
        occurred_before="2026-03-25T00:00:00Z",
        limit=10,
    )
    assert [event.event_id for event in by_pr] == [first.event_id]
    assert by_pr[0].raw_payload == {"pull_request": {"number": 17}}


def test_record_event_if_new_returns_none_for_existing_event_id(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    created = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:comment-1",
        provider="github",
        event_type="pull_request_review_comment",
        source="polling",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:00:01Z",
        payload={"action": "created", "comment_id": "comment-1"},
    )
    duplicate = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:comment-1",
        provider="github",
        event_type="pull_request_review_comment",
        source="polling",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:05:00Z",
        payload={"action": "created", "comment_id": "comment-1"},
    )

    assert created is not None
    assert duplicate is None
    assert [event.event_id for event in store.list_events(limit=10)] == [
        "github:poll:pull_request_review_comment:comment-1"
    ]


def test_dedupe_key_is_content_derived_not_event_id(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)
    event_type = "pull_request_review_comment"
    repo_slug = "acme/widgets"
    pr_number = 17
    comment_id = "3330969470"
    event_id = f"github:poll:{event_type}:7f81b70789930f207325f5096a1eae3e"

    created = store.record_event_if_new(
        event_id=event_id,
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key=event_id,
        repo_slug=repo_slug,
        repo_id="repo-1",
        pr_number=pr_number,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:00:01Z",
        payload={"action": "created", "comment_id": comment_id},
    )

    assert created is not None
    assert created.dedupe_key != event_id
    assert created.dedupe_key == (
        f"{event_type}:{repo_slug}:{pr_number}:{comment_id}:unbound"
    )
    assert "7f81b70789930f207325f5096a1eae3e" not in str(created.dedupe_key)


def test_rotating_event_ids_produce_same_dedupe_key(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)
    event_type = "pull_request_review_comment"
    repo_slug = "acme/widgets"
    pr_number = 17
    comment_id = "3330969470"
    dedupe_key = f"{event_type}:{repo_slug}:{pr_number}:{comment_id}:unbound"

    created = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:7f81b70789930f207325f5096a1eae3e",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key="github:poll:pull_request_review_comment:7f81b70789930f207325f5096a1eae3e",
        repo_slug=repo_slug,
        repo_id="repo-1",
        pr_number=pr_number,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:00:01Z",
        payload={"action": "created", "comment_id": comment_id},
    )
    duplicate = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:40a33fc1cfde79607c4e92705281798e",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key="github:poll:pull_request_review_comment:40a33fc1cfde79607c4e92705281798e",
        repo_slug=repo_slug,
        repo_id="repo-1",
        pr_number=pr_number,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:05:29Z",
        payload={"action": "created", "comment_id": comment_id},
    )
    rows = store.list_events(limit=10)

    assert created is not None
    assert duplicate is None
    assert len(rows) == 1
    assert rows[0].dedupe_key == dedupe_key


def test_webhook_events_have_null_dedupe_key(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)
    event_type = "pull_request_review_comment"

    created = store.record_event_if_new(
        event_id=f"github:delivery:{event_type}:created",
        provider="github",
        event_type=event_type,
        source="webhook",
        repo_slug="acme/widgets",
        pr_number=17,
        delivery_id="delivery-created",
        payload={"action": "created", "comment_id": "3328937952"},
    )
    edited = store.record_event_if_new(
        event_id=f"github:delivery:{event_type}:edited",
        provider="github",
        event_type=event_type,
        source="webhook",
        repo_slug="acme/widgets",
        pr_number=17,
        delivery_id="delivery-edited",
        payload={"action": "edited", "comment_id": "3328937952"},
    )
    rows = store.list_events(limit=10)

    assert created is not None
    assert edited is not None
    assert [event.dedupe_key for event in rows] == [None, None]


def test_bound_polling_event_inserts_after_unbound_row_for_same_comment(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)
    event_type = "pull_request_review_comment"
    repo_slug = "acme/widgets"
    pr_number = 17
    comment_id = "2844"
    base = f"{event_type}:{repo_slug}:{pr_number}:{comment_id}"

    unbound = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:unbound-hash",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key=f"{base}:unbound:binding-1",
        repo_slug=repo_slug,
        pr_number=pr_number,
        payload={"action": "created", "comment_id": comment_id},
    )
    bound = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:bound-hash",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key=f"{base}:thread:thread-123",
        repo_slug=repo_slug,
        pr_number=pr_number,
        payload={"action": "created", "comment_id": comment_id},
    )

    assert unbound is not None
    assert bound is not None
    assert unbound.event_id != bound.event_id
    assert len(store.list_events(limit=10)) == 2


def test_different_comment_ids_produce_different_dedupe_keys(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)
    event_type = "pull_request_review_comment"
    repo_slug = "acme/widgets"
    pr_number = 17

    first = store.record_event_if_new(
        event_id=f"github:poll:{event_type}:first",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key=f"github:poll:{event_type}:first",
        repo_slug=repo_slug,
        pr_number=pr_number,
        payload={"action": "created", "comment_id": "comment-1"},
    )
    second = store.record_event_if_new(
        event_id=f"github:poll:{event_type}:second",
        provider="github",
        event_type=event_type,
        source="polling",
        dedupe_key=f"github:poll:{event_type}:second",
        repo_slug=repo_slug,
        pr_number=pr_number,
        payload={"action": "created", "comment_id": "comment-2"},
    )

    assert first is not None
    assert second is not None
    assert first.dedupe_key == f"{event_type}:{repo_slug}:{pr_number}:comment-1:unbound"
    assert (
        second.dedupe_key == f"{event_type}:{repo_slug}:{pr_number}:comment-2:unbound"
    )
    assert first.dedupe_key != second.dedupe_key


def test_record_event_rejects_oversized_raw_payload(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    try:
        store.record_event(
            provider="github",
            event_type="pull_request",
            source="webhook",
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
        source="webhook",
        delivery_id="delivery-1",
        occurred_at="2026-03-25T01:30:00+01:00",
        received_at="2026-03-25T01:31:00+01:00",
        payload={"action": "opened"},
    )
    second = store.record_event(
        provider="github",
        event_type="pull_request",
        source="webhook",
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
