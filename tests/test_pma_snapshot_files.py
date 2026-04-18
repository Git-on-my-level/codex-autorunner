from pathlib import Path

from codex_autorunner.core.pma_context import _snapshot_pma_files
from codex_autorunner.core.pma_context_shared import THREAD_SNAPSHOT_FIELDS


def _write(dir_path: Path, name: str, content: bytes = b"test") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_bytes(content)
    return path


def test_thread_snapshot_fields_includes_core_projection_keys() -> None:
    required = {
        "managed_thread_id",
        "status",
        "lifecycle_status",
        "operator_status",
        "freshness",
        "repo_id",
        "agent",
        "chat_bound",
        "cleanup_protected",
    }
    actual = set(THREAD_SNAPSHOT_FIELDS)
    missing = required - actual
    assert not missing, f"THREAD_SNAPSHOT_FIELDS missing required keys: {missing}"


def test_thread_snapshot_fields_no_duplicates() -> None:
    assert len(THREAD_SNAPSHOT_FIELDS) == len(set(THREAD_SNAPSHOT_FIELDS))


def test_snapshot_pma_files_empty(tmp_path: Path) -> None:
    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)
    assert pma_files == {"inbox": [], "outbox": []}
    assert pma_files_detail == {"inbox": [], "outbox": []}


def test_snapshot_pma_files_from_filebox(tmp_path: Path) -> None:
    _write(tmp_path / ".codex-autorunner" / "filebox" / "inbox", "upload.md", b"data")
    _write(tmp_path / ".codex-autorunner" / "filebox" / "outbox", "report.txt", b"out")

    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)

    assert pma_files["inbox"] == ["upload.md"]
    assert pma_files["outbox"] == ["report.txt"]

    inbox_detail = pma_files_detail["inbox"]
    assert len(inbox_detail) == 1
    assert inbox_detail[0]["name"] == "upload.md"
    assert inbox_detail[0]["box"] == "inbox"
    assert inbox_detail[0]["source"] == "filebox"
    assert inbox_detail[0]["item_type"] == "pma_file"
    assert inbox_detail[0]["next_action"] == "process_uploaded_file"
    assert inbox_detail[0]["size"] == "4"
    assert inbox_detail[0]["modified_at"] != ""

    outbox_detail = pma_files_detail["outbox"]
    assert len(outbox_detail) == 1
    assert outbox_detail[0]["name"] == "report.txt"
    assert outbox_detail[0]["source"] == "filebox"


def test_snapshot_pma_files_ignores_legacy_pma(tmp_path: Path) -> None:
    _write(tmp_path / ".codex-autorunner" / "pma" / "inbox", "legacy.txt", b"legacy")

    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)

    assert pma_files["inbox"] == []
    assert pma_files["outbox"] == []


def test_snapshot_pma_files_ignores_legacy_duplicates(tmp_path: Path) -> None:
    _write(tmp_path / ".codex-autorunner" / "pma" / "inbox", "dup.txt", b"legacy")
    _write(tmp_path / ".codex-autorunner" / "filebox" / "inbox", "dup.txt", b"primary")

    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)

    assert pma_files["inbox"] == ["dup.txt"]
    inbox_detail = pma_files_detail["inbox"]
    assert len(inbox_detail) == 1
    assert inbox_detail[0]["source"] == "filebox"
    assert inbox_detail[0]["size"] == "7"


def test_snapshot_pma_files_includes_size_and_modified_at(tmp_path: Path) -> None:
    _write(
        tmp_path / ".codex-autorunner" / "filebox" / "inbox",
        "sized.txt",
        b"1234567890",
    )

    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)

    inbox_detail = pma_files_detail["inbox"]
    assert len(inbox_detail) == 1
    assert inbox_detail[0]["size"] == "10"
    assert inbox_detail[0]["modified_at"] != ""
    assert "T" in inbox_detail[0]["modified_at"]


def test_snapshot_pma_threads_includes_operator_status(hub_env) -> None:
    from codex_autorunner.core.pma_context import _snapshot_pma_threads
    from codex_autorunner.core.pma_thread_store import PmaThreadStore

    store = PmaThreadStore(hub_env.hub_root)
    store.create_thread(
        "codex",
        workspace_root=hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="operator-status-test",
    )

    threads = _snapshot_pma_threads(hub_env.hub_root)

    assert len(threads) >= 1
    thread = threads[0]
    assert "operator_status" in thread
    assert thread["operator_status"] == "idle"


def test_snapshot_pma_threads_includes_operator_status_for_failed(
    hub_env,
) -> None:
    from codex_autorunner.core.pma_context import _snapshot_pma_threads
    from codex_autorunner.core.pma_thread_store import PmaThreadStore

    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread(
        "codex",
        workspace_root=hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="failed-operator-status-test",
    )
    thread_id = thread["managed_thread_id"]
    turn = store.create_turn(thread_id, prompt="fail test")
    store.mark_turn_finished(
        turn["managed_turn_id"], status="error", assistant_text="fail"
    )

    threads = _snapshot_pma_threads(hub_env.hub_root)

    failed = next(t for t in threads if t["managed_thread_id"] == thread_id)
    assert failed["operator_status"] == "attention_required"
