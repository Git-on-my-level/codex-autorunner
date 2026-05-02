from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.browser.models import (
    Viewport,
    parse_viewport,
    resolve_out_dir,
)
from codex_autorunner.core.filebox import outbox_dir


def test_parse_viewport_accepts_width_x_height() -> None:
    parsed = parse_viewport("1920x1080")
    assert parsed == Viewport(width=1920, height=1080)


def test_parse_viewport_rejects_invalid_format() -> None:
    with pytest.raises(ValueError, match="Expected format WIDTHxHEIGHT"):
        parse_viewport("wide")


def test_resolve_out_dir_defaults_to_filebox_outbox(tmp_path: Path) -> None:
    assert resolve_out_dir(tmp_path, None) == outbox_dir(tmp_path)
