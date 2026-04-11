from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.orchestration.sqlite import (
    initialize_orchestration_sqlite,
    open_orchestration_sqlite,
)

runner = CliRunner()


def _seed_terminal_execution(hub_root: Path, execution_id: str) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orch_thread_targets (
                    thread_target_id,
                    agent_id,
                    backend_thread_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    workspace_root,
                    display_name,
                    lifecycle_status,
                    runtime_status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "thread-cli",
                    "codex",
                    "backend-thread-cli",
                    "repo-1",
                    "repo",
                    "repo-1",
                    str(hub_root / "workspace"),
                    "CLI",
                    "active",
                    "completed",
                    "2026-04-12T00:00:00Z",
                    "2026-04-12T00:05:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id,
                    thread_target_id,
                    client_request_id,
                    request_kind,
                    prompt_text,
                    status,
                    backend_turn_id,
                    assistant_text,
                    error_text,
                    model_id,
                    reasoning_level,
                    transcript_mirror_id,
                    started_at,
                    finished_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    "thread-cli",
                    "client-cli",
                    "message",
                    "Summarize",
                    "completed",
                    "backend-turn-cli",
                    "",
                    None,
                    "gpt-test",
                    "high",
                    None,
                    "2026-04-12T00:00:00Z",
                    "2026-04-12T00:05:00Z",
                    "2026-04-12T00:00:00Z",
                ),
            )
            for index in range(1, 21):
                event_type = "output_delta"
                payload = {
                    "event_index": index,
                    "event_family": "output_delta",
                    "event": {
                        "timestamp": f"2026-04-12T00:00:{index:02d}Z",
                        "delta_type": "assistant_message",
                        "content": f"chunk-{index}",
                    },
                }
                if index == 1:
                    event_type = "turn_started"
                    payload = {
                        "event_index": index,
                        "event_family": "run_notice",
                        "event": {
                            "timestamp": "2026-04-12T00:00:00Z",
                            "kind": "info",
                            "message": "started",
                        },
                    }
                if index == 20:
                    event_type = "turn_completed"
                    payload = {
                        "event_index": index,
                        "event_family": "terminal",
                        "event": {
                            "timestamp": "2026-04-12T00:05:00Z",
                            "final_message": "done",
                        },
                    }
                conn.execute(
                    """
                    INSERT INTO orch_event_projections (
                        event_id,
                        event_family,
                        event_type,
                        target_kind,
                        target_id,
                        execution_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        run_id,
                        timestamp,
                        status,
                        payload_json,
                        processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"turn-timeline:{execution_id}:{index:04d}",
                        "turn.timeline",
                        event_type,
                        "thread_target",
                        "thread-cli",
                        execution_id,
                        "repo-1",
                        "repo",
                        "repo-1",
                        None,
                        payload["event"]["timestamp"],
                        "recorded",
                        json.dumps(payload),
                        1,
                    ),
                )


def test_hub_orchestration_audit_and_compaction_commands(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    _seed_terminal_execution(hub_root, "exec-cli")

    audit = runner.invoke(
        app,
        ["hub", "orchestration", "audit", "--path", str(hub_root), "--json"],
    )

    assert audit.exit_code == 0
    audit_payload = json.loads(audit.output)
    assert audit_payload["total_executions"] == 1
    assert audit_payload["timeline_rows"] == 20
    assert audit_payload["missing_manifest_execution_ids"] == ["exec-cli"]

    compact = runner.invoke(
        app,
        [
            "hub",
            "orchestration",
            "compact-history",
            "--path",
            str(hub_root),
            "--dry-run",
            "--json",
        ],
    )

    assert compact.exit_code == 0
    compact_payload = json.loads(compact.output)
    assert compact_payload["compacted_executions"] == 1
    assert compact_payload["rows_before"] == 20
    assert compact_payload["rows_after"] <= 16


def test_hub_orchestration_canary_command(tmp_path: Path) -> None:
    hub_root = tmp_path / "canary"

    result = runner.invoke(
        app,
        [
            "hub",
            "orchestration",
            "canary",
            "--path",
            str(hub_root),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["migration"]["candidate_executions"] >= 1
    assert payload["compaction"]["compacted_executions"] >= 1
    assert payload["startup"]["status_code"] == 200
    assert payload["recovery"]["pma_checkpoint_restore"]["backend_turn_id"]
    assert payload["recovery"]["discord_bound_checkpoint_restore"]["backend_turn_id"]
    assert "tool_call" in payload["trace_validation"]["families"]
    shutil.rmtree(hub_root, ignore_errors=True)
