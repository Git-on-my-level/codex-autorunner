"""Tests for FlowController lifecycle repo_id injection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.flows import (
    FlowController,
    FlowDefinition,
    FlowRunRecord,
    StepOutcome,
)
from codex_autorunner.core.lifecycle_events import (
    LifecycleEventStore,
    LifecycleEventType,
)
from codex_autorunner.manifest import MANIFEST_VERSION, Manifest, save_manifest

pytestmark = pytest.mark.integration


@pytest.fixture
def hub_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "repos" / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)

    manifest = Manifest(version=MANIFEST_VERSION, repos=[])
    repo = manifest.ensure_repo(hub_root, repo_root, repo_id="demo-repo")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)
    return hub_root, repo_root, repo.id


@pytest.fixture
def simple_definition() -> FlowDefinition:
    async def step(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        await asyncio.sleep(0)
        return StepOutcome.complete(output={"ok": True})

    definition = FlowDefinition(
        flow_type="test_flow",
        initial_step="step",
        steps={"step": step},
    )
    definition.validate()
    return definition


@pytest.mark.asyncio
async def test_flow_controller_emits_repo_id_from_manifest(
    hub_repo: tuple[Path, Path, str],
    simple_definition: FlowDefinition,
) -> None:
    hub_root, repo_root, repo_id = hub_repo
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"

    controller = FlowController(
        definition=simple_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
        hub_root=hub_root,
    )
    controller.initialize()
    record = await controller.start_flow(input_data={})
    await controller.run_flow(record.id)
    controller.shutdown()

    store = LifecycleEventStore(hub_root)
    events = store.load()
    completed = [e for e in events if e.event_type == LifecycleEventType.FLOW_COMPLETED]
    assert completed
    event = completed[-1]
    assert event.repo_id == repo_id
    assert event.data.get("repo_id") == repo_id


@pytest.mark.asyncio
async def test_flow_controller_keeps_empty_repo_id_without_hub(
    tmp_path: Path,
    simple_definition: FlowDefinition,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"

    controller = FlowController(
        definition=simple_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
        hub_root=None,
    )
    controller.initialize()

    captured: list[tuple[str, str, dict]] = []

    def _capture(event_type: str, repo_id: str, run_id: str, data: dict) -> None:
        captured.append((event_type, repo_id, data))

    controller.add_lifecycle_event_listener(_capture)

    record = await controller.start_flow(input_data={})
    await controller.run_flow(record.id)
    controller.shutdown()

    completed = [entry for entry in captured if entry[0] == "flow_completed"]
    assert completed
    _, repo_id, data = completed[-1]
    assert repo_id == ""
    assert "repo_id" not in data
