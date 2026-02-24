from __future__ import annotations

from codex_autorunner.integrations.discord.rendering import (
    DISCORD_MAX_MESSAGE_LENGTH,
    chunk_discord_message,
    escape_discord_code,
    escape_discord_markdown,
    format_bold,
    format_code_block,
    format_discord_message,
    format_inline_code,
    format_italic,
    truncate_for_discord,
)


class TestEscapeDiscordMarkdown:
    def test_escapes_special_characters(self) -> None:
        assert escape_discord_markdown("*bold*") == "\\*bold\\*"
        assert escape_discord_markdown("_italic_") == "\\_italic\\_"
        assert escape_discord_markdown("`code`") == "\\`code\\`"
        assert escape_discord_markdown("~strike~") == "\\~strike\\~"

    def test_escapes_pipe_and_braces(self) -> None:
        assert escape_discord_markdown("|pipe|") == "\\|pipe\\|"
        assert escape_discord_markdown("{braces}") == "\\{braces\\}"

    def test_returns_empty_for_empty_input(self) -> None:
        assert escape_discord_markdown("") == ""


class TestEscapeDiscordCode:
    def test_escapes_backticks(self) -> None:
        assert escape_discord_code("use `code` here") == "use \\`code\\` here"

    def test_escapes_backslashes(self) -> None:
        assert escape_discord_code("path\\to\\file") == "path\\\\to\\\\file"

    def test_returns_empty_for_empty_input(self) -> None:
        assert escape_discord_code("") == ""


class TestFormatCodeBlock:
    def test_formats_plain_code_block(self) -> None:
        result = format_code_block("print('hello')")
        assert result == "```\nprint('hello')\n```"

    def test_formats_code_block_with_language(self) -> None:
        result = format_code_block("print('hello')", language="python")
        assert result == "```python\nprint('hello')\n```"

    def test_handles_empty_code(self) -> None:
        result = format_code_block("")
        assert result == "```\n```"


class TestFormatInlineCode:
    def test_formats_inline_code(self) -> None:
        result = format_inline_code("x = 1")
        assert result == "`x = 1`"

    def test_escapes_backticks_in_code(self) -> None:
        result = format_inline_code("use `backtick`")
        assert result == "`use \\`backtick\\``"

    def test_handles_empty_code(self) -> None:
        result = format_inline_code("")
        assert result == "``"


class TestFormatBold:
    def test_formats_bold_text(self) -> None:
        result = format_bold("important")
        assert result == "**important**"

    def test_escapes_markdown_in_text(self) -> None:
        result = format_bold("important *stuff*")
        assert result == "**important \\*stuff\\***"


class TestFormatItalic:
    def test_formats_italic_text(self) -> None:
        result = format_italic("emphasis")
        assert result == "*emphasis*"

    def test_escapes_markdown_in_text(self) -> None:
        result = format_italic("emphasis `code`")
        assert result == "*emphasis \\`code\\`*"


class TestFormatDiscordMessage:
    def test_preserves_code_blocks(self) -> None:
        text = "Here is code:\n```python\nprint('hi')\n```\nDone."
        result = format_discord_message(text)
        assert "print('hi')" in result
        assert "```" in result

    def test_preserves_inline_code(self) -> None:
        text = "Use `code` here"
        result = format_discord_message(text)
        assert "`code`" in result

    def test_preserves_bold_and_italic(self) -> None:
        text = "This is *italic* and **bold**"
        result = format_discord_message(text)
        assert "*italic*" in result
        assert "**bold**" in result

    def test_handles_empty_text(self) -> None:
        assert format_discord_message("") == ""


class TestTruncateForDiscord:
    def test_keeps_short_text(self) -> None:
        text = "Short text"
        assert truncate_for_discord(text) == text

    def test_truncates_long_text(self) -> None:
        text = "x" * 3000
        result = truncate_for_discord(text)
        assert len(result) == DISCORD_MAX_MESSAGE_LENGTH
        assert result.endswith("...")

    def test_uses_custom_max_len(self) -> None:
        text = "x" * 100
        result = truncate_for_discord(text, max_len=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_handles_empty_text(self) -> None:
        assert truncate_for_discord("") == ""


class TestChunkDiscordMessage:
    def test_returns_single_chunk_for_short_text(self) -> None:
        text = "Short text"
        chunks = chunk_discord_message(text)
        assert chunks == [text]

    def test_chunks_long_text(self) -> None:
        text = "Line 1\n" * 500
        chunks = chunk_discord_message(text, max_len=500)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 500

    def test_applies_numbering_when_enabled(self) -> None:
        text = "A" * 3000
        chunks = chunk_discord_message(text, max_len=1000, with_numbering=True)
        assert len(chunks) > 1
        for i, chunk in enumerate(chunks, 1):
            assert chunk.startswith(f"Part {i}/{len(chunks)}\n")

    def test_no_numbering_when_disabled(self) -> None:
        text = "A" * 3000
        chunks = chunk_discord_message(text, max_len=1000, with_numbering=False)
        for chunk in chunks:
            assert not chunk.startswith("Part ")

    def test_handles_empty_text(self) -> None:
        assert chunk_discord_message("") == []
