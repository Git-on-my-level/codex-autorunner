from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional, cast

from .locks import file_lock
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
)
from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    PMA_AUTOMATION_STORE_FILENAME,
    TIMER_TYPE_ONE_SHOT,
    _iso_now,
    _normalize_lane_id,
    _normalize_text,
    default_pma_automation_state,
)
from .text_utils import lock_path_for

logger = logging.getLogger(__name__)


class PmaAutomationPersistence:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable
        self._path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_AUTOMATION_STORE_FILENAME
        )

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is not None:
                return state
            state = default_pma_automation_state()
            return state

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        try:
            with open_orchestration_sqlite(
                self._hub_root,
                durable=self._durable,
                migrate=False,
            ) as conn:
                subscriptions = self._load_subscriptions_from_sqlite(conn)
                timers = self._load_timers_from_sqlite(conn)
                wakeups = self._load_wakeups_from_sqlite(conn)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return None
        updated_values: list[str] = []
        updated_values.extend(entry.updated_at for entry in subscriptions)
        updated_values.extend(entry.updated_at for entry in timers)
        updated_values.extend(entry.updated_at for entry in wakeups)
        state = default_pma_automation_state()
        if updated_values:
            state["updated_at"] = max(updated_values)
        state["subscriptions"] = [entry.to_dict() for entry in subscriptions]
        state["timers"] = [entry.to_dict() for entry in timers]
        state["wakeups"] = [entry.to_dict() for entry in wakeups]
        return state

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        state = self._load_unlocked()
        if state is None:
            state = default_pma_automation_state()
        return (
            state,
            self._normalize_subscriptions(state.get("subscriptions")),
            self._normalize_timers(state.get("timers")),
            self._normalize_wakeups(state.get("wakeups")),
        )

    def _normalize_subscriptions(self, value: Any) -> list[PmaLifecycleSubscription]:
        out: list[PmaLifecycleSubscription] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaLifecycleSubscription):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaLifecycleSubscription.from_dict(entry))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]:
        out: list[PmaAutomationTimer] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaAutomationTimer):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaAutomationTimer.from_dict(entry))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]:
        out: list[PmaAutomationWakeup] = []
        if not isinstance(value, list):
            return out
        for entry in value:
            if isinstance(entry, PmaAutomationWakeup):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                out.append(PmaAutomationWakeup.from_dict(entry))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _save_structured_unlocked(
        self,
        state: dict[str, Any],
        subscriptions: list[PmaLifecycleSubscription],
        timers: list[PmaAutomationTimer],
        wakeups: list[PmaAutomationWakeup],
    ) -> None:
        """Rewrite all automation rows for explicit migration/backfill callers only."""
        subscription_ids = {
            entry.subscription_id
            for entry in subscriptions
            if entry.subscription_id.strip()
        }
        filtered_timers = [
            entry
            for entry in timers
            if entry.subscription_id is None
            or entry.subscription_id in subscription_ids
        ]
        filtered_wakeups = [
            entry
            for entry in wakeups
            if entry.subscription_id is None
            or entry.subscription_id in subscription_ids
        ]
        dropped_timers = len(timers) - len(filtered_timers)
        dropped_wakeups = len(wakeups) - len(filtered_wakeups)
        if dropped_timers or dropped_wakeups:
            logger.warning(
                "Dropping orphaned automation rows before save (timers=%s, wakeups=%s)",
                dropped_timers,
                dropped_wakeups,
            )
        state["updated_at"] = _iso_now()
        state["subscriptions"] = [entry.to_dict() for entry in subscriptions]
        state["timers"] = [entry.to_dict() for entry in filtered_timers]
        state["wakeups"] = [entry.to_dict() for entry in filtered_wakeups]
        with open_orchestration_sqlite(self._hub_root, durable=self._durable) as conn:
            with conn:
                conn.execute("DELETE FROM orch_automation_wakeups")
                conn.execute("DELETE FROM orch_automation_timers")
                conn.execute("DELETE FROM orch_automation_subscriptions")
                for subscription in subscriptions:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_subscriptions (
                            subscription_id,
                            event_types_json,
                            repo_id,
                            run_id,
                            thread_target_id,
                            binding_id,
                            lane_id,
                            from_state,
                            to_state,
                            notify_once,
                            state,
                            match_count,
                            metadata_json,
                            created_at,
                            updated_at,
                            disabled_at,
                            reason_text,
                            idempotency_key,
                            max_matches
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            subscription.subscription_id,
                            json.dumps(subscription.event_types),
                            subscription.repo_id,
                            subscription.run_id,
                            subscription.thread_id,
                            None,
                            subscription.lane_id,
                            subscription.from_state,
                            subscription.to_state,
                            1 if subscription.max_matches == 1 else 0,
                            subscription.state,
                            subscription.match_count,
                            json.dumps(subscription.metadata),
                            subscription.created_at,
                            subscription.updated_at,
                            (
                                subscription.updated_at
                                if subscription.state != "active"
                                else None
                            ),
                            subscription.reason,
                            subscription.idempotency_key,
                            subscription.max_matches,
                        ),
                    )
                for timer in filtered_timers:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_timers (
                            timer_id,
                            subscription_id,
                            repo_id,
                            run_id,
                            thread_target_id,
                            timer_kind,
                            schedule_key,
                            available_at,
                            payload_json,
                            state,
                            created_at,
                            updated_at,
                            fired_at,
                            reason_text,
                            idempotency_key,
                            idle_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            timer.timer_id,
                            timer.subscription_id,
                            timer.repo_id,
                            timer.run_id,
                            timer.thread_id,
                            timer.timer_type,
                            timer.subscription_id or timer.idempotency_key,
                            timer.due_at,
                            json.dumps(
                                {
                                    "metadata": timer.metadata,
                                    "from_state": timer.from_state,
                                    "to_state": timer.to_state,
                                }
                            ),
                            timer.state,
                            timer.created_at,
                            timer.updated_at,
                            timer.fired_at,
                            timer.reason,
                            timer.idempotency_key,
                            timer.idle_seconds,
                        ),
                    )
                for wakeup in filtered_wakeups:
                    conn.execute(
                        """
                        INSERT INTO orch_automation_wakeups (
                            wakeup_id,
                            subscription_id,
                            repo_id,
                            run_id,
                            thread_target_id,
                            lane_id,
                            wakeup_kind,
                            state,
                            available_at,
                            claimed_at,
                            completed_at,
                            reason_text,
                            payload_json,
                            created_at,
                            updated_at,
                            dispatched_at,
                            timestamp,
                            idempotency_key,
                            timer_id,
                            event_id,
                            event_type
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            wakeup.wakeup_id,
                            wakeup.subscription_id,
                            wakeup.repo_id,
                            wakeup.run_id,
                            wakeup.thread_id,
                            wakeup.lane_id,
                            wakeup.source,
                            wakeup.state,
                            wakeup.timestamp,
                            None,
                            None,
                            wakeup.reason,
                            json.dumps(
                                {
                                    "metadata": wakeup.metadata,
                                    "event_data": wakeup.event_data,
                                    "from_state": wakeup.from_state,
                                    "to_state": wakeup.to_state,
                                }
                            ),
                            wakeup.created_at,
                            wakeup.updated_at,
                            wakeup.dispatched_at,
                            wakeup.timestamp,
                            wakeup.idempotency_key,
                            wakeup.timer_id,
                            wakeup.event_id,
                            wakeup.event_type,
                        ),
                    )

    @staticmethod
    def _json_load(raw: str | None) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_load_list(raw: str | None) -> list[str]:
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(item).lower() for item in parsed if isinstance(item, str)]

    def _row_to_subscription(self, row: Any) -> PmaLifecycleSubscription:
        return PmaLifecycleSubscription(
            subscription_id=str(row["subscription_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "active"),
            event_types=self._json_load_list(row["event_types_json"]),
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=str(row["lane_id"] or DEFAULT_PMA_LANE_ID),
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason_text"],
            idempotency_key=row["idempotency_key"],
            max_matches=(
                int(row["max_matches"]) if row["max_matches"] is not None else None
            ),
            match_count=int(row["match_count"] or 0),
            metadata=self._json_load(row["metadata_json"]),
        )

    def _row_to_timer(self, row: Any) -> PmaAutomationTimer:
        payload = self._json_load(row["payload_json"])
        metadata = cast(
            dict[str, Any],
            (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
        return PmaAutomationTimer(
            timer_id=str(row["timer_id"]),
            due_at=str(row["available_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "pending"),
            fired_at=row["fired_at"],
            timer_type=str(row["timer_kind"] or TIMER_TYPE_ONE_SHOT),
            idle_seconds=(
                int(row["idle_seconds"]) if row["idle_seconds"] is not None else None
            ),
            subscription_id=row["subscription_id"],
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=_normalize_lane_id(payload.get("lane_id")),
            from_state=_normalize_text(payload.get("from_state")),
            to_state=_normalize_text(payload.get("to_state")),
            reason=row["reason_text"],
            idempotency_key=row["idempotency_key"],
            metadata=metadata,
        )

    def _row_to_wakeup(self, row: Any) -> PmaAutomationWakeup:
        payload = self._json_load(row["payload_json"])
        event_data = cast(
            dict[str, Any],
            (
                payload.get("event_data")
                if isinstance(payload.get("event_data"), dict)
                else {}
            ),
        )
        metadata = cast(
            dict[str, Any],
            (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
        return PmaAutomationWakeup(
            wakeup_id=str(row["wakeup_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            state=str(row["state"] or "pending"),
            dispatched_at=row["dispatched_at"],
            source=str(row["wakeup_kind"] or "automation"),
            repo_id=row["repo_id"],
            run_id=row["run_id"],
            thread_id=row["thread_target_id"],
            lane_id=str(row["lane_id"] or DEFAULT_PMA_LANE_ID),
            from_state=_normalize_text(payload.get("from_state")),
            to_state=_normalize_text(payload.get("to_state")),
            reason=row["reason_text"],
            timestamp=row["timestamp"],
            idempotency_key=row["idempotency_key"],
            subscription_id=row["subscription_id"],
            timer_id=row["timer_id"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            event_data=event_data,
            metadata=metadata,
        )

    def _load_subscriptions_from_sqlite(
        self,
        conn: Any,
    ) -> list[PmaLifecycleSubscription]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_subscriptions
             ORDER BY created_at ASC, subscription_id ASC
            """
        ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def _load_timers_from_sqlite(self, conn: Any) -> list[PmaAutomationTimer]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_timers
             ORDER BY created_at ASC, timer_id ASC
            """
        ).fetchall()
        return [self._row_to_timer(row) for row in rows]

    def _load_wakeups_from_sqlite(self, conn: Any) -> list[PmaAutomationWakeup]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_wakeups
             ORDER BY created_at ASC, wakeup_id ASC
            """
        ).fetchall()
        return [self._row_to_wakeup(row) for row in rows]

    def _insert_subscription_row(
        self, conn: Any, subscription: PmaLifecycleSubscription
    ) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_subscriptions (
                subscription_id, event_types_json, repo_id, run_id,
                thread_target_id, binding_id, lane_id, from_state, to_state,
                notify_once, state, match_count, metadata_json,
                created_at, updated_at, disabled_at,
                reason_text, idempotency_key, max_matches
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription.subscription_id,
                json.dumps(subscription.event_types),
                subscription.repo_id,
                subscription.run_id,
                subscription.thread_id,
                None,
                subscription.lane_id,
                subscription.from_state,
                subscription.to_state,
                1 if subscription.max_matches == 1 else 0,
                subscription.state,
                subscription.match_count,
                json.dumps(subscription.metadata),
                subscription.created_at,
                subscription.updated_at,
                (subscription.updated_at if subscription.state != "active" else None),
                subscription.reason,
                subscription.idempotency_key,
                subscription.max_matches,
            ),
        )

    def _update_subscription_row(
        self, conn: Any, subscription: PmaLifecycleSubscription
    ) -> None:
        conn.execute(
            """
            UPDATE orch_automation_subscriptions
               SET event_types_json = ?,
                   repo_id = ?,
                   run_id = ?,
                   thread_target_id = ?,
                   lane_id = ?,
                   from_state = ?,
                   to_state = ?,
                   notify_once = ?,
                   state = ?,
                   match_count = ?,
                   metadata_json = ?,
                   updated_at = ?,
                   disabled_at = ?,
                   reason_text = ?,
                   idempotency_key = ?,
                   max_matches = ?
             WHERE subscription_id = ?
            """,
            (
                json.dumps(subscription.event_types),
                subscription.repo_id,
                subscription.run_id,
                subscription.thread_id,
                subscription.lane_id,
                subscription.from_state,
                subscription.to_state,
                1 if subscription.max_matches == 1 else 0,
                subscription.state,
                subscription.match_count,
                json.dumps(subscription.metadata),
                subscription.updated_at,
                (subscription.updated_at if subscription.state != "active" else None),
                subscription.reason,
                subscription.idempotency_key,
                subscription.max_matches,
                subscription.subscription_id,
            ),
        )

    def _insert_timer_row(self, conn: Any, timer: PmaAutomationTimer) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_timers (
                timer_id, subscription_id, repo_id, run_id,
                thread_target_id, timer_kind, schedule_key, available_at,
                payload_json, state, created_at, updated_at,
                fired_at, reason_text, idempotency_key, idle_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timer.timer_id,
                timer.subscription_id,
                timer.repo_id,
                timer.run_id,
                timer.thread_id,
                timer.timer_type,
                timer.subscription_id or timer.idempotency_key,
                timer.due_at,
                json.dumps(
                    {
                        "metadata": timer.metadata,
                        "from_state": timer.from_state,
                        "to_state": timer.to_state,
                    }
                ),
                timer.state,
                timer.created_at,
                timer.updated_at,
                timer.fired_at,
                timer.reason,
                timer.idempotency_key,
                timer.idle_seconds,
            ),
        )

    def _update_timer_row(self, conn: Any, timer: PmaAutomationTimer) -> None:
        conn.execute(
            """
            UPDATE orch_automation_timers
               SET subscription_id = ?,
                   repo_id = ?,
                   run_id = ?,
                   thread_target_id = ?,
                   timer_kind = ?,
                   schedule_key = ?,
                   available_at = ?,
                   payload_json = ?,
                   state = ?,
                   updated_at = ?,
                   fired_at = ?,
                   reason_text = ?,
                   idempotency_key = ?,
                   idle_seconds = ?
             WHERE timer_id = ?
            """,
            (
                timer.subscription_id,
                timer.repo_id,
                timer.run_id,
                timer.thread_id,
                timer.timer_type,
                timer.subscription_id or timer.idempotency_key,
                timer.due_at,
                json.dumps(
                    {
                        "metadata": timer.metadata,
                        "from_state": timer.from_state,
                        "to_state": timer.to_state,
                    }
                ),
                timer.state,
                timer.updated_at,
                timer.fired_at,
                timer.reason,
                timer.idempotency_key,
                timer.idle_seconds,
                timer.timer_id,
            ),
        )

    def _insert_wakeup_row(self, conn: Any, wakeup: PmaAutomationWakeup) -> None:
        conn.execute(
            """
            INSERT INTO orch_automation_wakeups (
                wakeup_id, subscription_id, repo_id, run_id,
                thread_target_id, lane_id, wakeup_kind, state,
                available_at, claimed_at, completed_at, reason_text,
                payload_json, created_at, updated_at, dispatched_at,
                timestamp, idempotency_key, timer_id, event_id, event_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wakeup.wakeup_id,
                wakeup.subscription_id,
                wakeup.repo_id,
                wakeup.run_id,
                wakeup.thread_id,
                wakeup.lane_id,
                wakeup.source,
                wakeup.state,
                wakeup.timestamp,
                None,
                None,
                wakeup.reason,
                json.dumps(
                    {
                        "metadata": wakeup.metadata,
                        "event_data": wakeup.event_data,
                        "from_state": wakeup.from_state,
                        "to_state": wakeup.to_state,
                    }
                ),
                wakeup.created_at,
                wakeup.updated_at,
                wakeup.dispatched_at,
                wakeup.timestamp,
                wakeup.idempotency_key,
                wakeup.timer_id,
                wakeup.event_id,
                wakeup.event_type,
            ),
        )

    def _update_wakeup_row(self, conn: Any, wakeup: PmaAutomationWakeup) -> None:
        conn.execute(
            """
            UPDATE orch_automation_wakeups
               SET subscription_id = ?,
                   repo_id = ?,
                   run_id = ?,
                   thread_target_id = ?,
                   lane_id = ?,
                   wakeup_kind = ?,
                   state = ?,
                   available_at = ?,
                   reason_text = ?,
                   payload_json = ?,
                   updated_at = ?,
                   dispatched_at = ?,
                   timestamp = ?,
                   idempotency_key = ?,
                   timer_id = ?,
                   event_id = ?,
                   event_type = ?
             WHERE wakeup_id = ?
            """,
            (
                wakeup.subscription_id,
                wakeup.repo_id,
                wakeup.run_id,
                wakeup.thread_id,
                wakeup.lane_id,
                wakeup.source,
                wakeup.state,
                wakeup.timestamp,
                wakeup.reason,
                json.dumps(
                    {
                        "metadata": wakeup.metadata,
                        "event_data": wakeup.event_data,
                        "from_state": wakeup.from_state,
                        "to_state": wakeup.to_state,
                    }
                ),
                wakeup.updated_at,
                wakeup.dispatched_at,
                wakeup.timestamp,
                wakeup.idempotency_key,
                wakeup.timer_id,
                wakeup.event_id,
                wakeup.event_type,
                wakeup.wakeup_id,
            ),
        )

    def with_write_connection(self) -> Any:
        return open_orchestration_sqlite(self._hub_root, durable=self._durable)

    def find_active_subscription_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaLifecycleSubscription]:
        return self._find_active_subscription_by_key(conn, key)

    def find_pending_timer_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaAutomationTimer]:
        return self._find_pending_timer_by_key(conn, key)

    def subscription_id_exists(self, conn: Any, sub_id: str) -> bool:
        return self._subscription_id_exists(conn, sub_id)

    def insert_subscription(
        self, conn: Any, subscription: PmaLifecycleSubscription
    ) -> None:
        self._insert_subscription_row(conn, subscription)

    def insert_timer(self, conn: Any, timer: PmaAutomationTimer) -> None:
        self._insert_timer_row(conn, timer)

    def insert_wakeup(self, conn: Any, wakeup: PmaAutomationWakeup) -> None:
        self._insert_wakeup_row(conn, wakeup)

    def update_timer(self, conn: Any, timer: PmaAutomationTimer) -> None:
        self._update_timer_row(conn, timer)

    def update_wakeup(self, conn: Any, wakeup: PmaAutomationWakeup) -> None:
        self._update_wakeup_row(conn, wakeup)

    def update_subscription(
        self, conn: Any, subscription: PmaLifecycleSubscription
    ) -> None:
        self._update_subscription_row(conn, subscription)

    def find_wakeup_by_idempotency_key(
        self, conn: Any, key: str
    ) -> Optional[PmaAutomationWakeup]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_automation_wakeups
             WHERE idempotency_key = ?
             LIMIT 1
            """,
            (key,),
        ).fetchone()
        return self._row_to_wakeup(row) if row is not None else None

    def cancel_subscription(self, conn: Any, subscription_id: str, stamp: str) -> bool:
        cursor = conn.execute(
            """
            UPDATE orch_automation_subscriptions
               SET state = 'cancelled',
                   updated_at = ?,
                   disabled_at = ?
             WHERE subscription_id = ?
               AND state != 'cancelled'
            """,
            (stamp, stamp, subscription_id),
        )
        if cursor.rowcount <= 0:
            return False
        conn.execute(
            """
            UPDATE orch_automation_rules
               SET enabled = 0,
                   updated_at = ?
             WHERE rule_id = ?
            """,
            (stamp, f"builtin:pma:subscription:{subscription_id}"),
        )
        return True

    def purge_subscription(
        self, conn: Any, subscription_id: str, *, require_inactive: bool
    ) -> bool:
        row = conn.execute(
            "SELECT state FROM orch_automation_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        if row is None:
            return False
        if require_inactive and str(row["state"]) == "active":
            return False
        self._delete_subscription_dependents(conn, [subscription_id])
        conn.execute(
            "DELETE FROM orch_automation_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        )
        return True

    def purge_subscriptions(
        self,
        conn: Any,
        *,
        state_filter: Optional[str],
        dry_run: bool,
    ) -> list[PmaLifecycleSubscription]:
        query = "SELECT * FROM orch_automation_subscriptions"
        params: tuple[Any, ...] = ()
        if state_filter is not None:
            query += " WHERE state = ?"
            params = (state_filter,)
        rows = conn.execute(query, params).fetchall()
        removed = [self._row_to_subscription(row) for row in rows]
        if removed and not dry_run:
            removed_ids = [entry.subscription_id for entry in removed]
            self._delete_subscription_dependents(conn, removed_ids)
            placeholders = ",".join("?" for _ in removed_ids)
            conn.execute(
                f"DELETE FROM orch_automation_subscriptions WHERE subscription_id IN ({placeholders})",
                tuple(removed_ids),
            )
        return removed

    def _delete_subscription_dependents(
        self, conn: Any, subscription_ids: list[str]
    ) -> None:
        total_orphaned_timers = 0
        total_orphaned_wakeups = 0
        for sub_id in subscription_ids:
            total_orphaned_timers += conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_timers WHERE subscription_id = ?",
                (sub_id,),
            ).fetchone()["c"]
            total_orphaned_wakeups += conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_wakeups WHERE subscription_id = ?",
                (sub_id,),
            ).fetchone()["c"]
            conn.execute(
                "DELETE FROM orch_automation_wakeups WHERE subscription_id = ?",
                (sub_id,),
            )
            conn.execute(
                "DELETE FROM orch_automation_timers WHERE subscription_id = ?",
                (sub_id,),
            )
        if total_orphaned_timers or total_orphaned_wakeups:
            logger.warning(
                "Dropping orphaned automation rows before save (timers=%s, wakeups=%s)",
                total_orphaned_timers,
                total_orphaned_wakeups,
            )

    def cancel_timer(
        self,
        conn: Any,
        timer_id: str,
        *,
        cancelled_at: str,
        reason: Optional[str],
    ) -> bool:
        if reason is not None:
            cursor = conn.execute(
                """
                UPDATE orch_automation_timers
                   SET state = 'cancelled',
                       updated_at = ?,
                       reason_text = ?
                 WHERE timer_id = ?
                   AND state != 'cancelled'
                """,
                (cancelled_at, reason, timer_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE orch_automation_timers
                   SET state = 'cancelled',
                       updated_at = ?
                 WHERE timer_id = ?
                   AND state != 'cancelled'
                """,
                (cancelled_at, timer_id),
            )
        if cursor.rowcount <= 0:
            return False
        conn.execute(
            """
            UPDATE orch_automation_schedules
               SET state = 'cancelled',
                   next_fire_at = NULL,
                   updated_at = ?
             WHERE schedule_id = ?
            """,
            (cancelled_at, f"pma-timer:{timer_id}"),
        )
        return True

    def purge_timer(self, conn: Any, timer_id: str, *, require_inactive: bool) -> bool:
        row = conn.execute(
            "SELECT state FROM orch_automation_timers WHERE timer_id = ?",
            (timer_id,),
        ).fetchone()
        if row is None:
            return False
        if require_inactive and str(row["state"]) == "pending":
            return False
        conn.execute(
            "DELETE FROM orch_automation_timers WHERE timer_id = ?",
            (timer_id,),
        )
        return True

    def purge_wakeup(
        self, conn: Any, wakeup_id: str, *, require_inactive: bool
    ) -> bool:
        row = conn.execute(
            "SELECT state FROM orch_automation_wakeups WHERE wakeup_id = ?",
            (wakeup_id,),
        ).fetchone()
        if row is None:
            return False
        if require_inactive and str(row["state"]) in {"pending", "queued"}:
            return False
        conn.execute(
            "DELETE FROM orch_automation_wakeups WHERE wakeup_id = ?",
            (wakeup_id,),
        )
        return True

    def _find_active_subscription_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaLifecycleSubscription]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_subscriptions
             WHERE idempotency_key = ? AND state = 'active'
             LIMIT 1
            """,
            (key,),
        ).fetchall()
        return self._row_to_subscription(rows[0]) if rows else None

    def _find_pending_timer_by_key(
        self, conn: Any, key: str
    ) -> Optional[PmaAutomationTimer]:
        rows = conn.execute(
            """
            SELECT *
              FROM orch_automation_timers
             WHERE idempotency_key = ? AND state = 'pending'
             LIMIT 1
            """,
            (key,),
        ).fetchall()
        return self._row_to_timer(rows[0]) if rows else None

    def _subscription_id_exists(self, conn: Any, sub_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM orch_automation_subscriptions WHERE subscription_id = ?",
            (sub_id,),
        ).fetchone()
        return row is not None


__all__ = ["PmaAutomationPersistence"]
