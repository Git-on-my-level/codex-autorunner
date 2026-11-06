from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ....core.preview_services.ids import validate_service_id
from ....core.utils import atomic_write

_CAPABILITIES_RELATIVE_PATH = Path(".codex-autorunner/services/capabilities.json")
DEFAULT_PREVIEW_CAPABILITY_TTL_SECONDS = 60 * 60


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreviewCapability:
    token: str
    service_id: str
    path_prefix: str
    expires_at: float

    @property
    def url(self) -> str:
        path = self.path_prefix.strip("/")
        suffix = f"{path}/" if path else ""
        return f"/preview/p/{self.token}/{suffix}"


class PreviewCapabilityStore:
    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._path = self._hub_root / _CAPABILITIES_RELATIVE_PATH
        self._durable = durable
        self._now = now
        self._lock = threading.RLock()
        self._active_tokens: dict[str, PreviewCapability] = {}

    def issue(
        self,
        service_id: str,
        *,
        path_prefix: str = "",
        ttl_seconds: int = DEFAULT_PREVIEW_CAPABILITY_TTL_SECONDS,
    ) -> PreviewCapability:
        clean_service_id = validate_service_id(service_id)
        ttl = max(1, int(ttl_seconds))
        clean_path = _clean_path_prefix(path_prefix)
        token = secrets.token_urlsafe(32)
        expires_at = self._now() + ttl
        entry = {
            "token_hash": _sha256(token),
            "service_id": clean_service_id,
            "path_prefix": clean_path,
            "issued_at": self._now(),
            "expires_at": expires_at,
            "revoked": False,
        }
        capability = PreviewCapability(
            token=token,
            service_id=clean_service_id,
            path_prefix=clean_path,
            expires_at=expires_at,
        )
        with self._lock:
            data = self._read_store()
            entries = _active_entries(data, now=self._now())
            entries.append(entry)
            data["tokens"] = entries
            self._write_store(data)
            self._active_tokens[clean_service_id] = capability
        return capability

    def get_active(self, service_id: str) -> PreviewCapability | None:
        clean_service_id = validate_service_id(service_id)
        with self._lock:
            candidate = self._active_tokens.get(clean_service_id)
            if candidate is None:
                return None
            if candidate.expires_at <= self._now():
                self._active_tokens.pop(clean_service_id, None)
                return None
            if self.validate(candidate.token) is None:
                self._active_tokens.pop(clean_service_id, None)
                return None
            return candidate

    def get_or_issue(
        self,
        service_id: str,
        *,
        path_prefix: str = "",
        ttl_seconds: int | None = None,
    ) -> PreviewCapability:
        """Return the active capability for ``service_id``, or issue a new one.

        An active capability is reused when the caller did not request a
        specific ``ttl_seconds``. When a TTL is requested explicitly, the
        active capability is only reused if it lives at least that long;
        otherwise a fresh capability with the requested TTL is issued.
        """
        clean_path = _clean_path_prefix(path_prefix)
        active = self.get_active(service_id)
        if active is not None and active.path_prefix == clean_path:
            if ttl_seconds is None:
                return active
            if ttl_seconds <= active.expires_at - self._now():
                return active
        return self.issue(
            service_id,
            path_prefix=clean_path,
            ttl_seconds=(
                DEFAULT_PREVIEW_CAPABILITY_TTL_SECONDS
                if ttl_seconds is None
                else ttl_seconds
            ),
        )

    def validate(self, token: str) -> PreviewCapability | None:
        candidate = token.strip()
        if not candidate:
            return None
        token_hash = _sha256(candidate)
        with self._lock:
            data = self._read_store()
            entries = _active_entries(data, now=self._now())
            changed = len(entries) != len(data.get("tokens") or [])
            matched: dict[str, Any] | None = None
            for entry in entries:
                stored_hash = entry.get("token_hash")
                if isinstance(stored_hash, str) and hmac.compare_digest(
                    stored_hash, token_hash
                ):
                    matched = entry
                    break
            if changed:
                data["tokens"] = entries
                self._write_store(data)
            if matched is None:
                return None
            return PreviewCapability(
                token=candidate,
                service_id=str(matched["service_id"]),
                path_prefix=str(matched.get("path_prefix") or ""),
                expires_at=float(matched["expires_at"]),
            )

    def revoke_service(self, service_id: str) -> int:
        clean_service_id = validate_service_id(service_id)
        with self._lock:
            self._active_tokens.pop(clean_service_id, None)
            data = self._read_store()
            entries = data.get("tokens")
            if not isinstance(entries, list):
                return 0
            revoked = 0
            for entry in entries:
                if (
                    isinstance(entry, dict)
                    and entry.get("service_id") == clean_service_id
                    and not entry.get("revoked")
                ):
                    entry["revoked"] = True
                    revoked += 1
            if revoked:
                self._write_store(data)
            return revoked

    def _read_store(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"tokens": []}
        if not isinstance(data, dict):
            return {"tokens": []}
        if not isinstance(data.get("tokens"), list):
            data["tokens"] = []
        return data

    def _write_store(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.parent.chmod(0o700)
        except OSError:
            pass
        atomic_write(
            self._path,
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            durable=self._durable,
        )
        try:
            self._path.chmod(0o600)
        except OSError:
            pass


def _active_entries(data: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
    entries = data.get("tokens")
    if not isinstance(entries, list):
        return []
    active: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("revoked"):
            continue
        service_id = entry.get("service_id")
        expires_at = entry.get("expires_at")
        token_hash = entry.get("token_hash")
        if (
            isinstance(service_id, str)
            and isinstance(token_hash, str)
            and isinstance(expires_at, (int, float))
            and float(expires_at) > now
        ):
            active.append(entry)
    return active


def _clean_path_prefix(path_prefix: str) -> str:
    clean = path_prefix.strip("/")
    if not clean:
        return ""
    parts = [part for part in clean.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("preview capability path prefix must not traverse")
    return "/".join(parts)
