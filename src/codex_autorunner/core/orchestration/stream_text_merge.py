from __future__ import annotations

import re
from dataclasses import dataclass

_NORMALIZE_DEDUP_RE = re.compile(r"[*_`#>~\s]+")


def normalize_stream_text_for_dedup(text: str) -> str:
    """Collapse markdown emphasis/heading markers and whitespace for comparison.

    Some agents (e.g. Hermes) stream a reasoning block as plain token deltas and
    then re-stream the same block reformatted with markdown
    (``**Title**\n\n...``). The two forms differ only in emphasis/whitespace, so
    neither contains the other as a raw substring and a naive merge concatenates
    a near-duplicate. Comparing the normalized forms lets the merge/coalesce
    layers recognize the reformat as a redundant re-emission.
    """
    return _NORMALIZE_DEDUP_RE.sub(" ", text).strip()


_NO_SPACE_BEFORE = frozenset(",.;:!?)]}")
_NO_SPACE_AFTER = frozenset("([{")

# Latin subword prefixes where Hermes-style token streams may split one word
# across chunks (for example ``inter`` + ``national`` -> ``international``).
_SUBWORD_PREFIXES_2 = frozenset({"de", "ex", "im", "in", "re", "un"})
_SUBWORD_PREFIXES_3_PLUS = frozenset(
    {
        "anti",
        "counter",
        "dis",
        "extra",
        "hyper",
        "inter",
        "macro",
        "mega",
        "meta",
        "micro",
        "mis",
        "mono",
        "multi",
        "non",
        "over",
        "post",
        "pre",
        "pseudo",
        "semi",
        "sub",
        "super",
        "trans",
        "under",
    }
)

# Short compounds that stay glued when split across tiny trailing chunks.
_SHORT_COMPOUND_MERGES = frozenset(
    {
        "codex",
        "inbox",
        "sitrep",
        "redo",
        "undo",
        "preset",
        "reapply",
        "reopen",
        "retest",
        "reedit",
    }
)


def _trailing_alpha_run(text: str) -> str:
    run = []
    for char in reversed(text):
        if not char.isalpha():
            break
        run.append(char)
    return "".join(reversed(run))


def _current_word_fragment(text: str) -> str:
    fragment = []
    for char in reversed(text):
        if char.isspace():
            break
        fragment.append(char)
    return "".join(reversed(fragment))


def _trailing_markdown_star_run_is_opening(text: str) -> bool:
    if not text.endswith("*"):
        return False
    run_length = 0
    for char in reversed(text):
        if char != "*":
            break
        run_length += 1
    if run_length not in {1, 2}:
        return False
    before_run_index = len(text) - run_length - 1
    if before_run_index < 0:
        return True
    before_run = text[before_run_index]
    return before_run.isspace() or before_run in "([{"


def _incoming_markdown_star_run_is_closing(current: str, incoming: str) -> bool:
    if incoming not in {"*", "**"}:
        return False
    return current.count(incoming) % 2 == 1


def _incoming_starts_closing_bold_run(current: str, incoming: str) -> bool:
    return incoming.startswith("**") and current.count("**") % 2 == 1


def _likely_subword_prefix_continuation(current: str, incoming: str) -> bool:
    """True when ``current + incoming`` is probably one word split for streaming."""
    if not current or not incoming:
        return False
    merged = f"{current}{incoming}"
    if merged.lower() in _SHORT_COMPOUND_MERGES:
        return True
    if not merged.isascii() or not merged.isalpha() or not merged.islower():
        return False
    if incoming in _SUBWORD_PREFIXES_2 | _SUBWORD_PREFIXES_3_PLUS:
        return False
    if len(incoming) < 4:
        return False
    if current in _SUBWORD_PREFIXES_3_PLUS:
        return True
    if len(current) == 2 and current in _SUBWORD_PREFIXES_2:
        return len(merged) >= 7
    return False


def _looks_like_token_continuation(current: str, incoming: str) -> bool:
    current_alpha = _trailing_alpha_run(current)
    incoming_alpha = "".join(char for char in incoming if char.isalpha())
    if not current_alpha or not incoming_alpha:
        return False
    if current_alpha.isupper() and incoming_alpha.isupper():
        return True
    fragment = _current_word_fragment(current)
    if any(char in fragment for char in "/._-`*"):
        return True
    if "." in incoming[:-1]:
        return True
    return _likely_subword_prefix_continuation(current_alpha, incoming_alpha)


def _needs_readable_boundary(current: str, incoming: str) -> bool:
    if not current or not incoming:
        return False
    previous = current[-1]
    next_char = incoming[0]
    if previous.isspace() or next_char.isspace():
        return False
    if next_char in _NO_SPACE_BEFORE:
        return False
    if previous in _NO_SPACE_AFTER:
        return False
    if _incoming_starts_closing_bold_run(current, incoming) and previous.isalnum():
        return False
    if incoming and all(char == "*" for char in incoming):
        if _incoming_markdown_star_run_is_closing(current, incoming):
            return False
        return previous in {".", ":", ";", "!", "?"}
    if previous.isdigit() and next_char.isdigit():
        return False
    if previous.isalnum() and (next_char.isalnum() or next_char in {"`", "*"}):
        if previous.isalpha() and next_char.isalpha():
            if _looks_like_token_continuation(current, incoming):
                return False
        return True
    if previous == "*" and next_char.isalnum():
        return not _trailing_markdown_star_run_is_opening(current)
    if previous == "`" and next_char.isalnum():
        return True
    if previous in {".", ":", ";", "!", "?"} and next_char in {"`", "*"}:
        return True
    return False


def append_assistant_stream_text_readably(current: str, incoming: str) -> str:
    """Append tokenized assistant chunks without erasing word boundaries."""

    if not incoming:
        return current
    if not current:
        return incoming
    separator = " " if _needs_readable_boundary(current, incoming) else ""
    return f"{current}{separator}{incoming}"


def merge_assistant_stream_text(current: str, incoming: str) -> str:
    """Merge overlapping streamed assistant chunks without duplicating prefixes."""
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current:
        return current
    if len(incoming) > len(current) and incoming.startswith(current):
        return incoming
    max_overlap = min(len(current), max(len(incoming) - 1, 0))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"


@dataclass
class AssistantTextAccumulator:
    """Shared assistant text reducer for stream and terminal message text."""

    stream_text: str = ""
    final_text: str = ""
    last_stream_chunk_had_explicit_spacing: bool = False

    @staticmethod
    def _has_explicit_spacing(text: str) -> bool:
        return any(char.isspace() for char in text)

    def _should_append_verbatim(self, text: str) -> bool:
        # ACP streams are mixed in practice. Hermes chunks often carry leading
        # spaces for the next word and then finish that same word in a following
        # chunk (``" Sit"`` + ``"rep"``), while other agents still need a
        # readable-boundary fallback later in the same turn.
        return (
            self._has_explicit_spacing(text)
            or self.last_stream_chunk_had_explicit_spacing
        )

    def append_delta(self, text: str, *, preserve_word_boundaries: bool = False) -> str:
        """Record a strict append-only stream delta."""
        if isinstance(text, str) and text:
            if preserve_word_boundaries:
                if self._should_append_verbatim(text):
                    self.stream_text = f"{self.stream_text}{text}"
                else:
                    self.stream_text = append_assistant_stream_text_readably(
                        self.stream_text, text
                    )
            else:
                self.stream_text = f"{self.stream_text}{text}"
            self.last_stream_chunk_had_explicit_spacing = self._has_explicit_spacing(
                text
            )
        return self.text

    def merge_snapshot(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        """Record a stream chunk that may be cumulative or overlap prior chunks."""
        if isinstance(text, str) and text:
            merged = merge_assistant_stream_text(self.stream_text, text)
            if (
                preserve_word_boundaries
                and not self._should_append_verbatim(text)
                and merged == f"{self.stream_text}{text}"
                and not text.startswith(self.stream_text)
            ):
                merged = append_assistant_stream_text_readably(self.stream_text, text)
            self.stream_text = merged
            self.last_stream_chunk_had_explicit_spacing = self._has_explicit_spacing(
                text
            )
        return self.text

    def replace_final(self, text: str) -> str:
        """Record the canonical terminal assistant message."""
        if isinstance(text, str):
            self.final_text = text
        return self.text

    @property
    def text(self) -> str:
        if self.final_text.strip():
            return self.final_text
        return self.stream_text


@dataclass
class AssistantOutputState(AssistantTextAccumulator):
    """Reduced assistant output state, distinct from append-only timelines."""

    def note_stream_delta(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        return self.append_delta(
            text, preserve_word_boundaries=preserve_word_boundaries
        )

    def note_stream_snapshot(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        return self.merge_snapshot(
            text, preserve_word_boundaries=preserve_word_boundaries
        )

    def note_final_message(self, text: str) -> str:
        return self.replace_final(text)
