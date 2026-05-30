from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Request

from ....core.config_types import BrowserAuthCookieSecure

BOOTSTRAP_TOKEN_RELATIVE_PATH = Path(".codex-autorunner/bootstrap-token")
BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH = Path(".codex-autorunner/bootstrap-token.json")
SESSION_STORE_RELATIVE_PATH = Path(".codex-autorunner/browser-sessions.json")
SESSION_COOKIE_NAME = "car_session"
BOOTSTRAP_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_private_text(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ""
    if not stat.S_ISREG(mode):
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def ensure_bootstrap_token(root: Path, now: Optional[float] = None) -> tuple[Path, str]:
    path = root / BOOTSTRAP_TOKEN_RELATIVE_PATH
    metadata_path = root / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH
    observed_now = time.time() if now is None else now
    existing = _read_private_text(path).strip()
    if existing:
        issued_at = _read_bootstrap_issued_at(metadata_path)
        if (
            issued_at is not None
            and issued_at <= observed_now + 60
            and issued_at + BOOTSTRAP_TOKEN_MAX_AGE_SECONDS > observed_now
        ):
            try:
                if not path.is_symlink():
                    os.chmod(path, 0o600)
            except OSError:
                pass
            return path, existing
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            metadata_path.unlink()
        except OSError:
            pass
    token = secrets.token_urlsafe(48)
    _write_private_text(path, f"{token}\n")
    _write_bootstrap_metadata(metadata_path, issued_at=observed_now)
    return path, token


def _read_bootstrap_issued_at(path: Path) -> Optional[float]:
    try:
        data = json.loads(_read_private_text(path))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    issued_at = data.get("issued_at")
    if not isinstance(issued_at, (int, float)):
        return None
    value = float(issued_at)
    return value if math.isfinite(value) else None


def _write_bootstrap_metadata(path: Path, *, issued_at: float) -> None:
    _write_private_text(
        path,
        json.dumps(
            {
                "issued_at": issued_at,
                "expires_at": issued_at + BOOTSTRAP_TOKEN_MAX_AGE_SECONDS,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


@dataclass(frozen=True)
class BootstrapClaim:
    session_token: str
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS


def resolve_cookie_secure(
    request: Request, cookie_secure: BrowserAuthCookieSecure
) -> bool:
    if cookie_secure == "auto":
        return request.url.scheme == "https"
    return bool(cookie_secure)


class BrowserAuthStore:
    def __init__(self, root: Path, now: Callable[[], float] = time.time):
        self.root = root
        self.bootstrap_token_path = root / BOOTSTRAP_TOKEN_RELATIVE_PATH
        self.session_store_path = root / SESSION_STORE_RELATIVE_PATH
        self._now = now
        self._lock = threading.Lock()

    def ensure_bootstrap_token(self) -> tuple[Path, str]:
        return ensure_bootstrap_token(self.root, now=self._now())

    def claim_bootstrap_token(self, token: str) -> Optional[BootstrapClaim]:
        candidate = token.strip()
        if not candidate:
            return None
        with self._lock:
            expected = _read_private_text(self.bootstrap_token_path).strip()
            if not expected:
                return None
            issued_at = _read_bootstrap_issued_at(
                self.root / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH
            )
            now = self._now()
            if (
                issued_at is None
                or issued_at > now + 60
                or issued_at + BOOTSTRAP_TOKEN_MAX_AGE_SECONDS <= now
            ):
                return None
            if not hmac.compare_digest(candidate, expected):
                return None

            session_token = secrets.token_urlsafe(48)
            self._add_session(session_token)
            try:
                self.bootstrap_token_path.unlink()
            except FileNotFoundError:
                pass
            try:
                (self.root / BOOTSTRAP_TOKEN_METADATA_RELATIVE_PATH).unlink()
            except FileNotFoundError:
                pass
            return BootstrapClaim(session_token=session_token)

    def validate_session_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        token_hash = _sha256(token.strip())
        if not token_hash:
            return False
        with self._lock:
            data = self._read_store()
            sessions = data.get("sessions")
            if not isinstance(sessions, list):
                return False
            now = self._now()
            valid = False
            active_sessions = []
            changed = False
            for entry in sessions:
                if not isinstance(entry, dict):
                    changed = True
                    continue
                stored_hash = entry.get("token_hash")
                expires_at = entry.get("expires_at")
                if not isinstance(stored_hash, str) or not isinstance(
                    expires_at, (int, float)
                ):
                    changed = True
                    continue
                if expires_at <= now:
                    changed = True
                    continue
                active_sessions.append(entry)
                if hmac.compare_digest(stored_hash, token_hash):
                    valid = True
            if changed:
                data["sessions"] = active_sessions
                self._write_store(data)
            return valid

    def revoke_session_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        token_hash = _sha256(token.strip())
        with self._lock:
            data = self._read_store()
            sessions = data.get("sessions")
            if not isinstance(sessions, list):
                return False
            retained = [
                entry
                for entry in sessions
                if not (
                    isinstance(entry, dict)
                    and hmac.compare_digest(
                        str(entry.get("token_hash", "")), token_hash
                    )
                )
            ]
            if len(retained) == len(sessions):
                return False
            data["sessions"] = retained
            self._write_store(data)
            return True

    def revoke_all_sessions(self) -> int:
        with self._lock:
            data = self._read_store()
            sessions = data.get("sessions")
            count = len(sessions) if isinstance(sessions, list) else 0
            data["sessions"] = []
            self._write_store(data)
            return count

    def _add_session(self, token: str) -> None:
        data = self._read_store()
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        token_hash = _sha256(token)
        now = self._now()
        active_sessions = []
        for entry in sessions:
            if not isinstance(entry, dict):
                continue
            stored_hash = entry.get("token_hash")
            expires_at = entry.get("expires_at")
            if not isinstance(stored_hash, str) or not isinstance(
                expires_at, (int, float)
            ):
                continue
            if expires_at <= now or stored_hash == token_hash:
                continue
            active_sessions.append(entry)
        active_sessions.append(
            {
                "token_hash": token_hash,
                "issued_at": now,
                "expires_at": now + SESSION_MAX_AGE_SECONDS,
            }
        )
        data["sessions"] = active_sessions
        self._write_store(data)

    def _read_store(self) -> dict[str, Any]:
        try:
            data = json.loads(_read_private_text(self.session_store_path))
        except json.JSONDecodeError:
            return {"sessions": []}
        return data if isinstance(data, dict) else {"sessions": []}

    def _write_store(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, sort_keys=True, indent=2)
        _write_private_text(self.session_store_path, f"{text}\n")
