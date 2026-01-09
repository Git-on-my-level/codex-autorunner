from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.engine import Engine
from codex_autorunner.spec_ingest import (
    SpecIngestError,
    ensure_can_overwrite,
    parse_spec_ingest_output,
    write_ingested_docs,
)


def test_parse_spec_ingest_output_success() -> None:
    text = "<TODO>todo</TODO><PROGRESS>progress</PROGRESS><OPINIONS>opinions</OPINIONS>"
    parsed = parse_spec_ingest_output(text)
    assert parsed == {"todo": "todo", "progress": "progress", "opinions": "opinions"}


def test_parse_spec_ingest_output_missing_sections() -> None:
    with pytest.raises(SpecIngestError):
        parse_spec_ingest_output("<TODO>only</TODO>")


def test_ensure_can_overwrite_rejects_existing_docs(repo: Path) -> None:
    engine = Engine(repo)
    with pytest.raises(SpecIngestError):
        ensure_can_overwrite(engine, force=False)


def test_write_ingested_docs_appends_newline(repo: Path) -> None:
    engine = Engine(repo)
    write_ingested_docs(
        engine,
        {"todo": "hi", "progress": "there", "opinions": "friend"},
        force=True,
    )
    todo_text = engine.config.doc_path("todo").read_text(encoding="utf-8")
    assert todo_text.endswith("\n")
