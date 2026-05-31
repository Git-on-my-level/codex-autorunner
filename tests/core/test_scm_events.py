from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.scm_events import ScmEventStore


def _scm_event_rows(tmp_path: Path) -> list[dict[str, object]]:
    with open_orchestration_sqlite(tmp_path) as conn:
        rows = conn.execute(
            """
            SELECT event_id, provider, event_type, repo_slug, pr_number,
                   source, comment_id, dedupe_key
              FROM orch_scm_events
             ORDER BY event_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


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


def test_record_event_if_new_returns_none_for_existing_event_id(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    created = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:comment-1",
        provider="github",
        event_type="pull_request_review_comment",
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


def test_record_event_if_new_dedupes_polling_rotating_ids_by_comment_identity(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)

    first = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:7f81b70789930f207325f5096a1eae3e",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="polling",
        comment_id="3330969470",
        occurred_at="2026-05-31T21:40:25Z",
        received_at="2026-05-31T21:40:25Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )
    duplicate = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:40a33fc1cfde79607c4e92705281798e",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="polling",
        comment_id="3330969470",
        occurred_at="2026-05-31T21:46:53Z",
        received_at="2026-05-31T21:46:53Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )

    expected_dedupe_key = (
        "pull_request_review_comment:Git-on-my-level/codex-autorunner:1988:3330969470"
    )
    rows = _scm_event_rows(tmp_path)

    assert first is not None
    assert duplicate is None
    assert len(rows) == 1
    assert first.dedupe_key == expected_dedupe_key
    assert rows[0]["dedupe_key"] == expected_dedupe_key
    assert rows[0]["dedupe_key"] not in {rows[0]["event_id"], first.event_id}
    assert "7f81b70789930f207325f5096a1eae3e" not in str(rows[0]["dedupe_key"])
    assert "40a33fc1cfde79607c4e92705281798e" not in str(rows[0]["dedupe_key"])


def test_record_event_if_new_allows_distinct_polling_comment_ids(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)

    first = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:event-1",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="polling",
        comment_id="3330969470",
        occurred_at="2026-05-31T21:40:25Z",
        received_at="2026-05-31T21:40:25Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )
    second = store.record_event_if_new(
        event_id="github:poll:pull_request_review_comment:event-2",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="polling",
        comment_id="3330969471",
        occurred_at="2026-05-31T21:46:53Z",
        received_at="2026-05-31T21:46:53Z",
        payload={"action": "created", "comment_id": "3330969471"},
    )

    rows = _scm_event_rows(tmp_path)

    assert first is not None
    assert second is not None
    assert len(rows) == 2
    assert {row["dedupe_key"] for row in rows} == {
        "pull_request_review_comment:Git-on-my-level/codex-autorunner:1988:3330969470",
        "pull_request_review_comment:Git-on-my-level/codex-autorunner:1988:3330969471",
    }


def test_record_event_if_new_does_not_dedupe_webhook_comment_events(
    tmp_path: Path,
) -> None:
    store = ScmEventStore(tmp_path)

    webhook = store.record_event_if_new(
        event_id="delivery-1",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="webhook",
        comment_id="3330969470",
        occurred_at="2026-05-31T21:40:25Z",
        received_at="2026-05-31T21:40:25Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )
    another_webhook = store.record_event_if_new(
        event_id="delivery-2",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        source="webhook",
        comment_id="3330969470",
        occurred_at="2026-05-31T21:46:53Z",
        received_at="2026-05-31T21:46:53Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )
    no_source = store.record_event_if_new(
        event_id="delivery-3",
        provider="github",
        event_type="pull_request_review_comment",
        repo_slug="Git-on-my-level/codex-autorunner",
        repo_id="repo-1",
        pr_number=1988,
        comment_id="3330969470",
        occurred_at="2026-05-31T21:50:00Z",
        received_at="2026-05-31T21:50:00Z",
        payload={"action": "created", "comment_id": "3330969470"},
    )

    rows = _scm_event_rows(tmp_path)

    assert webhook is not None
    assert another_webhook is not None
    assert no_source is not None
    assert len(rows) == 3
    assert {row["dedupe_key"] for row in rows} == {None}


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
