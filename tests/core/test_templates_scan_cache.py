from pathlib import Path

from codex_autorunner.core.templates.scan_cache import (
    TemplateScanRecord,
    get_scan_record,
    scan_lock,
    scan_lock_path,
    scan_record_path,
    write_scan_record,
)


def _record(blob_sha: str) -> TemplateScanRecord:
    return TemplateScanRecord(
        blob_sha=blob_sha,
        repo_id="blessed",
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="deadbeef",
        trusted=False,
        decision="approve",
        severity="low",
        reason="safe",
        evidence=["snippet"],
        scanned_at="2026-01-31T00:00:00Z",
        scanner={"agent": "scan-agent", "model": "gpt-test"},
    )


def test_scan_cache_read_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    record = _record("abc123")

    assert get_scan_record(hub_root, record.blob_sha) is None

    write_scan_record(record, hub_root)

    loaded = get_scan_record(hub_root, record.blob_sha)
    assert loaded == record

    path = scan_record_path(hub_root, record.blob_sha)
    assert path.exists()


def test_scan_lock_path_and_creation(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    blob_sha = "beef123"

    path = scan_lock_path(hub_root, blob_sha)
    assert path == (
        hub_root
        / ".codex-autorunner"
        / "templates"
        / "scans"
        / "locks"
        / "beef123.lock"
    )

    with scan_lock(hub_root, blob_sha):
        assert path.exists()
