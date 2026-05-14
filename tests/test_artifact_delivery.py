from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.artifact_delivery import (
    ArtifactDeliveryService,
    ArtifactDeliveryStore,
    artifact_delivery_db_path,
)
from codex_autorunner.core.filebox import outbox_dir, outbox_pending_dir


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_delivery_lifecycle_records_receipt_and_archive_metadata(
    tmp_path: Path,
) -> None:
    service = ArtifactDeliveryService(tmp_path)
    source = _write(tmp_path / "report.txt", b"hello")

    intent = service.enqueue_file(
        source,
        target_surface="telegram",
        target_conversation_key="chat:123/thread:456",
        workspace_scope="repo:/example",
    )

    assert intent.state == "pending"
    assert intent.attempts == 0

    claimed = service.claim_next(target_surface="telegram")
    assert claimed is not None
    assert claimed.intent.delivery_id == intent.delivery_id
    assert claimed.artifact.filename == "report.txt"
    assert claimed.artifact.size == 5
    assert claimed.artifact.storage_path.exists()

    sending = service.mark_sending(
        intent.delivery_id,
        claim_token=claimed.claim_token,
    )
    assert sending.state == "sending"

    sent = service.mark_sent(
        intent.delivery_id,
        receipt={"message_id": 99},
        archived_path=tmp_path
        / ".codex-autorunner"
        / "filebox"
        / "outbox"
        / "sent"
        / "report.txt",
        claim_token=claimed.claim_token,
    )
    assert sent.state == "sent"
    assert sent.receipt_json == {"message_id": 99}
    assert sent.archived_path is not None
    assert sent.sent_at is not None

    pending = service.list_deliveries(states=("pending",))
    assert pending == []


def test_enqueue_is_idempotent_for_same_artifact_and_target(tmp_path: Path) -> None:
    service = ArtifactDeliveryService(tmp_path)
    source = _write(tmp_path / "same.txt", b"same contents")

    first = service.enqueue_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:1",
        workspace_scope="repo:/same",
    )
    second = service.enqueue_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:1",
        workspace_scope="repo:/same",
    )
    third = service.enqueue_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:2",
        workspace_scope="repo:/same",
    )

    assert second.delivery_id == first.delivery_id
    assert third.delivery_id != first.delivery_id
    assert len(service.list_deliveries()) == 2


def test_claim_next_can_filter_by_target_conversation(tmp_path: Path) -> None:
    service = ArtifactDeliveryService(tmp_path)
    source = _write(tmp_path / "same.txt", b"same contents")
    first = service.enqueue_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:1",
    )
    second = service.enqueue_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:2",
    )

    claimed = service.claim_next(
        target_surface="discord",
        target_conversation_key="channel:2",
    )

    assert claimed is not None
    assert claimed.intent.delivery_id == second.delivery_id
    assert service.inspect(first.delivery_id).state == "pending"


def test_failure_retry_and_cancel_metadata(tmp_path: Path) -> None:
    service = ArtifactDeliveryService(tmp_path)
    source = _write(tmp_path / "retry.txt", b"retry")
    intent = service.enqueue_file(
        source,
        target_surface="telegram",
        target_conversation_key="chat:123",
    )
    claimed = service.claim_next(target_surface="telegram")
    assert claimed is not None

    failed = service.mark_failed(
        intent.delivery_id,
        error="rate limited",
        next_attempt_at="2026-05-14T01:02:03Z",
        claim_token=claimed.claim_token,
    )
    assert failed.state == "failed"
    assert failed.attempts == 1
    assert failed.last_error == "rate limited"
    assert failed.next_attempt_at == "2026-05-14T01:02:03Z"
    assert failed.failed_at is not None

    retry = service.retry(intent.delivery_id)
    assert retry.state == "pending"
    assert retry.attempts == 1
    assert retry.last_error is None
    assert retry.next_attempt_at is None

    cancelled = service.cancel(intent.delivery_id)
    assert cancelled.state == "cancelled"
    assert cancelled.cancelled_at is not None


def test_legacy_outbox_import_preserves_files_and_records_origin(
    tmp_path: Path,
) -> None:
    direct = _write(outbox_dir(tmp_path) / "direct.txt", b"direct")
    pending = _write(outbox_pending_dir(tmp_path) / "pending.txt", b"pending")
    service = ArtifactDeliveryService(tmp_path)

    imported = service.import_legacy_outbox(
        target_surface="discord",
        target_conversation_key="channel:42",
        workspace_scope="repo:/legacy",
    )

    assert len(imported) == 2
    assert direct.read_bytes() == b"direct"
    assert pending.read_bytes() == b"pending"
    by_name = {
        service.store.get_artifact(intent.artifact_id).filename: intent
        for intent in imported
    }
    assert by_name["direct.txt"].metadata_json["legacy_filebox_source"] == "outbox"
    assert (
        by_name["pending.txt"].metadata_json["legacy_filebox_source"]
        == "outbox/pending"
    )


def test_store_can_reopen_existing_journal(tmp_path: Path) -> None:
    source = _write(tmp_path / "persist.txt", b"persist")
    service = ArtifactDeliveryService(tmp_path)
    intent = service.enqueue_file(
        source,
        target_surface="web",
        target_conversation_key="conversation:abc",
    )

    reopened = ArtifactDeliveryStore(artifact_delivery_db_path(tmp_path))

    assert reopened.inspect(intent.delivery_id) == intent
