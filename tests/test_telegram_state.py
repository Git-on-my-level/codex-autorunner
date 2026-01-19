from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.state import TelegramStateStore


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
