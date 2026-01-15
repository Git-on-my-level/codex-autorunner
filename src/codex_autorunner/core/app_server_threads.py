from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .state import state_lock
from .utils import atomic_write, read_json

APP_SERVER_THREADS_FILENAME = ".codex-autorunner/app_server_threads.json"
APP_SERVER_THREADS_VERSION = 1
DOC_CHAT_KINDS = ("todo", "progress", "opinions", "spec", "summary")
DOC_CHAT_PREFIX = "doc_chat."
DOC_CHAT_KEYS = {f"{DOC_CHAT_PREFIX}{kind}" for kind in DOC_CHAT_KINDS}
FEATURE_KEYS = DOC_CHAT_KEYS | {"spec_ingest", "autorunner"}


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
    raise ValueError(f"invalid feature key: {raw}")


class AppServerThreadRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str]:
        with state_lock(self._path):
            return self._load_unlocked()

    def feature_map(self) -> dict[str, object]:
        threads = self.load()
        return {
            "doc_chat": {
                kind: threads.get(f"{DOC_CHAT_PREFIX}{kind}") for kind in DOC_CHAT_KINDS
            },
            "spec_ingest": threads.get("spec_ingest"),
            "autorunner": threads.get("autorunner"),
        }

    def get_thread_id(self, key: str) -> Optional[str]:
        normalized = normalize_feature_key(key)
        with state_lock(self._path):
            threads = self._load_unlocked()
            return threads.get(normalized)

    def set_thread_id(self, key: str, thread_id: str) -> None:
        normalized = normalize_feature_key(key)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread id is required")
        with state_lock(self._path):
            threads = self._load_unlocked()
            threads[normalized] = thread_id
            self._save_unlocked(threads)

    def reset_thread(self, key: str) -> bool:
        normalized = normalize_feature_key(key)
        with state_lock(self._path):
            threads = self._load_unlocked()
            if normalized not in threads:
                return False
            threads.pop(normalized, None)
            self._save_unlocked(threads)
            return True

    def _load_unlocked(self) -> dict[str, str]:
        data = read_json(self._path)
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
