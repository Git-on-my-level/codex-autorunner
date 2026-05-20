from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import (
    _json_dumps,
    _json_loads_object,
    _normalize_limit,
    _normalize_text,
)
from .time_utils import now_iso


class ScmPollingWatchLifecycleState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"
    ERROR = "error"


class ScmPollingWatchLifecycleTransition(str, Enum):
    CREATE = "create"
    REACTIVATE = "reactivate"
    CLOSE = "close"
    EXPIRE = "expire"
    ERROR = "error"
    REFRESH = "refresh"
    ADMIT_DUE = "admit_due"
    CLAIM_DUE = "claim_due"


@dataclass(frozen=True)
class ScmPollingWatchLifecyclePlan:
    transition: ScmPollingWatchLifecycleTransition
    previous_state: ScmPollingWatchLifecycleState | None
    state: ScmPollingWatchLifecycleState


_SCM_POLLING_WATCH_TRANSITIONS: dict[
    ScmPollingWatchLifecycleTransition,
    dict[ScmPollingWatchLifecycleState | None, ScmPollingWatchLifecycleState],
] = {
    ScmPollingWatchLifecycleTransition.CREATE: {
        None: ScmPollingWatchLifecycleState.ACTIVE,
    },
    ScmPollingWatchLifecycleTransition.REACTIVATE: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.ACTIVE,
        ScmPollingWatchLifecycleState.CLOSED: ScmPollingWatchLifecycleState.ACTIVE,
    },
    ScmPollingWatchLifecycleTransition.CLOSE: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.CLOSED,
        ScmPollingWatchLifecycleState.CLOSED: ScmPollingWatchLifecycleState.CLOSED,
    },
    ScmPollingWatchLifecycleTransition.EXPIRE: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.EXPIRED,
        ScmPollingWatchLifecycleState.EXPIRED: ScmPollingWatchLifecycleState.EXPIRED,
    },
    ScmPollingWatchLifecycleTransition.ERROR: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.ERROR,
        ScmPollingWatchLifecycleState.ERROR: ScmPollingWatchLifecycleState.ERROR,
    },
    ScmPollingWatchLifecycleTransition.REFRESH: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.ACTIVE,
        ScmPollingWatchLifecycleState.CLOSED: ScmPollingWatchLifecycleState.CLOSED,
        ScmPollingWatchLifecycleState.EXPIRED: ScmPollingWatchLifecycleState.EXPIRED,
        ScmPollingWatchLifecycleState.ERROR: ScmPollingWatchLifecycleState.ERROR,
    },
    ScmPollingWatchLifecycleTransition.ADMIT_DUE: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.ACTIVE,
    },
    ScmPollingWatchLifecycleTransition.CLAIM_DUE: {
        ScmPollingWatchLifecycleState.ACTIVE: ScmPollingWatchLifecycleState.ACTIVE,
    },
}

_WATCH_STATES = frozenset(state.value for state in ScmPollingWatchLifecycleState)


def _normalize_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


def _normalize_state(value: Any, *, field_name: str = "state") -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    if normalized not in _WATCH_STATES:
        allowed = ", ".join(sorted(_WATCH_STATES))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalized


def _watch_lifecycle_state(value: Any) -> ScmPollingWatchLifecycleState:
    normalized = _normalize_text(value)
    if normalized is None:
        raise RuntimeError("SCM polling watch lifecycle state is required")
    try:
        return ScmPollingWatchLifecycleState(normalized)
    except ValueError as exc:
        raise RuntimeError(
            f"unknown SCM polling watch lifecycle state: {normalized!r}"
        ) from exc


def plan_polling_watch_lifecycle_transition(
    transition: ScmPollingWatchLifecycleTransition | str,
    *,
    current_state: str | ScmPollingWatchLifecycleState | None,
) -> ScmPollingWatchLifecyclePlan:
    try:
        resolved_transition = ScmPollingWatchLifecycleTransition(transition)
    except ValueError as exc:
        raise RuntimeError(
            f"unknown SCM polling watch lifecycle transition: {transition!r}"
        ) from exc
    resolved_current = (
        _watch_lifecycle_state(current_state) if current_state is not None else None
    )
    allowed = _SCM_POLLING_WATCH_TRANSITIONS[resolved_transition]
    next_state = allowed.get(resolved_current)
    if next_state is None:
        raise RuntimeError(
            "invalid SCM polling watch lifecycle transition: "
            f"{resolved_transition.value} from "
            f"{resolved_current.value if resolved_current is not None else '<new>'}"
        )
    return ScmPollingWatchLifecyclePlan(
        transition=resolved_transition,
        previous_state=resolved_current,
        state=next_state,
    )


def _close_transition_for_state(
    state: ScmPollingWatchLifecycleState,
) -> ScmPollingWatchLifecycleTransition:
    if state is ScmPollingWatchLifecycleState.CLOSED:
        return ScmPollingWatchLifecycleTransition.CLOSE
    if state is ScmPollingWatchLifecycleState.EXPIRED:
        return ScmPollingWatchLifecycleTransition.EXPIRE
    if state is ScmPollingWatchLifecycleState.ERROR:
        return ScmPollingWatchLifecycleTransition.ERROR
    raise RuntimeError(f"cannot close SCM polling watch to {state.value!r}")


def _normalize_timestamp(value: Any, *, field_name: str) -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_after_seconds(value: str, *, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    shifted = parsed.astimezone(timezone.utc) + timedelta(seconds=max(1, int(seconds)))
    return shifted.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be a JSON object")


@dataclass(frozen=True)
class ScmPollingWatch:
    watch_id: str
    provider: str
    binding_id: str
    repo_slug: str
    pr_number: int
    workspace_root: str
    poll_interval_seconds: int
    state: str
    started_at: str
    updated_at: str
    expires_at: str
    next_poll_at: str
    repo_id: Optional[str] = None
    thread_target_id: Optional[str] = None
    last_polled_at: Optional[str] = None
    last_error_text: Optional[str] = None
    reaction_config: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _watch_from_row(row: sqlite3.Row) -> ScmPollingWatch:
    return ScmPollingWatch(
        watch_id=str(row["watch_id"]),
        provider=str(row["provider"]),
        binding_id=str(row["binding_id"]),
        repo_slug=str(row["repo_slug"]),
        pr_number=_normalize_int(row["pr_number"], field_name="pr_number"),
        workspace_root=str(row["workspace_root"]),
        poll_interval_seconds=_normalize_int(
            row["poll_interval_seconds"], field_name="poll_interval_seconds"
        ),
        state=_watch_lifecycle_state(row["state"]).value,
        started_at=str(row["started_at"]),
        updated_at=str(row["updated_at"]),
        expires_at=str(row["expires_at"]),
        next_poll_at=str(row["next_poll_at"]),
        repo_id=_normalize_text(row["repo_id"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        last_polled_at=_normalize_text(row["last_polled_at"]),
        last_error_text=_normalize_text(row["last_error_text"]),
        reaction_config=_json_loads_object(row["reaction_config_json"]),
        snapshot=_json_loads_object(row["snapshot_json"]),
    )


class ScmPollingWatchStore:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def get_watch(
        self,
        *,
        provider: str,
        binding_id: str,
    ) -> Optional[ScmPollingWatch]:
        normalized_provider = _normalize_text(provider)
        normalized_binding_id = _normalize_text(binding_id)
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_binding_id is None:
            raise ValueError("binding_id is required")
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_watch_row(
                conn,
                provider=normalized_provider,
                binding_id=normalized_binding_id,
            )
        return _watch_from_row(row) if row is not None else None

    def upsert_watch(
        self,
        *,
        provider: str,
        binding_id: str,
        repo_slug: str,
        pr_number: int,
        workspace_root: str,
        poll_interval_seconds: int,
        next_poll_at: str,
        expires_at: str,
        repo_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        reaction_config: Optional[dict[str, Any]] = None,
        snapshot: Optional[dict[str, Any]] = None,
    ) -> ScmPollingWatch:
        normalized_provider = _normalize_text(provider)
        normalized_binding_id = _normalize_text(binding_id)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_workspace_root = _normalize_text(workspace_root)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        normalized_poll_interval = _normalize_int(
            poll_interval_seconds,
            field_name="poll_interval_seconds",
        )
        normalized_next_poll_at = _normalize_timestamp(
            next_poll_at,
            field_name="next_poll_at",
        )
        normalized_expires_at = _normalize_timestamp(
            expires_at,
            field_name="expires_at",
        )
        normalized_repo_id = _normalize_text(repo_id)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        reaction_config_object = _normalize_json_object(
            reaction_config,
            field_name="reaction_config",
        )
        snapshot_object = _normalize_json_object(snapshot, field_name="snapshot")
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_binding_id is None:
            raise ValueError("binding_id is required")
        if normalized_repo_slug is None:
            raise ValueError("repo_slug is required")
        if normalized_workspace_root is None:
            raise ValueError("workspace_root is required")

        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_watch_row(
                conn,
                provider=normalized_provider,
                binding_id=normalized_binding_id,
            )
            if row is None:
                watch_id = uuid.uuid4().hex
                create_plan = plan_polling_watch_lifecycle_transition(
                    ScmPollingWatchLifecycleTransition.CREATE,
                    current_state=None,
                )
                conn.execute(
                    """
                    INSERT INTO orch_scm_polling_watches (
                        watch_id,
                        provider,
                        binding_id,
                        repo_slug,
                        repo_id,
                        pr_number,
                        workspace_root,
                        thread_target_id,
                        poll_interval_seconds,
                        state,
                        started_at,
                        updated_at,
                        expires_at,
                        next_poll_at,
                        last_polled_at,
                        last_error_text,
                        reaction_config_json,
                        snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        watch_id,
                        normalized_provider,
                        normalized_binding_id,
                        normalized_repo_slug,
                        normalized_repo_id,
                        normalized_pr_number,
                        normalized_workspace_root,
                        normalized_thread_target_id,
                        normalized_poll_interval,
                        create_plan.state.value,
                        timestamp,
                        timestamp,
                        normalized_expires_at,
                        normalized_next_poll_at,
                        _json_dumps(reaction_config_object),
                        _json_dumps(snapshot_object),
                    ),
                )
            else:
                reactivation_plan = plan_polling_watch_lifecycle_transition(
                    ScmPollingWatchLifecycleTransition.REACTIVATE,
                    current_state=row["state"],
                )
                conn.execute(
                    """
                    UPDATE orch_scm_polling_watches
                       SET repo_slug = ?,
                           repo_id = COALESCE(?, repo_id),
                           pr_number = ?,
                           workspace_root = ?,
                           thread_target_id = CASE
                               WHEN ? IS NULL THEN thread_target_id
                               ELSE ?
                           END,
                           poll_interval_seconds = ?,
                           state = ?,
                           updated_at = ?,
                           expires_at = ?,
                           next_poll_at = ?,
                           last_error_text = NULL,
                           reaction_config_json = ?,
                           snapshot_json = ?
                     WHERE watch_id = ?
                    """,
                    (
                        normalized_repo_slug,
                        normalized_repo_id,
                        normalized_pr_number,
                        normalized_workspace_root,
                        normalized_thread_target_id,
                        normalized_thread_target_id,
                        normalized_poll_interval,
                        reactivation_plan.state.value,
                        timestamp,
                        normalized_expires_at,
                        normalized_next_poll_at,
                        _json_dumps(reaction_config_object),
                        _json_dumps(snapshot_object),
                        str(row["watch_id"]),
                    ),
                )
            refreshed = self._load_watch_row(
                conn,
                provider=normalized_provider,
                binding_id=normalized_binding_id,
            )
        if refreshed is None:
            raise RuntimeError("SCM polling watch row missing after upsert")
        return _watch_from_row(refreshed)

    def list_due_watches(
        self,
        *,
        provider: Optional[str] = None,
        now_timestamp: Optional[str] = None,
        limit: int = 50,
    ) -> list[ScmPollingWatch]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        current_timestamp = _normalize_timestamp(
            now_timestamp or now_iso(),
            field_name="now_timestamp",
        )
        where_clauses = ["next_poll_at <= ?"]
        params: list[Any] = [current_timestamp]

        normalized_provider = _normalize_text(provider)
        if normalized_provider is not None:
            where_clauses.append("provider = ?")
            params.append(normalized_provider)

        params.append(resolved_limit)
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            self._assert_no_unknown_due_rows(
                conn,
                provider=normalized_provider,
                now_timestamp=current_timestamp,
            )
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE {" AND ".join(where_clauses)}
                   AND state = ?
                 ORDER BY next_poll_at ASC, started_at ASC, watch_id ASC
                 LIMIT ?
                """,
                tuple(
                    [
                        *params[:-1],
                        ScmPollingWatchLifecycleState.ACTIVE.value,
                        params[-1],
                    ]
                ),
            ).fetchall()
        watches: list[ScmPollingWatch] = []
        for row in rows:
            plan_polling_watch_lifecycle_transition(
                ScmPollingWatchLifecycleTransition.ADMIT_DUE,
                current_state=row["state"],
            )
            watches.append(_watch_from_row(row))
        return watches

    def claim_due_watches(
        self,
        *,
        provider: Optional[str] = None,
        now_timestamp: Optional[str] = None,
        limit: int = 50,
    ) -> list[ScmPollingWatch]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        claimed_at = _normalize_timestamp(
            now_timestamp or now_iso(),
            field_name="now_timestamp",
        )
        where_clauses = ["next_poll_at <= ?"]
        params: list[Any] = [claimed_at]

        normalized_provider = _normalize_text(provider)
        if normalized_provider is not None:
            where_clauses.append("provider = ?")
            params.append(normalized_provider)

        params.append(resolved_limit)
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_no_unknown_due_rows(
                conn,
                provider=normalized_provider,
                now_timestamp=claimed_at,
            )
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE {" AND ".join(where_clauses)}
                   AND state = ?
                 ORDER BY next_poll_at ASC, started_at ASC, watch_id ASC
                 LIMIT ?
                """,
                tuple(
                    [
                        *params[:-1],
                        ScmPollingWatchLifecycleState.ACTIVE.value,
                        params[-1],
                    ]
                ),
            ).fetchall()
            claimed: list[ScmPollingWatch] = []
            for row in rows:
                plan_polling_watch_lifecycle_transition(
                    ScmPollingWatchLifecycleTransition.CLAIM_DUE,
                    current_state=row["state"],
                )
                watch = _watch_from_row(row)
                claimed_until = _timestamp_after_seconds(
                    claimed_at,
                    seconds=watch.poll_interval_seconds,
                )
                cursor = conn.execute(
                    """
                    UPDATE orch_scm_polling_watches
                       SET updated_at = ?,
                           next_poll_at = ?
                     WHERE watch_id = ?
                       AND state = ?
                       AND next_poll_at <= ?
                    """,
                    (
                        claimed_at,
                        claimed_until,
                        watch.watch_id,
                        ScmPollingWatchLifecycleState.ACTIVE.value,
                        claimed_at,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                refreshed = conn.execute(
                    """
                    SELECT *
                      FROM orch_scm_polling_watches
                     WHERE watch_id = ?
                    """,
                    (watch.watch_id,),
                ).fetchone()
                if refreshed is not None:
                    claimed.append(_watch_from_row(refreshed))
            conn.commit()
        return claimed

    def refresh_watch(
        self,
        *,
        watch_id: str,
        snapshot: Optional[dict[str, Any]] = None,
        next_poll_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        last_polled_at: Optional[str] = None,
        last_error_text: Optional[str] = None,
    ) -> Optional[ScmPollingWatch]:
        normalized_watch_id = _normalize_text(watch_id)
        if normalized_watch_id is None:
            raise ValueError("watch_id is required")
        snapshot_object = _normalize_json_object(snapshot, field_name="snapshot")
        snapshot_json = _json_dumps(snapshot_object) if snapshot is not None else None
        has_snapshot = 1 if snapshot is not None else None
        normalized_next_poll_at = (
            _normalize_timestamp(next_poll_at, field_name="next_poll_at")
            if next_poll_at is not None
            else None
        )
        normalized_expires_at = (
            _normalize_timestamp(expires_at, field_name="expires_at")
            if expires_at is not None
            else None
        )
        normalized_last_polled_at = (
            _normalize_timestamp(last_polled_at, field_name="last_polled_at")
            if last_polled_at is not None
            else None
        )
        normalized_last_error_text = _normalize_text(last_error_text)
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
            if row is None:
                return None
            refresh_plan = plan_polling_watch_lifecycle_transition(
                ScmPollingWatchLifecycleTransition.REFRESH,
                current_state=row["state"],
            )
            cursor = conn.execute(
                """
                UPDATE orch_scm_polling_watches
                   SET updated_at = ?,
                       snapshot_json = CASE
                           WHEN ? IS NULL THEN snapshot_json
                           ELSE ?
                       END,
                       next_poll_at = COALESCE(?, next_poll_at),
                       expires_at = COALESCE(?, expires_at),
                       last_polled_at = COALESCE(?, last_polled_at),
                       last_error_text = ?,
                       state = ?
                 WHERE watch_id = ?
                """,
                (
                    timestamp,
                    has_snapshot,
                    snapshot_json,
                    normalized_next_poll_at,
                    normalized_expires_at,
                    normalized_last_polled_at,
                    normalized_last_error_text,
                    refresh_plan.state.value,
                    normalized_watch_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
        return _watch_from_row(row) if row is not None else None

    def close_watch(
        self,
        *,
        watch_id: str,
        state: str,
        last_error_text: Optional[str] = None,
    ) -> Optional[ScmPollingWatch]:
        normalized_watch_id = _normalize_text(watch_id)
        normalized_state = _normalize_state(state)
        normalized_last_error_text = _normalize_text(last_error_text)
        if normalized_watch_id is None:
            raise ValueError("watch_id is required")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
            if row is None:
                return None
            close_plan = plan_polling_watch_lifecycle_transition(
                _close_transition_for_state(_watch_lifecycle_state(normalized_state)),
                current_state=row["state"],
            )
            cursor = conn.execute(
                """
                UPDATE orch_scm_polling_watches
                   SET state = ?,
                       updated_at = ?,
                       last_error_text = ?
                 WHERE watch_id = ?
                """,
                (
                    close_plan.state.value,
                    timestamp,
                    normalized_last_error_text,
                    normalized_watch_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
        return _watch_from_row(row) if row is not None else None

    def _load_watch_row(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        binding_id: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_scm_polling_watches
             WHERE provider = ?
               AND binding_id = ?
             LIMIT 1
            """,
            (provider, binding_id),
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def _assert_no_unknown_due_rows(
        self,
        conn: sqlite3.Connection,
        *,
        provider: Optional[str],
        now_timestamp: str,
    ) -> None:
        states = tuple(state.value for state in ScmPollingWatchLifecycleState)
        provider_clause = ""
        params: list[Any] = [now_timestamp, *states]
        if provider is not None:
            provider_clause = " AND provider = ?"
            params.append(provider)
        row = conn.execute(
            f"""
            SELECT state
              FROM orch_scm_polling_watches
             WHERE next_poll_at <= ?
               AND state NOT IN (?, ?, ?, ?)
               {provider_clause}
             ORDER BY next_poll_at ASC, started_at ASC, watch_id ASC
             LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if row is not None:
            _watch_lifecycle_state(row["state"])


__all__ = [
    "ScmPollingWatch",
    "ScmPollingWatchLifecyclePlan",
    "ScmPollingWatchLifecycleState",
    "ScmPollingWatchLifecycleTransition",
    "ScmPollingWatchStore",
    "plan_polling_watch_lifecycle_transition",
]
