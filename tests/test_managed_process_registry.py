from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.managed_processes import registry


def _record(**overrides) -> registry.ProcessRecord:
    payload = {
        "kind": "opencode",
        "workspace_id": "ws-001",
        "pid": 12345,
        "pgid": 12345,
        "base_url": "http://127.0.0.1:8080",
        "command": ["opencode", "serve", "--hostname", "127.0.0.1"],
        "owner_pid": 999,
        "started_at": "2026-02-15T12:00:00Z",
        "metadata": {"run_id": "r-1"},
    }
    payload.update(overrides)
    return registry.ProcessRecord(**payload)


def test_write_process_record_is_atomic_and_uses_expected_path(
    monkeypatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    real_atomic_write = registry.atomic_write

    def _capture_atomic_write(path: Path, content: str, durable: bool = False) -> None:
        seen["path"] = path
        seen["durable"] = durable
        seen["content"] = content
        real_atomic_write(path, content, durable)

    monkeypatch.setattr(registry, "atomic_write", _capture_atomic_write)
    rec = _record()

    written_path = registry.write_process_record(tmp_path, rec, durable=True)

    expected_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "ws-001.json"
    )
    assert written_path == expected_path
    assert seen["path"] == expected_path
    assert seen["durable"] is True
    payload = json.loads(expected_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "opencode"
    assert payload["handle_id"] is None
    assert payload["workspace_id"] == "ws-001"
    assert payload["pid"] == 12345


def test_process_record_handle_id_is_preferred_for_new_records(
    tmp_path: Path,
) -> None:
    rec = _record(handle_id="__global__", workspace_id="legacy-workspace-id")

    written_path = registry.write_process_record(tmp_path, rec)
    loaded = registry.read_process_record(tmp_path, "opencode", "__global__")

    assert written_path.name == "__global__.json"
    assert loaded is not None
    assert loaded.handle_id == "__global__"
    assert loaded.workspace_id == "legacy-workspace-id"
    assert loaded.record_key() == "__global__"


def test_legacy_process_record_without_handle_id_stays_reapable(
    tmp_path: Path,
) -> None:
    legacy_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "ws-legacy.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(_record(workspace_id="ws-legacy").to_dict() | {"handle_id": None}),
        encoding="utf-8",
    )

    loaded = registry.read_process_record(tmp_path, "opencode", "ws-legacy")

    assert loaded is not None
    assert loaded.handle_id is None
    assert loaded.record_key() == "ws-legacy"


def test_read_list_and_delete_round_trip(tmp_path: Path) -> None:
    rec_one = _record(workspace_id="ws-a", pid=2001, metadata={"token": "a"})
    rec_two = _record(
        kind="codex_app_server",
        workspace_id=None,
        pid=3001,
        pgid=3001,
        command=["python", "-m", "codex_autorunner", "serve"],
        metadata={"token": "b"},
    )

    registry.write_process_record(tmp_path, rec_one)
    registry.write_process_record(tmp_path, rec_two)

    loaded_one = registry.read_process_record(tmp_path, "opencode", "ws-a")
    assert loaded_one is not None
    assert loaded_one.workspace_id == "ws-a"
    assert loaded_one.metadata["token"] == "a"

    loaded_two = registry.read_process_record(tmp_path, "codex_app_server", "3001")
    assert loaded_two is not None
    assert loaded_two.kind == "codex_app_server"
    assert loaded_two.pid == 3001

    all_records = registry.list_process_records(tmp_path)
    assert len(all_records) == 2
    opencode_records = registry.list_process_records(tmp_path, kind="opencode")
    assert len(opencode_records) == 1
    assert opencode_records[0].workspace_id == "ws-a"

    assert registry.delete_process_record(tmp_path, "opencode", "ws-a") is True
    assert registry.delete_process_record(tmp_path, "opencode", "ws-a") is False
    assert registry.read_process_record(tmp_path, "opencode", "ws-a") is None


def test_registry_summary_classifies_corrupt_stale_and_orphaned_records(
    tmp_path: Path,
) -> None:
    registry.write_process_record(
        tmp_path, _record(workspace_id="ws-live", owner_pid=1)
    )
    registry.write_process_record(
        tmp_path,
        _record(workspace_id="ws-stale", owner_pid=2),
    )
    orphan_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "wrong-key.json"
    )
    orphan_path.write_text(
        json.dumps(_record(workspace_id="actual-key", owner_pid=1).to_dict()),
        encoding="utf-8",
    )
    corrupt_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "broken.json"
    )
    corrupt_path.write_text("{not-json", encoding="utf-8")

    summary = registry.summarize_process_registry(
        tmp_path,
        owner_is_alive=lambda pid: pid == 1,
    )

    assert summary.counts["live"] == 1
    assert summary.counts["stale"] == 1
    assert summary.counts["orphaned"] == 1
    assert summary.counts["corrupt"] == 1
    corrupt = [entry for entry in summary.entries if entry.status.value == "corrupt"]
    assert corrupt[0].path == corrupt_path


def test_schema_validation_and_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="handle_id, workspace_id, or pid"):
        registry.write_process_record(
            tmp_path, _record(workspace_id=None, pid=None, pgid=None)
        )

    with pytest.raises(ValueError, match="kind contains invalid path characters"):
        registry.write_process_record(tmp_path, _record(kind="bad/kind"))

    invalid_json_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "broken.json"
    )
    invalid_json_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_json_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid process record JSON"):
        registry.read_process_record(tmp_path, "opencode", "broken")

    wrong_schema_path = (
        tmp_path / ".codex-autorunner" / "processes" / "opencode" / "wrong-schema.json"
    )
    wrong_schema_path.write_text(
        json.dumps(
            {
                "kind": "opencode",
                "workspace_id": "ws-b",
                "pid": 1,
                "pgid": 1,
                "base_url": None,
                "command": "not-a-list",
                "owner_pid": 42,
                "started_at": "2026-02-15T12:00:00Z",
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="command must be a non-empty list"):
        registry.read_process_record(tmp_path, "opencode", "wrong-schema")
