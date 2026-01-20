from __future__ import annotations

import html
import re

_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ESCAPE_RE = re.compile(r"([_*\[\]\(\)`])")
_MARKDOWN_V2_ESCAPE_RE = re.compile(r"([_*\[\]\(\)~`>#+\-=|{}.!\\])")
_REASONING_PREFIX_RE = re.compile(
    r"^(?:\[reasoning\]|reasoning:|thinking:|thought:)\s*", re.IGNORECASE
)


def _clean_reasoning_from_output(text: str) -> str:
    lines = text.split("\n")
    cleaned_lines: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(line)
            continue
        if in_code_block:
            cleaned_lines.append(line)
            continue
        if _REASONING_PREFIX_RE.match(stripped):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)
    return result.strip()


def _format_telegram_html(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _CODE_BLOCK_RE.finditer(text):
        parts.append(_format_telegram_inline(text[last : match.start()]))
        code = match.group(1)
        parts.append("<pre><code>")
        parts.append(html.escape(code, quote=False))
        parts.append("</code></pre>")
        last = match.end()
    parts.append(_format_telegram_inline(text[last:]))
    return "".join(parts)


def _format_telegram_inline(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def _replace_code(match: re.Match[str]) -> str:
        placeholders.append(html.escape(match.group(1), quote=False))
        return f"\x00CODE{len(placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_replace_code, text)
    escaped = html.escape(text, quote=False)
    escaped = _BOLD_RE.sub(lambda match: f"<b>{match.group(1)}</b>", escaped)
    for idx, code in enumerate(placeholders):
        token = f"\x00CODE{idx}\x00"
        escaped = escaped.replace(token, f"<code>{code}</code>")
    return escaped


def _escape_markdown_text(text: str, *, version: str) -> str:
    if not text:
        return ""
    if version == "MarkdownV2":
        return _MARKDOWN_V2_ESCAPE_RE.sub(r"\\\1", text)
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


def _escape_markdown_code(text: str, *, version: str) -> str:
    if not text:
        return ""
    if version == "MarkdownV2":
        return text.replace("\\", "\\\\").replace("`", "\\`")
    return text.replace("`", "\\`")


def _format_telegram_markdown(text: str, version: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _CODE_BLOCK_RE.finditer(text):
        parts.append(
            _format_telegram_markdown_inline(text[last : match.start()], version)
        )
        code = _escape_markdown_code(match.group(1), version=version)
        parts.append(f"```\n{code}\n```")
        last = match.end()
    parts.append(_format_telegram_markdown_inline(text[last:], version))
    return "".join(parts)


def _format_telegram_markdown_inline(text: str, version: str) -> str:
    if not text:
        return ""
    code_placeholders: list[str] = []
    bold_placeholders: list[str] = []

    def _replace_code(match: re.Match[str]) -> str:
        code_placeholders.append(_escape_markdown_code(match.group(1), version=version))
        return f"\x00CODE{len(code_placeholders) - 1}\x00"

    def _replace_bold(match: re.Match[str]) -> str:
        bold_placeholders.append(_escape_markdown_text(match.group(1), version=version))
        return f"\x00BOLD{len(bold_placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_replace_code, text)
    text = _BOLD_RE.sub(_replace_bold, text)
    escaped = _escape_markdown_text(text, version=version)
    for idx, bold in enumerate(bold_placeholders):
        token = f"\x00BOLD{idx}\x00"
        escaped = escaped.replace(token, f"*{bold}*")
    for idx, code in enumerate(code_placeholders):
        token = f"\x00CODE{idx}\x00"
        escaped = escaped.replace(token, f"`{code}`")
    return escaped
