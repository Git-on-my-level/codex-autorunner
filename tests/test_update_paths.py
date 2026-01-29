from __future__ import annotations

from pathlib import Path

from codex_autorunner.core import update as update_core
from codex_autorunner.core.update_paths import resolve_update_paths
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
)


def test_update_paths_default_match_modules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    paths = resolve_update_paths()

    expected_root = tmp_path / ".codex-autorunner"
    assert paths.cache_dir == expected_root / "update_cache"
    assert paths.status_path == expected_root / "update_status.json"
    assert paths.lock_path == expected_root / "update.lock"
    assert paths.compact_status_path == expected_root / "compact_status.json"

    assert update_core._update_status_path() == paths.status_path
    assert update_core._update_lock_path() == paths.lock_path

    handler = TelegramCommandHandlers()
    assert handler._update_status_path() == paths.status_path
    assert handler._compact_status_path() == paths.compact_status_path
