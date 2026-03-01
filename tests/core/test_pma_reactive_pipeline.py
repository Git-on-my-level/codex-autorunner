from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.core.pma_lane_worker import PmaLaneWorker
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.integrations.pma_delivery import deliver_pma_output_to_active_sink
from codex_autorunner.integrations.telegram.state import TelegramStateStore
from codex_autorunner.manifest import load_manifest, save_manifest


def _write_hub_config(hub_root: Path) -> None:
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["reactive_debounce_seconds"] = 0
    config_path = hub_root / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(cfg, indent=2) + "\n",
        encoding="utf-8",
    )


def _register_repo(hub_root: Path, repo_root: Path, repo_id: str) -> None:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(manifest_path, manifest, hub_root)


async def _process_one_item(
    hub_root: Path,
    *,
    queue: PmaQueue,
    telegram_state_path: Path,
    assistant_text: str = "ok",
) -> None:
    processed = asyncio.Event()

    async def executor(item) -> dict:
        lifecycle_event = item.payload.get("lifecycle_event") or {}
        turn_id = f"turn-{item.item_id}"
        store = PmaTranscriptStore(hub_root)
        store.write_transcript(
            turn_id=turn_id,
            metadata={
                "trigger": "lifecycle_event",
                "event_id": lifecycle_event.get("event_id"),
                "event_type": lifecycle_event.get("event_type"),
            },
            assistant_text=assistant_text,
        )
        await deliver_pma_output_to_active_sink(
            hub_root=hub_root,
            assistant_text=assistant_text,
            turn_id=turn_id,
            lifecycle_event=lifecycle_event,
            telegram_state_path=telegram_state_path,
        )
        processed.set()
        return {"status": "ok", "turn_id": turn_id, "message": assistant_text}

    worker = PmaLaneWorker("pma:default", queue, executor)
    await worker.start()
    await asyncio.wait_for(processed.wait(), timeout=2.0)
    await worker.stop()


@pytest.mark.anyio
async def test_reactive_flow_failed_writes_transcript_web_sink(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed(
            "repo-1", "run-1", origin="runner"
        )
        await asyncio.to_thread(supervisor.process_lifecycle_events)

        queue = PmaQueue(hub_root)
        await _process_one_item(
            hub_root,
            queue=queue,
            telegram_state_path=hub_root / "telegram_state.sqlite3",
        )

        items = await queue.list_items("pma:default")
        assert items
        assert items[0].state in (QueueItemState.COMPLETED, QueueItemState.FAILED)

        transcript_store = PmaTranscriptStore(hub_root)
        transcripts = transcript_store.list_recent(limit=1)
        assert transcripts, "expected a transcript entry"

        telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
        outbox = await telegram_store.list_outbox()
        await telegram_store.close()
        assert outbox == []
    finally:
        supervisor.shutdown()


@pytest.mark.anyio
async def test_reactive_dispatch_created_does_not_enqueue_telegram_outbox(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        PmaDeliveryTargetsStore(hub_root).set_targets(
            [
                {
                    "kind": "chat",
                    "platform": "telegram",
                    "chat_id": "1",
                    "thread_id": "2",
                }
            ]
        )
        repo_root = hub_root / "repo-1"
        repo_root.mkdir(parents=True, exist_ok=True)
        _register_repo(hub_root, repo_root, "repo-1")
        supervisor.lifecycle_emitter.emit_dispatch_created(
            "repo-1", "run-1", origin="runner"
        )
        await asyncio.to_thread(supervisor.process_lifecycle_events)

        queue = PmaQueue(hub_root)
        await _process_one_item(
            hub_root,
            queue=queue,
            telegram_state_path=hub_root / "telegram_state.sqlite3",
        )

        telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
        outbox = await telegram_store.list_outbox()
        await telegram_store.close()
        assert outbox == []
    finally:
        supervisor.shutdown()
