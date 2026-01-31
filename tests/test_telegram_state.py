from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.state import (
    TelegramStateStore,
    topic_key,
)


@pytest.mark.anyio
async def test_telegram_state_global_update_id(tmp_path: Path) -> None:
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        assert await store.get_last_update_id_global() is None
        assert await store.update_last_update_id_global(10) == 10
        assert await store.get_last_update_id_global() == 10
        assert await store.update_last_update_id_global(3) == 10
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_state_json_path_with_sqlite(tmp_path: Path) -> None:
    """
    Guard against regressions where a SQLite-backed state file still uses a
    `.json` suffix. The legacy migration should ignore the binary content
    instead of raising a UnicodeDecodeError.
    """

    path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(path)
    try:
        records = await store.list_pending_voice()
        assert records == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_state_pma_toggle(tmp_path: Path) -> None:
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    key = topic_key(123, None)
    try:
        record = await store.ensure_topic(key)
        assert record.pma_enabled is False
        record = await store.update_topic(
            key, lambda record: setattr(record, "pma_enabled", True)
        )
        assert record.pma_enabled is True
        record = await store.get_topic(key)
        assert record is not None
        assert record.pma_enabled is True
    finally:
        await store.close()
