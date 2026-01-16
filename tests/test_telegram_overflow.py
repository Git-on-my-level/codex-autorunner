from codex_autorunner.integrations.telegram.constants import (
    SHELL_OUTPUT_TRUNCATION_SUFFIX,
)
from codex_autorunner.integrations.telegram.overflow import (
    split_markdown_message,
    trim_markdown_message,
)


def test_split_markdown_message_closes_code_fences() -> None:
    text = "```python\n" + ("print('x')\n" * 40) + "```"
    chunks = split_markdown_message(text, max_len=120)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 120
        assert chunk.count("<pre><code>") == chunk.count("</code></pre>")


def test_trim_markdown_message_appends_suffix() -> None:
    text = "hello " * 100
    trimmed = trim_markdown_message(text, max_len=120)

    assert len(trimmed) <= 120
    assert SHELL_OUTPUT_TRUNCATION_SUFFIX.strip() in trimmed


def test_split_markdown_message_adds_continued_indicator() -> None:
    text = "\n\n".join(
        [
            "First paragraph " * 5,
            "Second paragraph " * 5,
        ]
    )
    chunks = split_markdown_message(
        text,
        max_len=80,
        render=lambda value: value,
    )

    assert len(chunks) > 1
    total = len(chunks)
    assert f"continued (1/{total})" in chunks[0]
    assert f"continued ({total}/{total})" in chunks[-1]
    assert "Second paragraph" not in chunks[0]
