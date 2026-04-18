"""Helpers for assembling dispatch/reply history and attachment URLs.

These were previously inline in messages.py and are reused across the
active-message, thread-list, and thread-detail endpoints.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


def timestamp(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def safe_attachment_name(name: str) -> str:
    base = os.path.basename(name or "").strip()
    if not base:
        raise ValueError("Missing attachment filename")
    if base.lower() == "user_reply.md":
        raise ValueError("Attachment filename reserved: USER_REPLY.md")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        raise ValueError(
            "Invalid attachment filename; use only letters, digits, dot, underscore, dash"
        )
    return base


def iter_seq_dirs(history_dir: Path) -> list[tuple[int, Path]]:
    if not history_dir.exists() or not history_dir.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    try:
        for child in history_dir.iterdir():
            try:
                if not child.is_dir():
                    continue
                name = child.name
                if not (len(name) == 4 and name.isdigit()):
                    continue
                out.append((int(name), child))
            except OSError:
                continue
    except OSError:
        return []
    out.sort(key=lambda x: x[0])
    return out


def collect_entry_files(
    entry_dir: Path,
    *,
    skip_name: str,
    url_prefix: str,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    try:
        for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
            try:
                if child.name.startswith("."):
                    continue
                if child.name == skip_name:
                    continue
                if child.is_dir():
                    continue
                url = f"{url_prefix}/{quote(child.name)}"
                size: Optional[int] = None
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                files.append({"name": child.name, "url": url, "size": size})
            except OSError:
                continue
    except OSError:
        files = []
    return files


def ticket_state_snapshot(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    ticket_state = state.get("ticket_engine") if isinstance(state, dict) else {}
    if not isinstance(ticket_state, dict):
        ticket_state = {}
    allowed_keys = {
        "current_ticket",
        "total_turns",
        "ticket_turns",
        "dispatch_seq",
        "reply_seq",
        "reason",
        "status",
    }
    return {k: ticket_state.get(k) for k in allowed_keys if k in ticket_state}
