from __future__ import annotations

from pathlib import Path

import pytest

_CHAT_SURFACE_LAB_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path = getattr(item, "path", None)
        if path is None:
            continue
        try:
            resolved = Path(path).resolve()
        except TypeError:
            continue
        if resolved.is_relative_to(_CHAT_SURFACE_LAB_DIR):
            item.add_marker(pytest.mark.slow)
