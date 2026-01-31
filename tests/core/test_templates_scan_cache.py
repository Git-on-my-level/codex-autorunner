from pathlib import Path

from codex_autorunner.core.templates.scan_cache import (
    TemplateScanRecord,
    get_scan_record,
    scan_lock,
    scan_lock_path,
    scan_record_path,
    write_scan_record,
)


def _record(
    blob_sha: str,
    decision: str = "approve",
    reason: str = "safe",
) -> TemplateScanRecord:
    return TemplateScanRecord(
        blob_sha=blob_sha,
        repo_id="blessed",
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="deadbeef",
        trusted=False,
        decision=decision,
        severity="low",
        reason=reason,
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


def test_scan_cache_roundtrip_with_all_fields(tmp_path: Path) -> None:
    """Test that all fields survive a write+read roundtrip."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    record = TemplateScanRecord(
        blob_sha="abc123def456",
        repo_id="my-team",
        path="deep/nested/template.md",
        ref="feature-branch",
        commit_sha="commit123",
        trusted=False,
        decision="approve",
        severity="low",
        reason="Template is safe",
        evidence=["evidence1", "evidence2", "evidence3"],
        scanned_at="2026-01-31T12:34:56Z",
        scanner={"agent": "scan-agent-v1", "model": "gpt-4", "version": "1.0"},
    )

    write_scan_record(record, hub_root)

    loaded = get_scan_record(hub_root, record.blob_sha)
    assert loaded is not None
    assert loaded == record
    assert loaded.blob_sha == "abc123def456"
    assert loaded.repo_id == "my-team"
    assert loaded.path == "deep/nested/template.md"
    assert loaded.ref == "feature-branch"
    assert loaded.commit_sha == "commit123"
    assert loaded.trusted is False
    assert loaded.decision == "approve"
    assert loaded.severity == "low"
    assert loaded.reason == "Template is safe"
    assert loaded.evidence == ["evidence1", "evidence2", "evidence3"]
    assert loaded.scanned_at == "2026-01-31T12:34:56Z"
    assert loaded.scanner == {
        "agent": "scan-agent-v1",
        "model": "gpt-4",
        "version": "1.0",
    }


def test_scan_cache_with_minimal_fields(tmp_path: Path) -> None:
    """Test that records work with minimal required fields."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    record = TemplateScanRecord(
        blob_sha="min123",
        repo_id="test",
        path="template.md",
        ref="main",
        commit_sha="commitmin",
        trusted=True,
        decision="approve",
        severity="low",
        reason="trusted repo",
        evidence=None,
        scanned_at="2026-01-31T00:00:00Z",
        scanner=None,
    )

    write_scan_record(record, hub_root)

    loaded = get_scan_record(hub_root, record.blob_sha)
    assert loaded is not None
    assert loaded == record
    assert loaded.evidence is None
    assert loaded.scanner is None


def test_scan_cache_missing_returns_none(tmp_path: Path) -> None:
    """Test that get_scan_record returns None for missing blob_sha."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    loaded = get_scan_record(hub_root, "nonexistent123")
    assert loaded is None


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


def test_scan_lock_context_manager(tmp_path: Path) -> None:
    """Test that scan_lock properly acquires and releases the lock."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    blob_sha = "lock123"

    path = scan_lock_path(hub_root, blob_sha)

    with scan_lock(hub_root, blob_sha):
        assert path.exists()


def test_scan_record_path_format(tmp_path: Path) -> None:
    """Test that scan_record_path generates correct paths."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    path = scan_record_path(hub_root, "sha123")
    assert path == (
        hub_root / ".codex-autorunner" / "templates" / "scans" / "sha123.json"
    )


def test_scan_multiple_records(tmp_path: Path) -> None:
    """Test storing and retrieving multiple scan records."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    record1 = _record("blob001")
    record2 = _record("blob002")
    record3 = _record("blob003")

    write_scan_record(record1, hub_root)
    write_scan_record(record2, hub_root)
    write_scan_record(record3, hub_root)

    loaded1 = get_scan_record(hub_root, "blob001")
    loaded2 = get_scan_record(hub_root, "blob002")
    loaded3 = get_scan_record(hub_root, "blob003")

    assert loaded1 == record1
    assert loaded2 == record2
    assert loaded3 == record3

    assert get_scan_record(hub_root, "blob999") is None


def test_scan_record_overwrite(tmp_path: Path) -> None:
    """Test that overwriting a record replaces the old one."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    original = _record("overwrite123", decision="deny", reason="original reason")
    write_scan_record(original, hub_root)

    updated = TemplateScanRecord(
        blob_sha="overwrite123",
        repo_id="blessed",
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="newcommit",
        trusted=False,
        decision="approve",
        severity="low",
        reason="updated reason",
        evidence=["new evidence"],
        scanned_at="2026-01-31T01:00:00Z",
        scanner={"agent": "scan-agent-v2"},
    )
    write_scan_record(updated, hub_root)

    loaded = get_scan_record(hub_root, "overwrite123")
    assert loaded is not None
    assert loaded == updated
    assert loaded.decision == "approve"
    assert loaded.reason == "updated reason"
    assert loaded.commit_sha == "newcommit"
