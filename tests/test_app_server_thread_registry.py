import json
from pathlib import Path

from codex_autorunner.core.app_server_threads import (
    AppServerThreadRegistry,
    normalize_feature_key,
)


def test_thread_registry_corruption_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "app_server_threads.json"
    path.write_text("{not-json", encoding="utf-8")

    registry = AppServerThreadRegistry(path)
    threads = registry.load()

    assert threads == {}
    notice = registry.corruption_notice()
    assert notice is not None
    assert notice.get("status") == "corrupt"
    backup = notice.get("backup_path")
    assert backup
    assert Path(backup).exists()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired.get("threads") == {}


def test_thread_registry_reset_all_clears_notice(tmp_path: Path) -> None:
    path = tmp_path / "app_server_threads.json"
    path.write_text("{not-json", encoding="utf-8")
    registry = AppServerThreadRegistry(path)
    registry.load()
    assert registry.corruption_notice()

    registry.reset_all()

    assert registry.corruption_notice() is None


def test_normalize_feature_key_accepts_pma() -> None:
    assert normalize_feature_key("pma") == "pma"
    assert normalize_feature_key("pma.opencode") == "pma.opencode"
    assert normalize_feature_key("PMA:OPENCODE") == "pma.opencode"
