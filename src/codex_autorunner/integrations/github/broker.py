from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import uuid
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
_COOLDOWN_KEY = "cooldown"
_CACHE_KEY_PREFIX = "cache:"
_LEASE_KEY_PREFIX = "lease:"
_RATE_LIMIT_CACHE_TTL_SECONDS = 30
_AUTH_STATUS_CACHE_TTL_SECONDS = 60
_REPO_VIEW_CACHE_TTL_SECONDS = 5 * 60
_PULL_REQUEST_CACHE_TTL_SECONDS = 15
_SCM_READ_CACHE_TTL_SECONDS = 20
_RATE_LIMIT_COOLDOWN_FALLBACK_SECONDS = 5 * 60
_BROKER_SQLITE_BUSY_TIMEOUT_MS = 5_000
_MAX_CACHED_STDOUT_BYTES = 256_000
_LEASE_MIN_TTL_SECONDS = 60
_LEASE_TTL_GRACE_SECONDS = 5
_LEASE_WAIT_POLL_SECONDS = 0.05

Runner = Callable[..., subprocess.CompletedProcess[str]]
ErrorFactory = Callable[[str, int], Exception]


@dataclass(frozen=True)
class _BrokerStateRecord:
    value_json: dict[str, Any]
    updated_at: str


@dataclass(frozen=True)
class _CommandCachePolicy:
    namespace: str
    ttl_seconds: int
    scope: str = "cwd"


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


def _is_auth_status_command(args: list[str]) -> bool:
    return len(args) >= 2 and args[0] == "auth" and args[1] == "status"


def _arg_value(args: list[str], flag: str) -> Optional[str]:
    for index, value in enumerate(args):
        if value != flag:
            continue
        next_index = index + 1
        if next_index >= len(args):
            return None
        return args[next_index]
    return None


def _api_method(args: list[str]) -> str:
    raw = _arg_value(args, "-X") or _arg_value(args, "--method") or "GET"
    return raw.strip().upper()


def _api_endpoint(args: list[str]) -> Optional[str]:
    if not args or args[0] != "api":
        return None
    for value in args[1:]:
        if value.startswith("-"):
            continue
        return value
    return None


def _command_cache_policy(args: list[str]) -> Optional[_CommandCachePolicy]:
    if _is_rate_limit_command(args):
        return _CommandCachePolicy(
            namespace="rate_limit",
            ttl_seconds=_RATE_LIMIT_CACHE_TTL_SECONDS,
            scope="global",
        )
    if _is_auth_status_command(args):
        return _CommandCachePolicy(
            namespace="auth_status",
            ttl_seconds=_AUTH_STATUS_CACHE_TTL_SECONDS,
            scope="global",
        )
    if len(args) >= 2 and args[0] == "repo" and args[1] == "view":
        return _CommandCachePolicy(
            namespace="repo_view",
            ttl_seconds=_REPO_VIEW_CACHE_TTL_SECONDS,
        )
    if len(args) >= 2 and args[0] == "pr" and args[1] in {"view", "list"}:
        return _CommandCachePolicy(
            namespace=f"pr_{args[1]}",
            ttl_seconds=_PULL_REQUEST_CACHE_TTL_SECONDS,
        )
    if len(args) >= 2 and args[0] == "issue" and args[1] == "view":
        return _CommandCachePolicy(
            namespace="issue_view",
            ttl_seconds=_SCM_READ_CACHE_TTL_SECONDS,
        )
    if not args or args[0] != "api" or _api_method(args) != "GET":
        return None
    endpoint = _api_endpoint(args)
    if endpoint is None:
        return None
    if endpoint == "graphql":
        return _CommandCachePolicy(
            namespace="graphql",
            ttl_seconds=_SCM_READ_CACHE_TTL_SECONDS,
        )
    if endpoint.endswith("/reviews"):
        return _CommandCachePolicy(
            namespace="reviews",
            ttl_seconds=_SCM_READ_CACHE_TTL_SECONDS,
        )
    if endpoint.endswith("/comments"):
        return _CommandCachePolicy(
            namespace="comments",
            ttl_seconds=_SCM_READ_CACHE_TTL_SECONDS,
        )
    if "/issues/" in endpoint:
        return _CommandCachePolicy(
            namespace="issues_api",
            ttl_seconds=_SCM_READ_CACHE_TTL_SECONDS,
        )
    return None


def _is_mutating_command(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "api":
        return _api_method(args) in {"POST", "PUT", "PATCH", "DELETE"}
    if args[0] == "pr" and len(args) >= 2:
        return args[1] in {"create", "edit", "merge", "close", "reopen", "review"}
    if args[0] == "issue" and len(args) >= 2:
        return args[1] in {"create", "edit", "close", "reopen"}
    return False


def _bypass_cooldown(args: list[str]) -> bool:
    return _is_rate_limit_command(args) or _is_auth_status_command(args)


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
        cache_policy = _command_cache_policy(args)
        lease_key: Optional[str] = None
        lease_owner = uuid.uuid4().hex
        lease_ttl_seconds = max(
            _LEASE_MIN_TTL_SECONDS,
            timeout_seconds + _LEASE_TTL_GRACE_SECONDS,
        )
        if cache_policy is not None:
            cached = self._cached_response(
                args,
                cache_policy=cache_policy,
                cwd=normalized_cwd,
            )
            if cached is not None:
                return cached
            lease_key = self._lease_key(
                args,
                cache_policy=cache_policy,
                cwd=normalized_cwd,
            )
            if not self._claim_lease(
                lease_key,
                owner=lease_owner,
                ttl_seconds=lease_ttl_seconds,
            ):
                waited = self._wait_for_cache_or_cooldown(
                    args,
                    cache_policy=cache_policy,
                    cwd=normalized_cwd,
                    timeout_seconds=timeout_seconds,
                    traffic_class=traffic_class,
                    lease_key=lease_key,
                    lease_owner=lease_owner,
                    lease_ttl_seconds=lease_ttl_seconds,
                )
                if waited is not None:
                    return waited

        if not _bypass_cooldown(args):
            cooldown_state = self._cooldown_state()
            cooldown_until_raw = cooldown_state.get("cooldown_until")
            cooldown_until = (
                _parse_iso(cooldown_until_raw)
                if isinstance(cooldown_until_raw, str)
                else None
            )
            if (
                cooldown_until is not None
                and cooldown_until > _utc_now()
                and self._should_block_for_cooldown(
                    traffic_class=traffic_class,
                    cooldown_state=cooldown_state,
                )
            ):
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
                return proc
            if proc.returncode != 0:
                return proc

            if cache_policy is not None:
                self._store_cached_response(
                    args,
                    cache_policy=cache_policy,
                    cwd=normalized_cwd,
                    stdout=proc.stdout or "",
                )
            if _is_mutating_command(args):
                self._invalidate_cached_responses()
            return proc
        finally:
            if lease_key is not None:
                self._release_lease(lease_key, owner=lease_owner)

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_cli_broker_leases (
                lease_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
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

    def _delete_states_matching(self, pattern: str) -> None:
        with open_sqlite(
            self._db_path,
            durable=False,
            busy_timeout_ms=_BROKER_SQLITE_BUSY_TIMEOUT_MS,
        ) as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM github_cli_broker_state
                 WHERE state_key LIKE ?
                """,
                (pattern,),
            )

    def _claim_lease(self, lease_key: str, *, owner: str, ttl_seconds: int) -> bool:
        with open_sqlite(
            self._db_path,
            durable=False,
            busy_timeout_ms=_BROKER_SQLITE_BUSY_TIMEOUT_MS,
        ) as conn:
            self._ensure_schema(conn)
            now_timestamp = now_iso()
            conn.execute(
                """
                DELETE FROM github_cli_broker_leases
                 WHERE lease_key = ?
                   AND expires_at <= ?
                """,
                (lease_key, now_timestamp),
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO github_cli_broker_leases (
                    lease_key,
                    owner,
                    expires_at,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    lease_key,
                    owner,
                    _iso_after_seconds(ttl_seconds),
                    now_timestamp,
                ),
            )
            return cursor.rowcount > 0

    def _release_lease(self, lease_key: str, *, owner: str) -> None:
        with open_sqlite(
            self._db_path,
            durable=False,
            busy_timeout_ms=_BROKER_SQLITE_BUSY_TIMEOUT_MS,
        ) as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM github_cli_broker_leases
                 WHERE lease_key = ?
                   AND owner = ?
                """,
                (lease_key, owner),
            )

    def _cache_key(
        self,
        args: list[str],
        *,
        cache_policy: _CommandCachePolicy,
        cwd: Path,
    ) -> str:
        fingerprint = json.dumps(
            {
                "namespace": cache_policy.namespace,
                "gh_path": self._gh_path,
                "scope_id": (
                    "global" if cache_policy.scope == "global" else str(cwd.resolve())
                ),
                "args": args,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"{_CACHE_KEY_PREFIX}{cache_policy.namespace}:{digest}"

    def _lease_key(
        self,
        args: list[str],
        *,
        cache_policy: _CommandCachePolicy,
        cwd: Path,
    ) -> str:
        return (
            f"{_LEASE_KEY_PREFIX}"
            f"{self._cache_key(args, cache_policy=cache_policy, cwd=cwd)}"
        )

    def _cached_response(
        self,
        args: list[str],
        *,
        cache_policy: _CommandCachePolicy,
        cwd: Path,
    ) -> Optional[subprocess.CompletedProcess[str]]:
        state = self._load_state(
            self._cache_key(args, cache_policy=cache_policy, cwd=cwd)
        )
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

    def _store_cached_response(
        self,
        args: list[str],
        *,
        cache_policy: _CommandCachePolicy,
        cwd: Path,
        stdout: str,
    ) -> None:
        if len(stdout.encode("utf-8")) > _MAX_CACHED_STDOUT_BYTES:
            return
        self._write_state(
            self._cache_key(args, cache_policy=cache_policy, cwd=cwd),
            {
                "stdout": stdout,
                "expires_at": _iso_after_seconds(cache_policy.ttl_seconds),
            },
        )
        if cache_policy.namespace != "rate_limit":
            return
        reset_at = _rate_limit_reset_at(stdout)
        if reset_at is None:
            return
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

    def _cooldown_state(self) -> dict[str, Any]:
        state = self._load_state(_COOLDOWN_KEY)
        if state is None:
            return {}
        return state.value_json

    def _wait_for_cache_or_cooldown(
        self,
        args: list[str],
        *,
        cache_policy: _CommandCachePolicy,
        cwd: Path,
        timeout_seconds: int,
        traffic_class: str,
        lease_key: str,
        lease_owner: str,
        lease_ttl_seconds: int,
    ) -> Optional[subprocess.CompletedProcess[str]]:
        _ = timeout_seconds
        while True:
            cached = self._cached_response(
                args,
                cache_policy=cache_policy,
                cwd=cwd,
            )
            if cached is not None:
                return cached
            cooldown_state = self._cooldown_state()
            cooldown_until_raw = cooldown_state.get("cooldown_until")
            if isinstance(cooldown_until_raw, str):
                try:
                    cooldown_until = _parse_iso(cooldown_until_raw)
                except ValueError:
                    cooldown_until = None
                if (
                    cooldown_until is not None
                    and cooldown_until > _utc_now()
                    and self._should_block_for_cooldown(
                        traffic_class=traffic_class,
                        cooldown_state=cooldown_state,
                    )
                ):
                    raise self._error_factory(
                        "GitHub CLI global cooldown active until "
                        f"{cooldown_until.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                        429,
                    )
            if self._claim_lease(
                lease_key,
                owner=lease_owner,
                ttl_seconds=lease_ttl_seconds,
            ):
                return None
            time.sleep(_LEASE_WAIT_POLL_SECONDS)

    def _should_block_for_cooldown(
        self,
        *,
        traffic_class: str,
        cooldown_state: dict[str, Any],
    ) -> bool:
        if traffic_class != "interactive":
            return True
        return not (
            cooldown_state.get("reason") == "rate_limit_hit"
            and cooldown_state.get("traffic_class") == "polling"
        )

    def _invalidate_cached_responses(self) -> None:
        self._delete_states_matching(f"{_CACHE_KEY_PREFIX}%")

    def _record_rate_limit_hit(self, *, traffic_class: str) -> None:
        reset_at: Optional[str] = None
        rate_limit_policy = _command_cache_policy(["api", "rate_limit"])
        if rate_limit_policy is not None:
            cached = self._load_state(
                self._cache_key(
                    ["api", "rate_limit"],
                    cache_policy=rate_limit_policy,
                    cwd=self._repo_root,
                )
            )
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
