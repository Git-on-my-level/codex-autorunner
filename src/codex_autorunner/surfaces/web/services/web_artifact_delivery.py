"""Web-surface artifact delivery drain.

The web surface is itself the delivery destination: a delivered file is reachable
through the existing artifact-delivery download route and rendered inline in the
chat transcript. So the web transport performs no outbound push; it simply
acknowledges the intent so the shared drain marks it ``sent``.

This drains only journal intents already targeted at this web conversation
(``car artifacts send --to explicit --surface web --conversation
managed_thread:{id}``, as instructed by the per-turn capsule). It deliberately
does NOT import the repo-global legacy outbox: that directory is shared by all
managed threads in a workspace, so importing it per turn would mis-attribute one
thread's files to another concurrent thread.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ....adapters.chat.artifact_delivery import (
    LegacyArchivePolicy,
    drain_artifact_deliveries,
)
from ....core.artifact_delivery import (
    ArtifactDeliveryService,
    ArtifactRecord,
    DeliveryIntent,
)
from ....core.filebox import outbox_sent_dir
from ....core.pma.message_options import (
    WEB_ARTIFACT_SURFACE,
    web_artifact_conversation_key,
)


class _WebArtifactTransport:
    """No-op transport: the blob is already downloadable via the web API."""

    async def send_artifact(
        self,
        *,
        artifact: ArtifactRecord,
        intent: DeliveryIntent,
    ) -> dict[str, Any]:
        return {
            "surface": WEB_ARTIFACT_SURFACE,
            "delivery_id": intent.delivery_id,
            "artifact_id": artifact.artifact_id,
        }


async def drain_web_artifact_deliveries(
    *,
    workspace_root: Path,
    managed_thread_id: str,
    logger: logging.Logger,
) -> None:
    """Drain pending web deliveries targeted at this managed thread.

    Per-turn send failures are logged by the shared drain and never raised, so a
    delivery hiccup cannot break turn finalization.
    """

    conversation_key = web_artifact_conversation_key(managed_thread_id)
    service = ArtifactDeliveryService(workspace_root)
    await drain_artifact_deliveries(
        service=service,
        transport=_WebArtifactTransport(),
        target_surface=WEB_ARTIFACT_SURFACE,
        target_conversation_key=conversation_key,
        archive_policy=LegacyArchivePolicy(
            mode="move-to-sent",
            sent_dir=outbox_sent_dir(workspace_root),
        ),
        logger=logger,
    )


__all__ = ["drain_web_artifact_deliveries"]
