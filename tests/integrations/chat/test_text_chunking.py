from __future__ import annotations

import pytest

from codex_autorunner.integrations.chat.text_chunking import chunk_text


def test_chunk_text_empty_or_whitespace() -> None:
    assert chunk_text("", max_len=10) == []
    assert chunk_text("   \n\t  ", max_len=10) == []


def test_chunk_text_single_chunk() -> None:
    text = "short message"
    assert chunk_text(text, max_len=100, with_numbering=True) == [text]
    assert chunk_text(text, max_len=100, with_numbering=False) == [text]


def test_chunk_text_multi_chunk_with_numbering() -> None:
    text = "alpha " * 200
    parts = chunk_text(text, max_len=120, with_numbering=True)
    assert len(parts) > 1
    assert parts[0].startswith("Part 1/")
    assert parts[-1].startswith(f"Part {len(parts)}/")
    assert all(len(part) <= 120 for part in parts)


def test_chunk_text_multi_chunk_without_numbering() -> None:
    text = "alpha " * 200
    parts = chunk_text(text, max_len=120, with_numbering=False)
    assert len(parts) > 1
    assert not parts[0].startswith("Part 1/")
    assert all(len(part) <= 120 for part in parts)


def test_chunk_text_raises_for_invalid_max_len() -> None:
    with pytest.raises(ValueError, match="max_len must be positive"):
        chunk_text("hello", max_len=0)
