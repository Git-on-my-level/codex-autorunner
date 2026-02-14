from __future__ import annotations

from pathlib import Path

from codex_autorunner.integrations.telegram.handlers.commands.files import (
    FilesCommands,
)


def test_pma_inbox_dir_uses_filebox(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)
    handler._hub_root = str(tmp_path)

    result = handler._pma_inbox_dir()

    assert result is not None
    assert result == tmp_path / ".codex-autorunner" / "filebox" / "inbox"


def test_pma_outbox_dir_uses_filebox(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)
    handler._hub_root = str(tmp_path)

    result = handler._pma_outbox_dir()

    assert result is not None
    assert result == tmp_path / ".codex-autorunner" / "filebox" / "outbox"


def test_pma_root_dir_uses_filebox(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)
    handler._hub_root = str(tmp_path)

    result = handler._pma_root_dir()

    assert result is not None
    assert result == tmp_path / ".codex-autorunner" / "filebox"


def test_pma_dirs_return_none_without_hub_root(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)

    assert handler._pma_root_dir() is None
    assert handler._pma_inbox_dir() is None
    assert handler._pma_outbox_dir() is None
