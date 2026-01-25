import re
from typing import List, Tuple

_REDACTIONS: List[Tuple[re.Pattern[str], str]] = [
    # OpenAI-like keys.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-[REDACTED]"),
    # GitHub personal access tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh_[REDACTED]"),
    # AWS access key ids (best-effort).
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    # JWT-ish blobs.
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[JWT_REDACTED]",
    ),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def get_redaction_patterns() -> List[str]:
    return [pattern.pattern for pattern, _ in _REDACTIONS]
