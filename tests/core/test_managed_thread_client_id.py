from pathlib import Path

import pytest

from codex_autorunner.core.managed_thread_store import ManagedThreadStore


def test_managed_thread_store_preserves_client_supplied_id(tmp_path: Path) -> None:
    store = ManagedThreadStore(tmp_path / "hub")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    thread = store.create_thread(
        "codex",
        workspace,
        managed_thread_id="pma:11111111-1111-4111-8111-111111111111",
    )

    assert thread["managed_thread_id"] == "pma:11111111-1111-4111-8111-111111111111"
    assert store.get_thread("pma:11111111-1111-4111-8111-111111111111") is not None


def test_managed_thread_store_rejects_non_uuid_client_ids(tmp_path: Path) -> None:
    store = ManagedThreadStore(tmp_path / "hub")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="UUID"):
        store.create_thread(
            "codex",
            workspace,
            managed_thread_id="pma:client-1",
        )


def test_managed_thread_store_rejects_duplicate_client_ids(tmp_path: Path) -> None:
    store = ManagedThreadStore(tmp_path / "hub")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    managed_thread_id = "pma:22222222-2222-4222-8222-222222222222"
    store.create_thread("codex", workspace, managed_thread_id=managed_thread_id)

    with pytest.raises(ValueError, match="already exists"):
        store.create_thread("codex", workspace, managed_thread_id=managed_thread_id)
