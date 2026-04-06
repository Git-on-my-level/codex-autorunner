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
from ...core.text_utils import lock_path_for
from ...core.utils import atomic_write

APP_SERVER_THREADS_FILENAME = ".codex-autorunner/app_server_threads.json"
APP_SERVER_THREADS_VERSION = 1
APP_SERVER_THREADS_CORRUPT_SUFFIX = ".corrupt"
APP_SERVER_THREADS_NOTICE_SUFFIX = ".corrupt.json"
FILE_CHAT_KEY = "file_chat"
FILE_CHAT_OPENCODE_KEY = "file_chat.opencode"
FILE_CHAT_HERMES_KEY = "file_chat.hermes"
FILE_CHAT_PREFIX = "file_chat."
FILE_CHAT_OPENCODE_PREFIX = "file_chat.opencode."
FILE_CHAT_HERMES_PREFIX = "file_chat.hermes."
PMA_KEY = "pma"
PMA_OPENCODE_KEY = "pma.opencode"
PMA_HERMES_KEY = "pma.hermes"
PMA_PREFIX = "pma."
PMA_OPENCODE_PREFIX = "pma.opencode."
PMA_HERMES_PREFIX = "pma.hermes."

LOGGER = logging.getLogger("codex_autorunner.app_server")

FEATURE_KEYS = {
    FILE_CHAT_KEY,
    FILE_CHAT_OPENCODE_KEY,
    FILE_CHAT_HERMES_KEY,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    PMA_HERMES_KEY,
    "autorunner",
    "autorunner.opencode",
    "autorunner.hermes",
}


def _normalize_pma_agent_family(agent: Optional[str]) -> Optional[str]:
    if not isinstance(agent, str):
        return None
    normalized = agent.strip().lower()
    if not normalized or normalized in {"all", "codex"}:
        return None
    return normalized


def _normalize_profile_segment(profile: Optional[str]) -> Optional[str]:
    if not isinstance(profile, str):
        return None
    normalized = profile.strip().lower()
    return normalized or None


def _append_profile_suffix(base_key: str, profile: Optional[str]) -> str:
    normalized_profile = _normalize_profile_segment(profile)
    if normalized_profile is None:
        return base_key
    return f"{base_key}.profile.{normalized_profile}"


def pma_base_key(agent: str, profile: Optional[str] = None) -> str:
    """
    Return the base PMA registry key for the given agent.

    Args:
        agent: Agent identifier ("opencode" or "codex"/other).

    Returns:
        PMA_OPENCODE_KEY if agent is "opencode", otherwise PMA_KEY.
    """
    normalized = _normalize_pma_agent_family(agent)
    base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
    return _append_profile_suffix(base_key, profile)


def pma_legacy_alias_keys(agent: str, profile: Optional[str]) -> tuple[str, ...]:
    """Return possible pre-normalization PMA base keys for alias-style Hermes configs.

    Before PMA key normalization, the runtime alias id was used as the PMA
    segment (for example ``pma.hermes-m4-pma`` or ``pma.hermes_m4_pma``) instead
    of the logical shape ``pma.hermes.profile.m4-pma``.  This returns every
    distinct legacy *base* key (no topic suffix) that might exist in old
    registries, so callers can try fallbacks in order.
    """
    if not isinstance(agent, str) or not isinstance(profile, str):
        return ()
    agent_norm = agent.strip().lower()
    prof_norm = profile.strip().lower()
    if not agent_norm or not prof_norm:
        return ()
    new_key = pma_base_key(agent_norm, prof_norm)
    raw_aliases: set[str] = {
        f"{agent_norm}-{prof_norm}",
        f"{agent_norm}_{prof_norm}",
    }
    unders = prof_norm.replace("-", "_")
    if unders != prof_norm:
        raw_aliases.add(f"{agent_norm}_{unders}")
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in sorted(raw_aliases):
        candidate = pma_base_key(raw)
        if candidate != new_key and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def pma_legacy_alias_key(agent: str, profile: Optional[str]) -> Optional[str]:
    """Return the first legacy alias-style PMA base key, or None if none apply."""
    keys = pma_legacy_alias_keys(agent, profile)
    return keys[0] if keys else None


def pma_legacy_migration_fallback_keys(
    canonical_key: str,
    agent: str,
    profile: Optional[str],
) -> tuple[str, ...]:
    """Map a canonical PMA registry key to legacy keys to try (including topic suffix).

    When ``require_topics`` is enabled, canonical keys look like
    ``pma.hermes.profile.m4-pma.<topic>`` while legacy keys used
    ``pma.hermes-m4-pma.<topic>`` (or underscore alias variants).  This
    mirrors the topic segment onto each legacy base so migration lookups
    succeed for per-topic PMA state.
    """
    logical_base = pma_base_key(agent, profile)
    legacy_bases = pma_legacy_alias_keys(agent, profile)
    if not legacy_bases:
        return ()
    if canonical_key == logical_base:
        return legacy_bases
    topic_prefix = logical_base + "."
    if canonical_key.startswith(topic_prefix):
        suffix = canonical_key[len(topic_prefix) :]
        return tuple(f"{base}.{suffix}" for base in legacy_bases)
    return ()


def pma_prefix_for_agent(agent: Optional[str], profile: Optional[str] = None) -> str:
    """
    Return the PMA registry key prefix for the given agent.

    This prefix matches both the base key and any topic-scoped variants.

    Args:
        agent: Agent identifier ("opencode" or "codex"/other/None).

    Returns:
        PMA_OPENCODE_PREFIX if agent is "opencode", otherwise PMA_PREFIX.
    """
    normalized = _normalize_pma_agent_family(agent)
    base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
    return f"{_append_profile_suffix(base_key, profile)}."


def pma_prefixes_for_reset(
    agent: Optional[str], profile: Optional[str] = None
) -> list[str]:
    """
    Return the list of PMA registry key prefixes to reset for a given agent.

    When resetting PMA state, we need to clear:
    - Global keys (pma, pma.opencode)
    - Topic-scoped keys (pma.*, pma.opencode.*)

    Args:
        agent: Agent identifier ("opencode", "codex", "all", or None).

    Returns:
        List of prefixes to reset.
    """
    normalized = _normalize_pma_agent_family(agent)
    normalized_profile = _normalize_profile_segment(profile)
    if normalized_profile is not None:
        base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
        return [f"{_append_profile_suffix(base_key, normalized_profile)}."]
    if normalized is None:
        if agent in {None, "", "all", "codex"}:
            return [PMA_PREFIX]
        return [PMA_PREFIX]
    return [f"{PMA_KEY}.{normalized}."]


def pma_topic_scoped_key(
    agent: str,
    chat_id: int,
    thread_id: Optional[int],
    topic_key_fn=None,
    profile: Optional[str] = None,
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
    base = pma_base_key(agent, profile)
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
        agent: Agent identifier ("opencode", "hermes", "codex"/other).
        channel_id: Discord channel ID.
        workspace_path: Absolute workspace path (hashed for key stability).

    Returns:
        Registry key like "file_chat.discord.{channel}.{digest}" or
        "file_chat.{agent}.discord.{channel}.{digest}".
    """
    normalized_agent = (agent or "").strip().lower()
    if normalized_agent in ("codex", ""):
        prefix = FILE_CHAT_PREFIX
    elif normalized_agent == "opencode":
        prefix = FILE_CHAT_OPENCODE_PREFIX
    elif normalized_agent == "hermes":
        prefix = FILE_CHAT_HERMES_PREFIX
    else:
        prefix = f"{FILE_CHAT_PREFIX}{normalized_agent}."
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
    for prefix in (
        FILE_CHAT_PREFIX,
        FILE_CHAT_OPENCODE_PREFIX,
        FILE_CHAT_HERMES_PREFIX,
    ):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    for prefix in (PMA_PREFIX, PMA_OPENCODE_PREFIX, PMA_HERMES_PREFIX):
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
        return lock_path_for(self._path)

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
        except (OSError, json.JSONDecodeError, ValueError):
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
            "file_chat_hermes": threads.get(FILE_CHAT_HERMES_KEY),
            "autorunner": threads.get("autorunner"),
            "autorunner_opencode": threads.get("autorunner.opencode"),
            "autorunner_hermes": threads.get("autorunner.hermes"),
            "pma": threads.get(PMA_KEY),
            "pma_opencode": threads.get(PMA_OPENCODE_KEY),
            "pma_hermes": threads.get(PMA_HERMES_KEY),
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

    def get_thread_id_with_fallback(
        self, key: str, *fallback_keys: str, migrate: bool = True
    ) -> Optional[str]:
        """Look up ``key``; on miss try ``fallback_keys`` and optionally migrate."""
        normalized = normalize_feature_key(key)
        with file_lock(self._lock_path()):
            threads = self._load_unlocked()
            value = threads.get(normalized)
            if value is not None:
                return value
            for fk in fallback_keys:
                norm_fk = normalize_feature_key(fk)
                value = threads.get(norm_fk)
                if value is not None:
                    if migrate:
                        threads[normalized] = value
                        threads.pop(norm_fk, None)
                        self._save_unlocked(threads)
                    return value
        return None

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

    def reset_threads_by_prefix(
        self, prefix: str, *, exclude_prefixes: tuple[str, ...] = ()
    ) -> list[str]:
        """
        Reset all threads whose keys start with the given prefix.

        Used by PMA lifecycle to clear both global and topic-scoped PMA keys.

        Args:
            prefix: Key prefix to match (e.g., "pma." or "pma.opencode.")
            exclude_prefixes: Optional prefixes to skip even if they start with
                ``prefix``. Used to keep nested families like ``pma.opencode.``
                from being cleared by the broader ``pma.`` reset path.

        Returns:
            List of keys that were cleared.
        """
        cleared_keys = []
        with file_lock(self._lock_path()):
            threads = self._load_unlocked()
            keys_to_remove = [
                key
                for key in threads
                if key.startswith(prefix)
                and not any(
                    key == excluded.rstrip(".") or key.startswith(excluded)
                    for excluded in exclude_prefixes
                )
            ]
            for key in keys_to_remove:
                threads.pop(key, None)
                cleared_keys.append(key)
            if cleared_keys:
                self._save_unlocked(threads)
        return cleared_keys

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
        except OSError:
            LOGGER.warning(
                "Failed to write app server thread corruption notice.",
                exc_info=True,
            )
        try:
            self._save_unlocked({})
        except OSError:
            LOGGER.warning(
                "Failed to reset app server thread registry after corruption.",
                exc_info=True,
            )
