from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .locks import file_lock
from .utils import atomic_write

APP_SERVER_THREADS_FILENAME = ".codex-autorunner/app_server_threads.json"
APP_SERVER_THREADS_VERSION = 1
APP_SERVER_THREADS_CORRUPT_SUFFIX = ".corrupt"
APP_SERVER_THREADS_NOTICE_SUFFIX = ".corrupt.json"
FILE_CHAT_KEY = "file_chat"
FILE_CHAT_OPENCODE_KEY = "file_chat.opencode"
FILE_CHAT_PREFIX = "file_chat."
FILE_CHAT_OPENCODE_PREFIX = "file_chat.opencode."

# Static keys that can be reset/managed via the UI.
FEATURE_KEYS = {
    FILE_CHAT_KEY,
    FILE_CHAT_OPENCODE_KEY,
    "autorunner",
    "autorunner.opencode",
}


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
            pass
        try:
            self._save_unlocked({})
        except Exception:
            pass
