import concurrent.futures
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import pma_active_context_content, seed_hub_files
from codex_autorunner.core import filebox
from codex_autorunner.core.config import DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_context import maybe_auto_prune_active_context
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import pma as pma_routes
from tests.pma_support import _disable_pma, _enable_pma

pytestmark = pytest.mark.slow


def test_pma_files_list_empty(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []
    assert payload["outbox"] == []


def test_pma_files_upload_list_download_delete(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to inbox
    files = {"file.txt": ("file.txt", b"Hello, PMA!", "text/plain")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    assert payload["inbox"][0]["name"] == "file.txt"
    assert payload["inbox"][0]["box"] == "inbox"
    assert payload["inbox"][0]["size"] == 11
    assert payload["inbox"][0]["source"] == "filebox"
    assert payload["inbox"][0]["next_action"] == "process_uploaded_file"
    assert payload["inbox"][0]["likely_false_positive"] is False
    assert "/hub/pma/files/inbox/file.txt" in payload["inbox"][0]["url"]
    assert payload["outbox"] == []
    assert (
        filebox.inbox_dir(hub_env.hub_root) / "file.txt"
    ).read_bytes() == b"Hello, PMA!"

    # Download file
    resp = client.get("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200
    assert resp.content == b"Hello, PMA!"

    # Delete file
    resp = client.delete("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200

    # Verify file is gone
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []


def test_pma_files_invalid_box(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Try to upload to invalid box
    files = {"file.txt": ("file.txt", b"test", "text/plain")}
    resp = client.post("/hub/pma/files/invalid", files=files)
    assert resp.status_code == 400

    # Try to download from invalid box
    resp = client.get("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400

    # Try to delete from invalid box
    resp = client.delete("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400


def test_pma_files_list_ignores_legacy_sources(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "primary.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy-pma.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "legacy-telegram.txt").write_bytes(b"legacy-telegram")

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    item = payload["inbox"][0]
    assert item["box"] == "inbox"
    assert item["item_type"] == "pma_file"
    assert item["name"] == "primary.txt"
    assert item["next_action"] == "process_uploaded_file"
    assert item["likely_false_positive"] is False
    assert item["size"] == 7
    assert item["source"] == "filebox"
    assert item["url"].endswith("/hub/pma/files/inbox/primary.txt")
    assert item["modified_at"]
    assert (item.get("freshness") or {}).get("is_stale") is False


def test_pma_files_list_marks_old_inbox_files_as_likely_stale(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    inbox_file = filebox.inbox_dir(hub_env.hub_root) / "forgotten.txt"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_bytes(b"old")
    old_ts = datetime(2026, 3, 16, 9, 0, tzinfo=timezone.utc).timestamp()
    os.utime(inbox_file, (old_ts, old_ts))

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    item = payload["inbox"][0]
    assert item["name"] == "forgotten.txt"
    assert item["next_action"] == "review_stale_uploaded_file"
    assert item["likely_false_positive"] is True
    assert "already handled" in (item["attention_summary"] or "").lower()
    assert (item.get("freshness") or {}).get("is_stale") is True


def test_pma_files_download_rejects_legacy_path(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy.txt").write_bytes(b"legacy")

    resp = client.get("/hub/pma/files/inbox/legacy.txt")
    assert resp.status_code == 404


def test_pma_files_outbox(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to outbox
    files = {"output.txt": ("output.txt", b"Output content", "text/plain")}
    resp = client.post("/hub/pma/files/outbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["outbox"]) == 1
    assert payload["outbox"][0]["name"] == "output.txt"
    assert payload["outbox"][0]["box"] == "outbox"
    assert "/hub/pma/files/outbox/output.txt" in payload["outbox"][0]["url"]
    assert payload["inbox"] == []

    # Download from outbox
    resp = client.get("/hub/pma/files/outbox/output.txt")
    assert resp.status_code == 200
    assert resp.content == b"Output content"


def test_pma_files_delete_removes_only_resolved_file(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-2"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "shared.txt").write_bytes(b"legacy-telegram")

    resp = client.delete("/hub/pma/files/inbox/shared.txt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    assert (legacy_telegram / "shared.txt").exists()


def test_pma_files_bulk_delete_removes_only_canonical_entries(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "a.txt").write_bytes(b"a")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "b.txt").write_bytes(b"b")
    legacy_telegram_pending = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-3"
        / "outbox"
        / "pending"
    )
    legacy_telegram_pending.mkdir(parents=True, exist_ok=True)
    (legacy_telegram_pending / "c.txt").write_bytes(b"c")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (client.get("/hub/pma/files").json()["outbox"]) == []
    assert not (filebox.outbox_dir(hub_env.hub_root) / "a.txt").exists()
    assert (legacy_pma / "b.txt").exists()
    assert (legacy_telegram_pending / "c.txt").exists()


def test_pma_files_bulk_delete_leaves_legacy_duplicates_hidden(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    payload = client.get("/hub/pma/files").json()
    assert payload["outbox"] == []


def test_pma_files_list_waits_for_bulk_delete_to_finish(hub_env, monkeypatch) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    race_file = filebox.inbox_dir(hub_env.hub_root) / "race.txt"
    race_file.write_bytes(b"race")

    unlink_started = threading.Event()
    release_unlink = threading.Event()
    original_unlink = Path.unlink

    def _gated_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == race_file and not unlink_started.is_set():
            unlink_started.set()
            if not release_unlink.wait(timeout=3):
                raise AssertionError("Timed out waiting to release bulk delete unlink")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _gated_unlink)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(lambda: client.delete("/hub/pma/files/inbox"))
        assert unlink_started.wait(timeout=3), "Bulk delete did not reach unlink gate"
        list_future = executor.submit(lambda: client.get("/hub/pma/files"))
        release_unlink.set()

        delete_resp = delete_future.result(timeout=3)
        list_resp = list_future.result(timeout=3)

    assert delete_resp.status_code == 200
    assert list_resp.status_code == 200
    assert list_resp.json()["inbox"] == []


def test_pma_files_rejects_invalid_filenames(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Test traversal attempts - upload rejects invalid filenames
    for filename in ["../x", "..", "a/b", "a\\b", ".", ""]:
        files = {"file": (filename, b"test", "text/plain")}
        resp = client.post("/hub/pma/files/inbox", files=files)
        assert resp.status_code == 400, f"Should reject filename: {filename}"
        assert "filename" in resp.json()["detail"].lower()


def test_pma_files_size_limit(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    max_upload_bytes = DEFAULT_HUB_CONFIG["pma"]["max_upload_bytes"]

    # Upload a file that exceeds the size limit
    large_content = b"x" * (max_upload_bytes + 1)
    files = {"large.bin": ("large.bin", large_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()

    # Upload a file that is exactly at the limit
    limit_content = b"y" * max_upload_bytes
    files = {"limit.bin": ("limit.bin", limit_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200
    assert "limit.bin" in resp.json()["saved"]


def test_pma_files_returns_404_for_nonexistent_files(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Download non-existent file
    resp = client.get("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]

    # Delete non-existent file
    resp = client.delete("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]


def test_pma_docs_list(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    assert "docs" in payload
    docs = payload["docs"]
    assert isinstance(docs, list)
    doc_names = [doc["name"] for doc in docs]
    assert doc_names == [
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    ]
    for doc in docs:
        assert "name" in doc
        assert "exists" in doc
        if doc["exists"]:
            assert "size" in doc
            assert "mtime" in doc
        if doc["name"] == "active_context.md":
            assert "line_count" in doc


def test_pma_docs_list_includes_auto_prune_metadata(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    active_context = (
        hub_env.hub_root / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    active_context.write_text(
        "\n".join(f"line {idx}" for idx in range(220)), encoding="utf-8"
    )
    state = maybe_auto_prune_active_context(hub_env.hub_root, max_lines=50)
    assert state is not None

    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    meta = payload.get("active_context_auto_prune")
    assert isinstance(meta, dict)
    assert meta.get("line_count_before") == 220
    assert meta.get("line_budget") == 50
    assert isinstance(meta.get("last_auto_pruned_at"), str)


def test_pma_docs_get(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert "content" in payload
    assert isinstance(payload["content"], str)


def test_pma_docs_get_nonexistent(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Delete the canonical doc, then try to get it
    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_agents_path = pma_dir / "docs" / "AGENTS.md"
    if docs_agents_path.exists():
        docs_agents_path.unlink()

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_pma_docs_list_migrates_legacy_doc_into_canonical(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_path = pma_dir / "docs" / "active_context.md"
    legacy_path = pma_dir / "active_context.md"

    docs_path.unlink(missing_ok=True)
    legacy_content = "# Legacy copy\n\n- migrated\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200

    assert docs_path.exists()
    assert docs_path.read_text(encoding="utf-8") == legacy_content
    assert not legacy_path.exists()

    resp = client.get("/hub/pma/docs/active_context.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == legacy_content


def test_pma_docs_put(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    new_content = "# AGENTS\n\nNew content"
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": new_content})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert payload["status"] == "ok"

    # Verify the content was saved
    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == new_content


def test_pma_docs_put_writes_via_to_thread(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    to_thread_calls: list[tuple[object, tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(pma_routes.asyncio, "to_thread", _fake_to_thread)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": "# AGENTS\n\nText"})
    assert resp.status_code == 200
    assert any(func is pma_routes.atomic_write for func, _, _ in to_thread_calls)


def test_pma_docs_put_invalid_name(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/invalid.md", json={"content": "test"})
    assert resp.status_code == 400
    assert "Unknown doc name" in resp.json()["detail"]


def test_pma_docs_put_too_large(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    large_content = "x" * 500_001
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": large_content})
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_pma_docs_put_invalid_content_type(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": 123})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI returns a list of validation errors
    if isinstance(detail, list):
        assert any("content" in str(err) for err in detail)
    else:
        assert "content" in str(detail)


def test_pma_context_snapshot(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_content = "# Active Context\n\n- alpha\n- beta\n"
    active_path.write_text(active_content, encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["active_context_line_count"] == len(active_content.splitlines())
    assert payload["reset"] is True

    log_content = (docs_dir / "context_log.md").read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert active_content in log_content
    assert active_path.read_text(encoding="utf-8") == pma_active_context_content()


def test_pma_context_snapshot_writes_via_to_thread(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    to_thread_calls: list[tuple[object, tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(pma_routes.asyncio, "to_thread", _fake_to_thread)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_path.write_text("# Active Context\n\n- alpha\n", encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    assert any(func is pma_routes.ensure_pma_docs for func, _, _ in to_thread_calls)
    assert any(func is pma_routes.atomic_write for func, _, _ in to_thread_calls)


def test_pma_docs_disabled(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 404

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
