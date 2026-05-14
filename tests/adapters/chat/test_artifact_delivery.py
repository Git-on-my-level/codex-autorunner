from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.adapters.chat.artifact_delivery import (
    LegacyArchivePolicy,
    drain_artifact_deliveries,
    enqueue_legacy_paths,
)
from codex_autorunner.core.artifact_delivery import (
    ArtifactDeliveryService,
    ArtifactRecord,
    DeliveryIntent,
)


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, bytes]] = []

    async def send_artifact(
        self,
        *,
        artifact: ArtifactRecord,
        intent: DeliveryIntent,
    ) -> dict[str, Any]:
        _ = intent
        if self.fail:
            raise RuntimeError("network timeout")
        data = Path(artifact.storage_path).read_bytes()
        self.sent.append((artifact.filename, data))
        return {"message_id": "m1"}


@pytest.mark.anyio
async def test_drain_marks_sent_after_transport_success_and_archives_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "outbox" / "report.txt"
    source.parent.mkdir(parents=True)
    source.write_text("hello", encoding="utf-8")
    sent_dir = tmp_path / "sent"
    service = ArtifactDeliveryService(tmp_path)
    enqueue_legacy_paths(
        service=service,
        paths=[source],
        target_surface="discord",
        target_conversation_key="channel:1",
        workspace_scope="repo:/example",
        legacy_source="outbox/pending",
    )
    transport = _Transport()

    await drain_artifact_deliveries(
        service=service,
        transport=transport,
        target_surface="discord",
        target_conversation_key="channel:1",
        archive_policy=LegacyArchivePolicy(sent_dir=sent_dir),
        logger=__import__("logging").getLogger("test"),
    )

    deliveries = service.list_deliveries()
    assert deliveries[0].state == "sent"
    assert deliveries[0].receipt_json == {"message_id": "m1"}
    assert transport.sent == [("report.txt", b"hello")]
    assert not source.exists()
    assert (sent_dir / "report.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.anyio
async def test_drain_failure_keeps_delivery_retryable_with_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "outbox" / "report.txt"
    source.parent.mkdir(parents=True)
    source.write_text("hello", encoding="utf-8")
    service = ArtifactDeliveryService(tmp_path)
    enqueue_legacy_paths(
        service=service,
        paths=[source],
        target_surface="telegram",
        target_conversation_key="chat:1",
        workspace_scope=None,
        legacy_source="outbox/pending",
    )

    await drain_artifact_deliveries(
        service=service,
        transport=_Transport(fail=True),
        target_surface="telegram",
        target_conversation_key="chat:1",
        archive_policy=LegacyArchivePolicy(mode="delete"),
        logger=__import__("logging").getLogger("test"),
    )

    deliveries = service.list_deliveries()
    assert deliveries[0].state == "failed"
    assert deliveries[0].attempts == 1
    assert deliveries[0].last_error == "network timeout"
    assert source.exists()

    retry_transport = _Transport()
    await drain_artifact_deliveries(
        service=service,
        transport=retry_transport,
        target_surface="telegram",
        target_conversation_key="chat:1",
        archive_policy=LegacyArchivePolicy(mode="delete"),
        logger=__import__("logging").getLogger("test"),
    )

    deliveries = service.list_deliveries()
    assert deliveries[0].state == "sent"
    assert retry_transport.sent == [("report.txt", b"hello")]
    assert not source.exists()
