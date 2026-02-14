from pathlib import Path

from codex_autorunner.core.pma_context import _snapshot_pma_files


def _write(dir_path: Path, name: str, content: bytes = b"test") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_bytes(content)
    return path


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


def test_snapshot_pma_files_includes_legacy_pma(tmp_path: Path) -> None:
    _write(tmp_path / ".codex-autorunner" / "pma" / "inbox", "legacy.txt", b"legacy")

    pma_files, pma_files_detail = _snapshot_pma_files(tmp_path)

    assert pma_files["inbox"] == ["legacy.txt"]
    assert pma_files["outbox"] == []

    inbox_detail = pma_files_detail["inbox"]
    assert len(inbox_detail) == 1
    assert inbox_detail[0]["name"] == "legacy.txt"
    assert inbox_detail[0]["source"] == "pma"


def test_snapshot_pma_files_dedupes_preferring_filebox(tmp_path: Path) -> None:
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
