from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

LOG_PREFIX_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\[(?P<level>[A-Z]+)\] "
    r"(?P<message>.*)$"
)

ERROR_EVENT_HINTS = (
    ".failed",
    ".error",
    ".timeout",
    ".disconnected",
    ".exception",
    "turn_error",
)


@dataclass(frozen=True)
class LogTraceMatch:
    path: Path
    line_no: int
    timestamp: Optional[str]
    level: Optional[str]
    event: Optional[str]
    payload: Optional[dict[str, Any]]
    raw_line: str
    is_error_candidate: bool
    context: tuple[str, ...]
    sequence: int = 0


def extract_conversation_id(query: str, conversation_pattern: re.Pattern[str]) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("conversation query is empty")
    match = conversation_pattern.search(cleaned)
    if match:
        return match.group("conversation_id").strip()
    lowered = cleaned.lower()
    if lowered.startswith("conversation "):
        cleaned = cleaned[len("conversation ") :].strip()
    return cleaned.strip("()[]{}.,;\"' ")


def split_log_line(raw_line: str) -> tuple[Optional[str], Optional[str], str]:
    match = LOG_PREFIX_PATTERN.match(raw_line)
    if not match:
        return None, None, raw_line
    return (
        match.group("timestamp"),
        match.group("level"),
        match.group("message"),
    )


def parse_log_payload(message: str) -> Optional[dict[str, Any]]:
    stripped = message.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def is_error_candidate(
    *,
    level: Optional[str],
    event: Optional[str],
    payload: Optional[dict[str, Any]],
    message: str,
) -> bool:
    normalized_level = (level or "").upper()
    if normalized_level in {"ERROR", "CRITICAL"}:
        return True
    normalized_event = (event or "").lower()
    if normalized_event and any(hint in normalized_event for hint in ERROR_EVENT_HINTS):
        return True
    if isinstance(payload, dict) and (
        isinstance(payload.get("error"), str)
        or isinstance(payload.get("error_type"), str)
    ):
        return True
    lowered_message = message.lower()
    return "traceback" in lowered_message or "exception" in lowered_message


def collect_log_paths(
    roots: list[Path],
    log_path: Path,
    backup_count: int,
    search_globs: tuple[str, ...],
) -> list[Path]:
    candidates: set[Path] = set()
    if log_path.exists():
        candidates.add(log_path)

    if backup_count > 0:
        for idx in range(1, backup_count + 1):
            rotated = log_path.with_name(f"{log_path.name}.{idx}")
            if rotated.exists():
                candidates.add(rotated)

    for root in roots:
        state_root = root / ".codex-autorunner"
        for pattern in search_globs:
            for path in state_root.glob(pattern):
                if path.is_file():
                    candidates.add(path)

    return sorted(candidates, key=lambda item: str(item))


def read_log_lines(path: Path, scan_lines: int) -> list[tuple[int, str]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if scan_lines <= 0:
            return [
                (idx, line.rstrip("\n")) for idx, line in enumerate(handle, start=1)
            ]
        tail: deque[tuple[int, str]] = deque(maxlen=scan_lines)
        for idx, line in enumerate(handle, start=1):
            tail.append((idx, line.rstrip("\n")))
    return list(tail)


def format_match_line(match: LogTraceMatch) -> str:
    timestamp = match.timestamp or "unknown-time"
    level = (match.level or "INFO").upper()
    event = f" event={match.event}" if match.event else ""
    return f"{timestamp} {level} {match.path}:{match.line_no}{event}"


def sanitize_payload_value(value: Any, text_sanitizer: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return text_sanitizer(value)
    if isinstance(value, list):
        return [sanitize_payload_value(item, text_sanitizer) for item in value]
    if isinstance(value, dict):
        return {
            str(key): sanitize_payload_value(inner_value, text_sanitizer)
            for key, inner_value in value.items()
        }
    return value
