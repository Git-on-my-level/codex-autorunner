from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

BOOTSTRAP_TOKEN_RELATIVE_PATH = Path(".codex-autorunner/bootstrap-token")
SESSION_STORE_RELATIVE_PATH = Path(".codex-autorunner/browser-sessions.json")
SESSION_COOKIE_NAME = "car_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def ensure_bootstrap_token(root: Path) -> tuple[Path, str]:
    path = root / BOOTSTRAP_TOKEN_RELATIVE_PATH
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path, existing
    token = secrets.token_urlsafe(48)
    _write_private_text(path, f"{token}\n")
    return path, token


@dataclass(frozen=True)
class BootstrapClaim:
    session_token: str
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS


class BrowserAuthStore:
    def __init__(self, root: Path):
        self.root = root
        self.bootstrap_token_path = root / BOOTSTRAP_TOKEN_RELATIVE_PATH
        self.session_store_path = root / SESSION_STORE_RELATIVE_PATH
        self._lock = threading.Lock()

    def ensure_bootstrap_token(self) -> tuple[Path, str]:
        return ensure_bootstrap_token(self.root)

    def has_bootstrap_token(self) -> bool:
        try:
            return bool(self.bootstrap_token_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            return False

    def claim_bootstrap_token(self, token: str) -> Optional[BootstrapClaim]:
        candidate = token.strip()
        if not candidate:
            return None
        with self._lock:
            try:
                expected = self.bootstrap_token_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return None
            if not expected or not hmac.compare_digest(candidate, expected):
                return None

            session_token = secrets.token_urlsafe(48)
            self._add_session(session_token)
            try:
                self.bootstrap_token_path.unlink()
            except FileNotFoundError:
                pass
            return BootstrapClaim(session_token=session_token)

    def validate_session_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        token_hash = _sha256(token.strip())
        if not token_hash:
            return False
        data = self._read_store()
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return False
        return any(
            isinstance(entry, str) and hmac.compare_digest(entry, token_hash)
            for entry in sessions
        )

    def _add_session(self, token: str) -> None:
        data = self._read_store()
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        token_hash = _sha256(token)
        if token_hash not in sessions:
            sessions.append(token_hash)
        data["sessions"] = sessions
        self._write_store(data)

    def _read_store(self) -> dict[str, Any]:
        try:
            data = json.loads(self.session_store_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"sessions": []}
        return data if isinstance(data, dict) else {"sessions": []}

    def _write_store(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, sort_keys=True, indent=2)
        _write_private_text(self.session_store_path, f"{text}\n")
