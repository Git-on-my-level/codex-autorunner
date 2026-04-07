from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from ...core.sqlite_utils import open_sqlite
from ...core.state_roots import resolve_global_state_root
from ...core.text_utils import _mapping
from ...core.time_utils import now_iso

_LOGGER = logging.getLogger(__name__)

_BROKER_DB_FILENAME = "github-cli.sqlite3"
_RATE_LIMIT_CACHE_KEY = "rate_limit_cache"
_COOLDOWN_KEY = "cooldown"
_RATE_LIMIT_CACHE_TTL_SECONDS = 30
_RATE_LIMIT_COOLDOWN_FALLBACK_SECONDS = 5 * 60
_BROKER_SQLITE_BUSY_TIMEOUT_MS = 5_000

Runner = Callable[..., subprocess.CompletedProcess[str]]
ErrorFactory = Callable[[str, int], Exception]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_after_seconds(seconds: int) -> str:
    return (_utc_now() + timedelta(seconds=max(0, int(seconds)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _normalize_positive_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _normalize_non_negative_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _looks_like_rate_limit(detail: str) -> bool:
    normalized = (detail or "").strip().lower()
    return bool(normalized) and "rate limit" in normalized


def _rate_limit_reset_at(payload_text: str) -> Optional[str]:
    try:
        payload = json.loads(payload_text or "{}")
    except json.JSONDecodeError:
        return None
    resources = _mapping(_mapping(payload).get("resources"))
    reset_epochs: list[int] = []
    for resource_name in ("graphql", "core"):
        entry = _mapping(resources.get(resource_name))
        remaining = _normalize_non_negative_int(entry.get("remaining"))
        reset_epoch = _normalize_positive_int(entry.get("reset"))
        if remaining == 0 and reset_epoch is not None:
            reset_epochs.append(reset_epoch)
    if not reset_epochs:
        return None
    return datetime.fromtimestamp(min(reset_epochs), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _is_rate_limit_command(args: list[str]) -> bool:
    return len(args) >= 2 and args[0] == "api" and args[1] == "rate_limit"


def _bypass_cooldown(args: list[str]) -> bool:
    if _is_rate_limit_command(args):
        return True
    return len(args) >= 2 and args[0] == "auth" and args[1] == "status"


@dataclass(frozen=True)
class _BrokerStateRecord:
    value_json: dict[str, Any]
    updated_at: str


class GitHubCliBroker:
    def __init__(
        self,
        *,
        repo_root: Path,
        raw_config: Optional[dict[str, Any]],
        gh_path: str,
        runner: Runner,
        error_factory: ErrorFactory,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._raw_config = raw_config or {}
        self._gh_path = gh_path
        self._runner = runner
        self._error_factory = error_factory
        config_ns = SimpleNamespace(root=self._repo_root, raw=self._raw_config)
        self._state_root = resolve_global_state_root(
            config=config_ns,
            repo_root=self._repo_root,
        )
        self._db_path = self._state_root / "github" / _BROKER_DB_FILENAME

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        check: bool,
        traffic_class: str = "interactive",
    ) -> subprocess.CompletedProcess[str]:
        normalized_cwd = Path(cwd)
        if _is_rate_limit_command(args):
            cached = self._cached_rate_limit_response(args)
            if cached is not None:
                return cached

        if not _bypass_cooldown(args):
            cooldown_until = self._cooldown_until()
            if cooldown_until is not None and cooldown_until > _utc_now():
                detail = (
                    "GitHub CLI global cooldown active until "
                    f"{cooldown_until.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
                _LOGGER.info(
                    "GitHub CLI broker blocked command during cooldown: class=%s args=%s",
                    traffic_class,
                    args[:2],
                )
                if check:
                    raise self._error_factory(detail, 429)
                return subprocess.CompletedProcess(
                    [self._gh_path] + args,
                    1,
                    stdout="",
                    stderr=detail,
                )

        try:
            proc = self._runner(
                [self._gh_path] + args,
                cwd=normalized_cwd,
                timeout_seconds=timeout_seconds,
                check=check,
            )
        except Exception as exc:
            if _looks_like_rate_limit(str(exc)):
                self._record_rate_limit_hit(traffic_class=traffic_class)
            raise

        if proc.returncode != 0 and _looks_like_rate_limit(
            (proc.stderr or "").strip() or (proc.stdout or "").strip()
        ):
            self._record_rate_limit_hit(traffic_class=traffic_class)
        elif _is_rate_limit_command(args) and proc.returncode == 0:
            self._store_rate_limit_cache(proc.stdout or "")
        return proc

    def _ensure_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_cli_broker_state (
                state_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _load_state(self, key: str) -> Optional[_BrokerStateRecord]:
        with open_sqlite(
            self._db_path,
            durable=False,
            busy_timeout_ms=_BROKER_SQLITE_BUSY_TIMEOUT_MS,
        ) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                """
                SELECT value_json, updated_at
                  FROM github_cli_broker_state
                 WHERE state_key = ?
                """,
                (key,),
            ).fetchone()
            if row is None:
                return None
            try:
                value_json = json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                return None
            return _BrokerStateRecord(
                value_json=value_json if isinstance(value_json, dict) else {},
                updated_at=str(row["updated_at"]),
            )

    def _write_state(self, key: str, value_json: dict[str, Any]) -> None:
        with open_sqlite(
            self._db_path,
            durable=False,
            busy_timeout_ms=_BROKER_SQLITE_BUSY_TIMEOUT_MS,
        ) as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO github_cli_broker_state (state_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value_json, sort_keys=True), now_iso()),
            )

    def _cached_rate_limit_response(
        self, args: list[str]
    ) -> Optional[subprocess.CompletedProcess[str]]:
        state = self._load_state(_RATE_LIMIT_CACHE_KEY)
        if state is None:
            return None
        expires_at = state.value_json.get("expires_at")
        if not isinstance(expires_at, str):
            return None
        try:
            if _parse_iso(expires_at) <= _utc_now():
                return None
        except ValueError:
            return None
        stdout = state.value_json.get("stdout")
        if not isinstance(stdout, str):
            return None
        return subprocess.CompletedProcess(
            [self._gh_path] + args,
            0,
            stdout=stdout,
            stderr="",
        )

    def _store_rate_limit_cache(self, stdout: str) -> None:
        payload = {
            "stdout": stdout,
            "expires_at": _iso_after_seconds(_RATE_LIMIT_CACHE_TTL_SECONDS),
        }
        self._write_state(_RATE_LIMIT_CACHE_KEY, payload)
        reset_at = _rate_limit_reset_at(stdout)
        if reset_at is not None:
            try:
                if _parse_iso(reset_at) > _utc_now():
                    self._write_state(
                        _COOLDOWN_KEY,
                        {
                            "cooldown_until": reset_at,
                            "reason": "rate_limit_payload_exhausted",
                        },
                    )
            except ValueError:
                pass

    def _cooldown_until(self) -> Optional[datetime]:
        state = self._load_state(_COOLDOWN_KEY)
        if state is None:
            return None
        cooldown_until = state.value_json.get("cooldown_until")
        if not isinstance(cooldown_until, str):
            return None
        try:
            return _parse_iso(cooldown_until)
        except ValueError:
            return None

    def _record_rate_limit_hit(self, *, traffic_class: str) -> None:
        reset_at: Optional[str] = None
        cached = self._load_state(_RATE_LIMIT_CACHE_KEY)
        if cached is not None:
            stdout = cached.value_json.get("stdout")
            if isinstance(stdout, str):
                reset_at = _rate_limit_reset_at(stdout)
        if reset_at is None:
            reset_at = _iso_after_seconds(_RATE_LIMIT_COOLDOWN_FALLBACK_SECONDS)
        self._write_state(
            _COOLDOWN_KEY,
            {
                "cooldown_until": reset_at,
                "reason": "rate_limit_hit",
                "traffic_class": traffic_class,
            },
        )


__all__ = ["GitHubCliBroker"]
