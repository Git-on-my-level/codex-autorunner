from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..sqlite_utils import open_sqlite
from .migrate_legacy_state import (
    LEGACY_PMA_AUDIT_LOG_PATH,
    LEGACY_PMA_AUTOMATION_PATH,
    LEGACY_PMA_LIFECYCLE_LOG_PATH,
    LEGACY_PMA_QUEUE_DIR,
    LEGACY_PMA_THREADS_DB_PATH,
    LEGACY_PMA_TRANSCRIPTS_DIR,
    _load_json_file,
    _load_json_lines,
    _table_exists,
)


@dataclass(frozen=True)
class ParityCheckResult:
    check_name: str
    status: str
    legacy_count: int
    new_count: int
    details: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass(frozen=True)
class TranscriptParityResult:
    check_name: str
    status: str
    legacy_transcripts: int
    new_transcripts: int
    content_hash_matches: int
    length_mismatches: int
    details: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass(frozen=True)
class MigrationVerificationSummary:
    run_id: str
    started_at: str
    finished_at: Optional[str]
    status: str
    thread_parity: list[ParityCheckResult]
    automation_parity: list[ParityCheckResult]
    queue_parity: list[ParityCheckResult]
    transcript_parity: Optional[TranscriptParityResult]
    event_parity: list[ParityCheckResult]
    audit_parity: ParityCheckResult
    overall_passed: bool
    rollback_available: bool
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "thread_parity": [
                {
                    "check_name": p.check_name,
                    "status": p.status,
                    "legacy_count": p.legacy_count,
                    "new_count": p.new_count,
                    "details": p.details,
                    "message": p.message,
                }
                for p in self.thread_parity
            ],
            "automation_parity": [
                {
                    "check_name": p.check_name,
                    "status": p.status,
                    "legacy_count": p.legacy_count,
                    "new_count": p.new_count,
                    "details": p.details,
                    "message": p.message,
                }
                for p in self.automation_parity
            ],
            "queue_parity": [
                {
                    "check_name": p.check_name,
                    "status": p.status,
                    "legacy_count": p.legacy_count,
                    "new_count": p.new_count,
                    "details": p.details,
                    "message": p.message,
                }
                for p in self.queue_parity
            ],
            "transcript_parity": (
                {
                    "check_name": self.transcript_parity.check_name,
                    "status": self.transcript_parity.status,
                    "legacy_transcripts": self.transcript_parity.legacy_transcripts,
                    "new_transcripts": self.transcript_parity.new_transcripts,
                    "content_hash_matches": self.transcript_parity.content_hash_matches,
                    "length_mismatches": self.transcript_parity.length_mismatches,
                    "details": self.transcript_parity.details,
                    "message": self.transcript_parity.message,
                }
                if self.transcript_parity
                else None
            ),
            "event_parity": [
                {
                    "check_name": p.check_name,
                    "status": p.status,
                    "legacy_count": p.legacy_count,
                    "new_count": p.new_count,
                    "details": p.details,
                    "message": p.message,
                }
                for p in self.event_parity
            ],
            "audit_parity": {
                "check_name": self.audit_parity.check_name,
                "status": self.audit_parity.status,
                "legacy_count": self.audit_parity.legacy_count,
                "new_count": self.audit_parity.new_count,
                "details": self.audit_parity.details,
                "message": self.audit_parity.message,
            },
            "overall_passed": self.overall_passed,
            "rollback_available": self.rollback_available,
            "recommendations": self.recommendations,
        }


def _count_legacy_threads(hub_root: Path) -> dict[str, int]:
    legacy_path = hub_root / LEGACY_PMA_THREADS_DB_PATH
    if not legacy_path.exists():
        return {"threads": 0, "turns": 0, "actions": 0}
    with open_sqlite(legacy_path) as conn:
        counts = {}
        if _table_exists(conn, "pma_managed_threads"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pma_managed_threads"
            ).fetchone()
            counts["threads"] = int(row["cnt"]) if row else 0
        else:
            counts["threads"] = 0
        if _table_exists(conn, "pma_managed_turns"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pma_managed_turns"
            ).fetchone()
            counts["turns"] = int(row["cnt"]) if row else 0
        else:
            counts["turns"] = 0
        if _table_exists(conn, "pma_managed_actions"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pma_managed_actions"
            ).fetchone()
            counts["actions"] = int(row["cnt"]) if row else 0
        else:
            counts["actions"] = 0
        return counts


def _get_thread_ids(hub_root: Path, limit: int = 10) -> list[str]:
    legacy_path = hub_root / LEGACY_PMA_THREADS_DB_PATH
    if not legacy_path.exists():
        return []
    with open_sqlite(legacy_path) as conn:
        if _table_exists(conn, "pma_managed_threads"):
            rows = conn.execute(
                "SELECT managed_thread_id FROM pma_managed_threads LIMIT ?", (limit,)
            ).fetchall()
            return [
                str(row["managed_thread_id"])
                for row in rows
                if row["managed_thread_id"]
            ]
    return []


def verify_thread_parity(hub_root: Path, conn: Any) -> list[ParityCheckResult]:
    legacy_counts = _count_legacy_threads(hub_root)
    results = []
    if _table_exists(conn, "orch_thread_targets"):
        new_threads = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_thread_targets"
        ).fetchone()
        new_thread_count = int(new_threads["cnt"]) if new_threads else 0
    else:
        new_thread_count = 0

    status = (
        "passed" if legacy_counts.get("threads", 0) == new_thread_count else "failed"
    )
    results.append(
        ParityCheckResult(
            check_name="thread_targets_count",
            status=status,
            legacy_count=legacy_counts.get("threads", 0),
            new_count=new_thread_count,
            message=(
                f"Thread targets: {legacy_counts.get('threads', 0)} legacy, "
                f"{new_thread_count} migrated"
            ),
        )
    )

    if _table_exists(conn, "orch_thread_executions"):
        new_turns = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_thread_executions"
        ).fetchone()
        new_turn_count = int(new_turns["cnt"]) if new_turns else 0
    else:
        new_turn_count = 0
    status = "passed" if legacy_counts.get("turns", 0) == new_turn_count else "failed"
    results.append(
        ParityCheckResult(
            check_name="thread_executions_count",
            status=status,
            legacy_count=legacy_counts.get("turns", 0),
            new_count=new_turn_count,
            message=(
                f"Thread executions: {legacy_counts.get('turns', 0)} legacy, "
                f"{new_turn_count} migrated"
            ),
        )
    )

    if _table_exists(conn, "orch_thread_actions"):
        new_actions = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_thread_actions"
        ).fetchone()
        new_action_count = int(new_actions["cnt"]) if new_actions else 0
    else:
        new_action_count = 0
    status = (
        "passed" if legacy_counts.get("actions", 0) == new_action_count else "failed"
    )
    results.append(
        ParityCheckResult(
            check_name="thread_actions_count",
            status=status,
            legacy_count=legacy_counts.get("actions", 0),
            new_count=new_action_count,
            message=(
                f"Thread actions: {legacy_counts.get('actions', 0)} legacy, "
                f"{new_action_count} migrated"
            ),
        )
    )

    legacy_ids = _get_thread_ids(hub_root)
    if legacy_ids and _table_exists(conn, "orch_thread_targets"):
        placeholders = ",".join("?" * len(legacy_ids))
        existing = conn.execute(
            f"SELECT thread_target_id FROM orch_thread_targets WHERE thread_target_id IN ({placeholders})",
            legacy_ids,
        ).fetchall()
        found_ids = {str(row["thread_target_id"]) for row in existing}
        missing = set(legacy_ids) - found_ids
        if missing:
            results.append(
                ParityCheckResult(
                    check_name="thread_target_ids_sample",
                    status="passed" if not missing else "failed",
                    legacy_count=len(legacy_ids),
                    new_count=len(found_ids),
                    details={
                        "sample_ids_checked": len(legacy_ids),
                        "missing_ids": list(missing)[:5],
                    },
                    message=(
                        f"Sample ID check: {len(found_ids)} of {len(legacy_ids)} found"
                        + (f", missing: {list(missing)[:3]}" if missing else "")
                    ),
                )
            )
    return results


def _count_legacy_automation(hub_root: Path) -> dict[str, int]:
    state = _load_json_file(hub_root / LEGACY_PMA_AUTOMATION_PATH)
    if state is None:
        return {"subscriptions": 0, "timers": 0, "wakeups": 0}
    subs = state.get("subscriptions")
    timers = state.get("timers")
    wakeups = state.get("wakeups")
    return {
        "subscriptions": len(subs) if isinstance(subs, list) else 0,
        "timers": len(timers) if isinstance(timers, list) else 0,
        "wakeups": len(wakeups) if isinstance(wakeups, list) else 0,
    }


def _get_automation_idempotency_keys(
    hub_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    state = _load_json_file(hub_root / LEGACY_PMA_AUTOMATION_PATH)
    if state is None:
        return ([], [], [])
    subs = state.get("subscriptions", [])
    timers = state.get("timers", [])
    wakeups = state.get("wakeups", [])
    sub_keys = [
        str(e.get("idempotency_key", "")) for e in subs if e.get("idempotency_key")
    ]
    timer_keys = [
        str(e.get("idempotency_key", "")) for e in timers if e.get("idempotency_key")
    ]
    wakeup_keys = [
        str(e.get("idempotency_key", "")) for e in wakeups if e.get("idempotency_key")
    ]
    return (sub_keys, timer_keys, wakeup_keys)


def verify_automation_parity(hub_root: Path, conn: Any) -> list[ParityCheckResult]:
    legacy_counts = _count_legacy_automation(hub_root)
    results = []
    if _table_exists(conn, "orch_automation_subscriptions"):
        new_subs = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_automation_subscriptions"
        ).fetchone()
        new_sub_count = int(new_subs["cnt"]) if new_subs else 0
    else:
        new_sub_count = 0
    status = (
        "passed" if legacy_counts.get("subscriptions", 0) == new_sub_count else "failed"
    )
    results.append(
        ParityCheckResult(
            check_name="automation_subscriptions_count",
            status=status,
            legacy_count=legacy_counts.get("subscriptions", 0),
            new_count=new_sub_count,
            message=(
                f"Subscriptions: {legacy_counts.get('subscriptions', 0)} legacy, "
                f"{new_sub_count} migrated"
            ),
        )
    )

    if _table_exists(conn, "orch_automation_timers"):
        new_timers = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_automation_timers"
        ).fetchone()
        new_timer_count = int(new_timers["cnt"]) if new_timers else 0
    else:
        new_timer_count = 0
    status = "passed" if legacy_counts.get("timers", 0) == new_timer_count else "failed"
    results.append(
        ParityCheckResult(
            check_name="automation_timers_count",
            status=status,
            legacy_count=legacy_counts.get("timers", 0),
            new_count=new_timer_count,
            message=(
                f"Timers: {legacy_counts.get('timers', 0)} legacy, {new_timer_count} migrated"
            ),
        )
    )

    if _table_exists(conn, "orch_automation_wakeups"):
        new_wakeups = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_automation_wakeups"
        ).fetchone()
        new_wakeup_count = int(new_wakeups["cnt"]) if new_wakeups else 0
    else:
        new_wakeup_count = 0
    status = (
        "passed" if legacy_counts.get("wakeups", 0) == new_wakeup_count else "failed"
    )
    results.append(
        ParityCheckResult(
            check_name="automation_wakeups_count",
            status=status,
            legacy_count=legacy_counts.get("wakeups", 0),
            new_count=new_wakeup_count,
            message=(
                f"Wakeups: {legacy_counts.get('wakeups', 0)} legacy, {new_wakeup_count} migrated"
            ),
        )
    )

    legacy_keys = _get_automation_idempotency_keys(hub_root)
    if legacy_keys[0] and _table_exists(conn, "orch_automation_subscriptions"):
        placeholders = ",".join("?" * len(legacy_keys[0]))
        existing = conn.execute(
            f"SELECT idempotency_key FROM orch_automation_subscriptions WHERE idempotency_key IN ({placeholders})",
            legacy_keys[0],
        ).fetchall()
        found_keys = {
            str(row["idempotency_key"]) for row in existing if row["idempotency_key"]
        }
        missing = set(legacy_keys[0]) - found_keys
        results.append(
            ParityCheckResult(
                check_name="subscription_idempotency_keys",
                status="passed" if not missing else "failed",
                legacy_count=len(legacy_keys[0]),
                new_count=len(found_keys),
                details={"missing_keys": list(missing)[:5]},
                message=(
                    f"Idempotency keys: {len(found_keys)} of {len(legacy_keys[0])} found"
                    + (f", missing: {list(missing)[:3]}" if missing else "")
                ),
            )
        )

    pending_states = ["pending", "armed", "scheduled"]
    if _table_exists(conn, "orch_automation_timers"):
        pending_timers = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_automation_timers WHERE state IN (?, ?, ?)",
            pending_states,
        ).fetchone()
        pending_timer_count = int(pending_timers["cnt"]) if pending_timers else 0
    else:
        pending_timer_count = 0
    results.append(
        ParityCheckResult(
            check_name="pending_timers_count",
            status="passed",
            legacy_count=legacy_counts.get("timers", 0),
            new_count=pending_timer_count,
            details={"pending_states": pending_states},
            message=f"Pending timers: {pending_timer_count}",
        )
    )

    if _table_exists(conn, "orch_automation_wakeups"):
        pending_wakeups = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_automation_wakeups WHERE state IN (?, ?, ?)",
            pending_states,
        ).fetchone()
        pending_wakeup_count = int(pending_wakeups["cnt"]) if pending_wakeups else 0
    else:
        pending_wakeup_count = 0
    results.append(
        ParityCheckResult(
            check_name="pending_wakeups_count",
            status="passed",
            legacy_count=legacy_counts.get("wakeups", 0),
            new_count=pending_wakeup_count,
            details={"pending_states": pending_states},
            message=f"Pending wakeups: {pending_wakeup_count}",
        )
    )
    return results


def _count_legacy_queue_items(hub_root: Path) -> dict[str, int]:
    queue_dir = hub_root / LEGACY_PMA_QUEUE_DIR
    if not queue_dir.exists():
        return {"lanes": 0, "items": 0}
    lane_count = 0
    item_count = 0
    for path in queue_dir.glob("*.jsonl"):
        lane_count += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.strip():
                item_count += 1
    return {"lanes": lane_count, "items": item_count}


def _get_queue_idempotency_keys(hub_root: Path) -> list[str]:
    queue_dir = hub_root / LEGACY_PMA_QUEUE_DIR
    if not queue_dir.exists():
        return []
    keys = []
    for path in queue_dir.glob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("idempotency_key"):
                keys.append(str(entry["idempotency_key"]))
    return keys


def verify_queue_parity(hub_root: Path, conn: Any) -> list[ParityCheckResult]:
    legacy_counts = _count_legacy_queue_items(hub_root)
    results = []
    if _table_exists(conn, "orch_queue_items"):
        new_items = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_queue_items"
        ).fetchone()
        new_item_count = int(new_items["cnt"]) if new_items else 0
    else:
        new_item_count = 0
    status = "passed" if legacy_counts.get("items", 0) == new_item_count else "failed"
    results.append(
        ParityCheckResult(
            check_name="queue_items_count",
            status=status,
            legacy_count=legacy_counts.get("items", 0),
            new_count=new_item_count,
            message=f"Queue items: {legacy_counts.get('items', 0)} legacy, {new_item_count} migrated",
        )
    )

    legacy_keys = _get_queue_idempotency_keys(hub_root)
    if legacy_keys and _table_exists(conn, "orch_queue_items"):
        placeholders = ",".join("?" * len(legacy_keys))
        existing = conn.execute(
            f"SELECT idempotency_key FROM orch_queue_items WHERE idempotency_key IN ({placeholders})",
            legacy_keys,
        ).fetchall()
        found_keys = {
            str(row["idempotency_key"]) for row in existing if row["idempotency_key"]
        }
        missing = set(legacy_keys) - found_keys
        results.append(
            ParityCheckResult(
                check_name="queue_idempotency_keys",
                status="passed" if not missing else "failed",
                legacy_count=len(legacy_keys),
                new_count=len(found_keys),
                details={"missing_keys": list(missing)[:5]},
                message=(
                    f"Queue idempotency keys: {len(found_keys)} of {len(legacy_keys)} found"
                    + (f", missing: {list(missing)[:3]}" if missing else "")
                ),
            )
        )

    pending_states = ["pending", "queued", "waiting"]
    if _table_exists(conn, "orch_queue_items"):
        pending_items = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_queue_items WHERE state IN (?, ?, ?)",
            pending_states,
        ).fetchone()
        pending_count = int(pending_items["cnt"]) if pending_items else 0
    else:
        pending_count = 0
    results.append(
        ParityCheckResult(
            check_name="pending_queue_items",
            status="passed",
            legacy_count=legacy_counts.get("items", 0),
            new_count=pending_count,
            details={"pending_states": pending_states},
            message=f"Pending queue items: {pending_count}",
        )
    )
    return results


def _compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _count_legacy_transcripts(hub_root: Path) -> tuple[int, dict[str, int]]:
    transcripts_dir = hub_root / LEGACY_PMA_TRANSCRIPTS_DIR
    if not transcripts_dir.exists():
        return (0, {})
    count = 0
    hashes: dict[str, int] = {}
    for path in sorted(transcripts_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            metadata = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        transcript_id = str(
            metadata.get("turn_id") or metadata.get("managed_turn_id") or ""
        ).strip()
        if not transcript_id:
            continue
        content_path = Path(str(metadata.get("content_path") or ""))
        if not content_path.is_absolute():
            content_path = (path.parent / content_path).resolve()
        try:
            content = content_path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        _compute_text_hash(content)
        hashes[transcript_id] = len(content)
        count += 1
    return (count, hashes)


def verify_transcript_parity(hub_root: Path, conn: Any) -> TranscriptParityResult:
    legacy_count, legacy_hashes = _count_legacy_transcripts(hub_root)
    if _table_exists(conn, "orch_transcript_mirrors"):
        new_count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_transcript_mirrors"
        ).fetchone()
        new_count = int(new_count_row["cnt"]) if new_count_row else 0
    else:
        new_count = 0
    content_hash_matches = 0
    length_mismatches = 0
    sample_mismatches: List[Dict[str, str | int]] = []
    if legacy_hashes and _table_exists(conn, "orch_transcript_mirrors"):
        for tid, expected_len in list(legacy_hashes.items())[:20]:
            row = conn.execute(
                "SELECT text_content, LENGTH(text_content) as len FROM orch_transcript_mirrors WHERE transcript_mirror_id = ?",
                (tid,),
            ).fetchone()
            if row:
                actual_len = int(row["len"]) if row["len"] else 0
                if actual_len == expected_len:
                    content_hash_matches += 1
                else:
                    length_mismatches += 1
                    if len(sample_mismatches) < 3:
                        sample_mismatches.append(
                            {"id": tid, "expected": expected_len, "actual": actual_len}
                        )
    status = (
        "passed" if legacy_count == new_count and length_mismatches == 0 else "failed"
    )
    return TranscriptParityResult(
        check_name="transcript_parity",
        status=status,
        legacy_transcripts=legacy_count,
        new_transcripts=new_count,
        content_hash_matches=content_hash_matches,
        length_mismatches=length_mismatches,
        details={
            "sample_mismatches": sample_mismatches,
            "sample_size": min(20, len(legacy_hashes)),
        },
        message=(
            f"Transcripts: {legacy_count} legacy, {new_count} migrated, "
            f"{content_hash_matches} content hash matches, {length_mismatches} length mismatches"
        ),
    )


def _count_legacy_events(hub_root: Path) -> int:
    return len(_load_json_lines(hub_root / LEGACY_PMA_LIFECYCLE_LOG_PATH))


def verify_event_parity(hub_root: Path, conn: Any) -> list[ParityCheckResult]:
    legacy_count = _count_legacy_events(hub_root)
    results = []
    if _table_exists(conn, "orch_event_projections"):
        new_count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_event_projections"
        ).fetchone()
        new_count = int(new_count_row["cnt"]) if new_count_row else 0
    else:
        new_count = 0
    status = "passed" if legacy_count == new_count else "failed"
    results.append(
        ParityCheckResult(
            check_name="event_projections_count",
            status=status,
            legacy_count=legacy_count,
            new_count=new_count,
            message=f"Event projections: {legacy_count} legacy, {new_count} migrated",
        )
    )
    return results


def _count_legacy_audit_entries(hub_root: Path) -> int:
    return len(_load_json_lines(hub_root / LEGACY_PMA_AUDIT_LOG_PATH))


def verify_audit_parity(hub_root: Path, conn: Any) -> ParityCheckResult:
    legacy_count = _count_legacy_audit_entries(hub_root)
    if _table_exists(conn, "orch_audit_entries"):
        new_count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM orch_audit_entries"
        ).fetchone()
        new_count = int(new_count_row["cnt"]) if new_count_row else 0
    else:
        new_count = 0
    status = "passed" if legacy_count == new_count else "failed"
    return ParityCheckResult(
        check_name="audit_entries_count",
        status=status,
        legacy_count=legacy_count,
        new_count=new_count,
        message=f"Audit entries: {legacy_count} legacy, {new_count} migrated",
    )


def _check_legacy_stores_available(hub_root: Path) -> dict[str, bool]:
    return {
        "threads_db": (hub_root / LEGACY_PMA_THREADS_DB_PATH).exists(),
        "automation": (hub_root / LEGACY_PMA_AUTOMATION_PATH).exists(),
        "queue": (hub_root / LEGACY_PMA_QUEUE_DIR).exists(),
        "transcripts": (hub_root / LEGACY_PMA_TRANSCRIPTS_DIR).exists(),
        "audit_log": (hub_root / LEGACY_PMA_AUDIT_LOG_PATH).exists(),
        "lifecycle": (hub_root / LEGACY_PMA_LIFECYCLE_LOG_PATH).exists(),
    }


def verify_migration(
    hub_root: Path,
    conn: Any,
) -> MigrationVerificationSummary:
    import uuid

    from ..time_utils import now_iso

    run_id = str(uuid.uuid4())
    started_at = now_iso()
    thread_parity = verify_thread_parity(hub_root, conn)
    automation_parity = verify_automation_parity(hub_root, conn)
    queue_parity = verify_queue_parity(hub_root, conn)
    transcript_parity = verify_transcript_parity(hub_root, conn)
    event_parity = verify_event_parity(hub_root, conn)
    audit_parity = verify_audit_parity(hub_root, conn)
    legacy_available = _check_legacy_stores_available(hub_root)
    rollback_available = any(legacy_available.values())
    all_checks: List[ParityCheckResult | TranscriptParityResult] = [
        *thread_parity,
        *automation_parity,
        *queue_parity,
        transcript_parity,
        *event_parity,
        audit_parity,
    ]
    overall_passed = all(
        check.status == "passed"
        for check in all_checks
        if isinstance(check, ParityCheckResult)
    )
    if isinstance(transcript_parity, TranscriptParityResult):
        if transcript_parity.status != "passed":
            overall_passed = False
    recommendations = []
    if not overall_passed:
        failed = [c.check_name for c in all_checks if c.status == "failed"]
        recommendations.append(f"Review failed checks: {', '.join(failed[:5])}")
    if rollback_available:
        recommendations.append(
            "Legacy stores available as read-only fallback. Rollback possible until verification passes."
        )
    else:
        recommendations.append(
            "WARNING: Legacy stores not available. Full migration required before deprecation."
        )
    if overall_passed:
        recommendations.append(
            "Migration verification passed. Legacy stores can be safely deprecated after final review."
        )
    return MigrationVerificationSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=now_iso(),
        status="passed" if overall_passed else "failed",
        thread_parity=thread_parity,
        automation_parity=automation_parity,
        queue_parity=queue_parity,
        transcript_parity=transcript_parity,
        event_parity=event_parity,
        audit_parity=audit_parity,
        overall_passed=overall_passed,
        rollback_available=rollback_available,
        recommendations=recommendations,
    )


__all__ = [
    "MigrationVerificationSummary",
    "ParityCheckResult",
    "TranscriptParityResult",
    "verify_audit_parity",
    "verify_automation_parity",
    "verify_event_parity",
    "verify_migration",
    "verify_queue_parity",
    "verify_thread_parity",
    "verify_transcript_parity",
]
