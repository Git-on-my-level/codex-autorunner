from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.chat.run_mirror import ChatRunMirror


def _init_flow_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={})
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_chat_run_mirror_writes_jsonl_and_registers_artifacts(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    _init_flow_run(tmp_path, run_id)
    mirror = ChatRunMirror(tmp_path)

    mirror.mirror_inbound(
        run_id=run_id,
        platform="telegram",
        event_type="flow_resume_command",
        text="/flow resume",
        chat_id=-1001,
        thread_id=12,
    )
    mirror.mirror_outbound(
        run_id=run_id,
        platform="telegram",
        event_type="flow_resume_notice",
        text=f"Resumed run `{run_id}`.",
        chat_id=-1001,
        thread_id=12,
    )

    chat_dir = tmp_path / ".codex-autorunner" / "flows" / run_id / "chat"
    inbound_records = _read_jsonl(chat_dir / "inbound.jsonl")
    outbound_records = _read_jsonl(chat_dir / "outbound.jsonl")
    assert len(inbound_records) == 1
    assert len(outbound_records) == 1
    assert inbound_records[0]["run_id"] == run_id
    assert inbound_records[0]["direction"] == "inbound"
    assert inbound_records[0]["event_type"] == "flow_resume_command"
    assert outbound_records[0]["run_id"] == run_id
    assert outbound_records[0]["direction"] == "outbound"
    assert outbound_records[0]["event_type"] == "flow_resume_notice"

    with FlowStore(tmp_path / ".codex-autorunner" / "flows.db") as store:
        artifacts = store.get_artifacts(run_id)
    kinds = {artifact.kind for artifact in artifacts}
    assert "chat_inbound" in kinds
    assert "chat_outbound" in kinds


def test_chat_run_mirror_logs_and_continues_when_artifact_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = str(uuid.uuid4())
    mirror = ChatRunMirror(tmp_path)

    def _boom(self) -> None:  # noqa: ANN001
        raise RuntimeError("flow store unavailable")

    monkeypatch.setattr(FlowStore, "initialize", _boom)
    caplog.set_level(logging.WARNING)

    mirror.mirror_inbound(
        run_id=run_id,
        platform="telegram",
        event_type="flow_resume_command",
        text="/flow resume",
        chat_id=-1001,
        thread_id=12,
    )

    inbound_path = (
        tmp_path / ".codex-autorunner" / "flows" / run_id / "chat" / "inbound.jsonl"
    )
    assert inbound_path.exists()
    records = _read_jsonl(inbound_path)
    assert len(records) == 1
    assert any(
        "chat.run_mirror.artifact_failed" in rec.message for rec in caplog.records
    )
