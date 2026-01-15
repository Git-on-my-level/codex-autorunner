from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.engine import Engine
from codex_autorunner.spec_ingest import (
    SpecIngestError,
    clear_work_docs,
    ensure_can_overwrite,
)


def test_ensure_can_overwrite_rejects_existing_docs(repo: Path) -> None:
    engine = Engine(repo)
    with pytest.raises(SpecIngestError):
        ensure_can_overwrite(engine, force=False)


def test_clear_work_docs_resets_defaults(repo: Path) -> None:
    engine = Engine(repo)
    docs = clear_work_docs(engine)
    assert docs["todo"] == "# TODO\n\n"
    assert docs["progress"] == "# Progress\n\n"
    assert docs["opinions"] == "# Opinions\n\n"
