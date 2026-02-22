from __future__ import annotations

import re
from typing import Final

from .overflow import split_markdown_message, trim_markdown_message

DISCORD_MAX_MESSAGE_LENGTH: Final[int] = 2000

_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_DISCORD_ESCAPE_RE = re.compile(r"([*_~`>|{}\\])")


def escape_discord_markdown(text: str) -> str:
    if not text:
        return ""
    return _DISCORD_ESCAPE_RE.sub(r"\\\1", text)


def escape_discord_code(text: str) -> str:
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def format_code_block(code: str, language: str = "") -> str:
    if not code:
        return "```\n```"
    escaped = code.replace("```", "\\`\\`\\`")
    if language:
        return f"```{language}\n{escaped}\n```"
    return f"```\n{escaped}\n```"


def format_inline_code(code: str) -> str:
    if not code:
        return "``"
    escaped = escape_discord_code(code)
    return f"`{escaped}`"


def format_bold(text: str) -> str:
    if not text:
        return "****"
    escaped = escape_discord_markdown(text)
    return f"**{escaped}**"


def format_italic(text: str) -> str:
    if not text:
        return "**"
    escaped = escape_discord_markdown(text)
    return f"*{escaped}*"


def format_discord_message(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _CODE_BLOCK_RE.finditer(text):
        parts.append(_format_discord_inline(text[last : match.start()]))
        language = match.group(1)
        code = match.group(2).replace("```", "\\`\\`\\`")
        parts.append(f"```{language}\n{code}\n```")
        last = match.end()
    parts.append(_format_discord_inline(text[last:]))
    return "".join(parts)


def _format_discord_inline(text: str) -> str:
    if not text:
        return ""
    code_placeholders: list[str] = []
    bold_placeholders: list[str] = []
    italic_placeholders: list[str] = []

    def _replace_code(match: re.Match[str]) -> str:
        code_placeholders.append(escape_discord_code(match.group(1)))
        return f"\x00CODE{len(code_placeholders) - 1}\x00"

    def _replace_bold(match: re.Match[str]) -> str:
        bold_placeholders.append(escape_discord_markdown(match.group(1)))
        return f"\x00BOLD{len(bold_placeholders) - 1}\x00"

    def _replace_italic(match: re.Match[str]) -> str:
        italic_placeholders.append(escape_discord_markdown(match.group(1)))
        return f"\x00ITALIC{len(italic_placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_replace_code, text)
    text = _BOLD_RE.sub(_replace_bold, text)
    text = _ITALIC_RE.sub(_replace_italic, text)

    escaped = escape_discord_markdown(text)

    for idx, italic in enumerate(italic_placeholders):
        token = f"\x00ITALIC{idx}\x00"
        escaped = escaped.replace(token, f"*{italic}*")
    for idx, bold in enumerate(bold_placeholders):
        token = f"\x00BOLD{idx}\x00"
        escaped = escaped.replace(token, f"**{bold}**")
    for idx, code in enumerate(code_placeholders):
        token = f"\x00CODE{idx}\x00"
        escaped = escaped.replace(token, f"`{code}`")

    return escaped


def truncate_for_discord(text: str, max_len: int = DISCORD_MAX_MESSAGE_LENGTH) -> str:
    if not text:
        return ""
    return trim_markdown_message(text, max_len=max_len)


def chunk_discord_message(
    text: str, max_len: int = DISCORD_MAX_MESSAGE_LENGTH, with_numbering: bool = True
) -> list[str]:
    if not text:
        return []
    return split_markdown_message(
        text, max_len=max_len, include_indicator=with_numbering
    )
