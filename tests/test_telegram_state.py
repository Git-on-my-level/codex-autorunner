from pathlib import Path

from codex_autorunner.telegram_state import TelegramStateStore


def test_telegram_state_global_update_id(tmp_path: Path) -> None:
    store = TelegramStateStore(tmp_path / "telegram_state.json")
    assert store.get_last_update_id_global() is None
    assert store.update_last_update_id_global(10) == 10
    assert store.get_last_update_id_global() == 10
    assert store.update_last_update_id_global(3) == 10
