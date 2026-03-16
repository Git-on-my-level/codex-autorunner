"""
AppServerThreadRegistry: Post-Cutover Thread Identity Cache

This module provides the registry that backs thread identity lookups across
managed-thread surfaces after the BackendOrchestrator removal.

The registry is NOT an orphaned BackendOrchestrator artifact. It remains a
valid runtime state store for:

1. PMA lifecycle resets (/new, /reset commands in Telegram/Discord/Web)
2. Telegram PMA thread identity (per-topic PMA isolation when require_topics)
3. Hub/Web channel status reads (active thread lookups for status display)
4. Discord file-chat thread lookups (channel-scoped conversations)

The registry stores feature-key to thread-id mappings in a per-worktree JSON
file under `.codex-autorunner/app_server_threads.json`. Keys are normalized
to lowercase with `.` separators.

Replacing this registry with PmaThreadStore or orchestration.sqlite3 requires
a separate migration ticket with explicit state-migration rules. Do not treat
it as dead code.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...core.locks import file_lock
from ...core.utils import atomic_write

APP_SERVER_THREADS_FILENAME = ".codex-autorunner/app_server_threads.json"
APP_SERVER_THREADS_VERSION = 1
APP_SERVER_THREADS_CORRUPT_SUFFIX = ".corrupt"
APP_SERVER_THREADS_NOTICE_SUFFIX = ".corrupt.json"
FILE_CHAT_KEY = "file_chat"
FILE_CHAT_OPENCODE_KEY = "file_chat.opencode"
FILE_CHAT_PREFIX = "file_chat."
FILE_CHAT_OPENCODE_PREFIX = "file_chat.opencode."
PMA_KEY = "pma"
PMA_OPENCODE_KEY = "pma.opencode"
PMA_PREFIX = "pma."
PMA_OPENCODE_PREFIX = "pma.opencode."

LOGGER = logging.getLogger("codex_autorunner.app_server")

# Static keys that can be reset/managed via the UI.
FEATURE_KEYS = {
    FILE_CHAT_KEY,
    FILE_CHAT_OPENCODE_KEY,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    "autorunner",
    "autorunner.opencode",
}


def pma_base_key(agent: str) -> str:
    """
    Return the base PMA registry key for the given agent.

    Args:
        agent: Agent identifier ("opencode" or "codex"/other).

    Returns:
        PMA_OPENCODE_KEY if agent is "opencode", otherwise PMA_KEY.
    """
    if isinstance(agent, str) and agent.strip().lower() == "opencode":
        return PMA_OPENCODE_KEY
    return PMA_KEY


def pma_topic_scoped_key(
    agent: str, chat_id: int, thread_id: Optional[int], topic_key_fn=None
) -> str:
    """
    Build a topic-scoped PMA registry key.

    Used by Telegram PMA when require_topics is enabled to give each topic
    its own isolated PMA conversation context.

    Args:
        agent: Agent identifier ("opencode" or "codex"/other).
        chat_id: Telegram chat ID.
        thread_id: Telegram thread/topic ID (None for root).
        topic_key_fn: Optional function to build topic key (injected for testing).

    Returns:
        Topic-scoped key like "pma.{chat_id}:{thread}" or "pma.opencode.{chat_id}:{thread}".
    """
    base = pma_base_key(agent)
    if topic_key_fn is None:
        from ..telegram.state import topic_key as _default_topic_key

        topic_key_fn = _default_topic_key
    return f"{base}.{topic_key_fn(chat_id, thread_id)}"


def file_chat_discord_key(agent: str, channel_id: str, workspace_path: str) -> str:
    """
    Build a Discord file-chat registry key.

    Discord file-chat keys are scoped to channel and workspace to allow
    multiple Discord channels to have independent file-chat threads per repo.

    Args:
        agent: Agent identifier ("opencode" or "codex"/other).
        channel_id: Discord channel ID.
        workspace_path: Absolute workspace path (hashed for key stability).

    Returns:
        Registry key like "file_chat.discord.{channel}.{digest}" or
        "file_chat.opencode.discord.{channel}.{digest}".
    """
    prefix = FILE_CHAT_OPENCODE_PREFIX if agent == "opencode" else FILE_CHAT_PREFIX
    digest = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}discord.{channel_id.strip()}.{digest}"


def default_app_server_threads_path(repo_root: Path) -> Path:
    return repo_root / APP_SERVER_THREADS_FILENAME


def normalize_feature_key(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("feature key must be a string")
    key = raw.strip().lower()
    if not key:
        raise ValueError("feature key is required")
    key = key.replace("/", ".").replace(":", ".")
    if key in FEATURE_KEYS:
        return key
    # Allow per-target file chat threads (e.g. file_chat.ticket.1, file_chat.workspace.spec).
    for prefix in (FILE_CHAT_PREFIX, FILE_CHAT_OPENCODE_PREFIX):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    # Allow per-topic PMA threads (e.g. pma.-1001234567890:42, pma.opencode.123:root).
    for prefix in (PMA_PREFIX, PMA_OPENCODE_PREFIX):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    raise ValueError(f"invalid feature key: {raw}")


class AppServerThreadRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def _notice_path(self) -> Path:
        return self._path.with_name(
            f"{self._path.name}{APP_SERVER_THREADS_NOTICE_SUFFIX}"
        )

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def corruption_notice(self) -> Optional[dict]:
        path = self._notice_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def clear_corruption_notice(self) -> None:
        self._notice_path().unlink(missing_ok=True)

    def load(self) -> dict[str, str]:
        with file_lock(self._lock_path()):
            return self._load_unlocked()

    def feature_map(self) -> dict[str, object]:
        threads = self.load()
        payload: dict[str, object] = {
            "file_chat": threads.get(FILE_CHAT_KEY),
            "file_chat_opencode": threads.get(FILE_CHAT_OPENCODE_KEY),
            "autorunner": threads.get("autorunner"),
            "autorunner_opencode": threads.get("autorunner.opencode"),
            "pma": threads.get(PMA_KEY),
            "pma_opencode": threads.get(PMA_OPENCODE_KEY),
        }
        notice = self.corruption_notice()
        if notice:
            payload["corruption"] = notice
        return payload

    def get_thread_id(self, key: str) -> Optional[str]:
        normalized = normalize_feature_key(key)
        with file_lock(self._lock_path()):
            threads = self._load_unlocked()
            return threads.get(normalized)

    def set_thread_id(self, key: str, thread_id: str) -> None:
        normalized = normalize_feature_key(key)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread id is required")
        with file_lock(self._lock_path()):
            threads = self._load_unlocked()
            threads[normalized] = thread_id
            self._save_unlocked(threads)

    def reset_thread(self, key: str) -> bool:
        normalized = normalize_feature_key(key)
        with file_lock(self._lock_path()):
            threads = self._load_unlocked()
            if normalized not in threads:
                return False
            threads.pop(normalized, None)
            self._save_unlocked(threads)
            return True

    def reset_all(self) -> None:
        with file_lock(self._lock_path()):
            self._save_unlocked({})
            self.clear_corruption_notice()

    def _load_unlocked(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._handle_corrupt_registry(str(exc))
            return {}
        if not isinstance(data, dict):
            return {}
        threads_raw = data.get("threads")
        if isinstance(threads_raw, dict):
            source = threads_raw
        else:
            source = data
        threads: dict[str, str] = {}
        for key, value in source.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                threads[key] = value
        return threads

    def _save_unlocked(self, threads: dict[str, str]) -> None:
        payload = {
            "version": APP_SERVER_THREADS_VERSION,
            "threads": threads,
        }
        atomic_write(self._path, json.dumps(payload, indent=2) + "\n")

    def _handle_corrupt_registry(self, detail: str) -> None:
        stamp = self._stamp()
        backup_path = self._path.with_name(
            f"{self._path.name}{APP_SERVER_THREADS_CORRUPT_SUFFIX}.{stamp}"
        )
        try:
            self._path.replace(backup_path)
            backup_value = str(backup_path)
        except OSError:
            backup_value = ""
        notice = {
            "status": "corrupt",
            "message": "Conversation state reset due to corrupted registry.",
            "detail": detail,
            "detected_at": stamp,
            "backup_path": backup_value,
        }
        try:
            atomic_write(self._notice_path(), json.dumps(notice, indent=2) + "\n")
        except Exception:
            LOGGER.warning(
                "Failed to write app server thread corruption notice.",
                exc_info=True,
            )
        try:
            self._save_unlocked({})
        except Exception:
            LOGGER.warning(
                "Failed to reset app server thread registry after corruption.",
                exc_info=True,
            )
