from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...core.artifact_delivery import (
    ArtifactDeliveryService,
    ArtifactRecord,
    DeliveryIntent,
    delivery_filename,
)
from ...core.filebox import outbox_sent_dir
from ...core.logging_utils import log_event
from ...core.time_utils import now_iso


class ChatArtifactTransport(Protocol):
    async def send_artifact(
        self,
        *,
        artifact: ArtifactRecord,
        intent: DeliveryIntent,
    ) -> dict[str, Any]:
        """Send one artifact to one concrete chat target."""


@dataclass(frozen=True)
class LegacyArchivePolicy:
    mode: str = "move-to-sent"
    sent_dir: Path | None = None


def delivery_target_key(
    *,
    surface: str,
    conversation_key: str,
) -> str:
    return f"{surface}:{conversation_key}"


async def import_and_drain_legacy_outbox(
    *,
    service: ArtifactDeliveryService,
    transport: ChatArtifactTransport,
    target_surface: str,
    target_conversation_key: str,
    workspace_scope: str | None,
    archive_policy: LegacyArchivePolicy,
    logger: logging.Logger,
) -> None:
    service.import_legacy_outbox(
        target_surface=target_surface,
        target_conversation_key=target_conversation_key,
        workspace_scope=workspace_scope,
    )
    await drain_artifact_deliveries(
        service=service,
        transport=transport,
        target_surface=target_surface,
        target_conversation_key=target_conversation_key,
        archive_policy=archive_policy,
        logger=logger,
    )


def enqueue_legacy_paths(
    *,
    service: ArtifactDeliveryService,
    paths: list[Path],
    target_surface: str,
    target_conversation_key: str,
    workspace_scope: str | None,
    legacy_source: str,
) -> list[DeliveryIntent]:
    intents: list[DeliveryIntent] = []
    for path in paths:
        intents.append(
            service.enqueue_file(
                path,
                target_surface=target_surface,
                target_conversation_key=target_conversation_key,
                workspace_scope=workspace_scope,
                metadata={
                    "legacy_filebox_source": legacy_source,
                    "legacy_path": str(path),
                },
            )
        )
    return intents


async def drain_artifact_deliveries(
    *,
    service: ArtifactDeliveryService,
    transport: ChatArtifactTransport,
    target_surface: str,
    target_conversation_key: str,
    archive_policy: LegacyArchivePolicy,
    logger: logging.Logger,
) -> None:
    _retry_due_failures(
        service=service,
        target_surface=target_surface,
        target_conversation_key=target_conversation_key,
    )
    while True:
        claimed = service.claim_next(
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
        )
        if claimed is None:
            return
        intent = claimed.intent
        service.mark_sending(intent.delivery_id, claim_token=claimed.claim_token)
        try:
            receipt = await transport.send_artifact(
                artifact=claimed.artifact,
                intent=intent,
            )
        except Exception as exc:
            service.mark_failed(
                intent.delivery_id,
                error=str(exc) or exc.__class__.__name__,
                claim_token=claimed.claim_token,
            )
            log_event(
                logger,
                logging.WARNING,
                "chat.artifact_delivery.send_failed",
                target_surface=target_surface,
                target_conversation_key=target_conversation_key,
                delivery_id=intent.delivery_id,
                artifact_id=intent.artifact_id,
                exc=exc,
            )
            continue
        archived_path = _archive_legacy_source(
            artifact=claimed.artifact,
            intent=intent,
            archive_policy=archive_policy,
            logger=logger,
        )
        service.mark_sent(
            intent.delivery_id,
            receipt=receipt,
            archived_path=archived_path,
            claim_token=claimed.claim_token,
        )


def _archive_legacy_source(
    *,
    artifact: ArtifactRecord,
    intent: DeliveryIntent,
    archive_policy: LegacyArchivePolicy,
    logger: logging.Logger,
) -> Path | None:
    legacy_path_value = intent.metadata_json.get("legacy_path")
    if not isinstance(legacy_path_value, str) or not legacy_path_value:
        return None
    source = Path(legacy_path_value)
    if archive_policy.mode == "delete":
        try:
            source.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "chat.artifact_delivery.legacy_delete_failed",
                delivery_id=intent.delivery_id,
                path=str(source),
                exc=exc,
            )
        return None
    sent_dir = archive_policy.sent_dir or outbox_sent_dir(Path(artifact.origin_root))
    destination = _sent_destination(
        sent_dir=sent_dir,
        filename=delivery_filename(intent, artifact),
    )
    sent_dir.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
        return destination
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            "chat.artifact_delivery.legacy_move_failed",
            delivery_id=intent.delivery_id,
            path=str(source),
            destination=str(destination),
            exc=exc,
        )
    try:
        destination.write_bytes(Path(artifact.storage_path).read_bytes())
        try:
            source.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "chat.artifact_delivery.legacy_cleanup_failed",
                delivery_id=intent.delivery_id,
                path=str(source),
                destination=str(destination),
                exc=exc,
            )
        return destination
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            "chat.artifact_delivery.legacy_archive_failed",
            delivery_id=intent.delivery_id,
            path=str(source),
            destination=str(destination),
            exc=exc,
        )
    return None


def _retry_due_failures(
    *,
    service: ArtifactDeliveryService,
    target_surface: str,
    target_conversation_key: str,
) -> None:
    now = now_iso()
    for intent in service.store.list_deliveries(
        states=("failed",),
        target_surface=target_surface,
    ):
        if intent.target_conversation_key != target_conversation_key:
            continue
        if intent.next_attempt_at is not None and intent.next_attempt_at > now:
            continue
        service.retry(intent.delivery_id)


def _sent_destination(*, sent_dir: Path, filename: str) -> Path:
    destination = sent_dir / filename
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        destination = sent_dir / f"{stem}-{secrets.token_hex(3)}{suffix}"
    return destination
