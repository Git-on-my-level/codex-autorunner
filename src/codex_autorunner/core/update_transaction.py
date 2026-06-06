from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .orchestration.migrations import ORCHESTRATION_SCHEMA_VERSION
from .orchestration.sqlite import resolve_orchestration_sqlite_path
from .utils import resolve_executable

_PRESERVED_STATUS_KEYS = (
    "notify_chat_id",
    "notify_thread_id",
    "notify_reply_to",
    "notify_platform",
    "notify_context",
    "notify_sent_at",
    "phase_timings",
    "last_phase_timing",
)
_MAX_PHASE_TIMINGS = 24

_ORCHESTRATION_DB_SUFFIXES = ("", "-wal", "-shm")
_SNAPSHOT_METADATA = "snapshot.json"
_DEFAULT_MAX_SNAPSHOTS = 1

OpenFileChecker = Callable[[Path], bool]


@dataclass(frozen=True)
class OrchestrationSchemaInfo:
    db_path: str
    db_exists: bool
    current_schema: int
    supported_schema: int
    snapshot_recommended: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrchestrationDbSnapshot:
    snapshot_dir: str
    hub_root: str
    db_path: str
    db_existed: bool
    copied_files: tuple[str, ...]
    current_schema: int
    supported_schema: int
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UpdateSnapshotPruneResult:
    snapshot_root: str
    kept: tuple[str, ...]
    pruned: tuple[str, ...]
    skipped_open: tuple[str, ...]
    skipped_invalid: tuple[str, ...]
    max_snapshots: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UpdateSnapshotDirectory:
    path: Path
    created_at: float


@dataclass(frozen=True)
class UpdateSnapshotTransaction:
    snapshot: OrchestrationDbSnapshot
    prune: UpdateSnapshotPruneResult

    def to_dict(self) -> dict[str, Any]:
        payload = self.snapshot.to_dict()
        payload["prune"] = self.prune.to_dict()
        return payload


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _snapshot_created_at(path: Path) -> float | None:
    metadata = _read_json(path / "orchestration" / _SNAPSHOT_METADATA)
    if metadata is None:
        return None
    try:
        created_at = float(metadata.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return None
    return created_at if created_at > 0 else None


def _has_open_files(path: Path) -> bool:
    lsof = resolve_executable("lsof")
    if lsof is None:
        return True
    try:
        completed = subprocess.run(
            [lsof, "+D", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if completed.returncode not in (0, 1):
        return True
    return completed.returncode == 0


def _positive_int_or_none(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def collect_update_snapshots(
    snapshot_root: Path,
) -> tuple[list[UpdateSnapshotDirectory], tuple[str, ...]]:
    if not snapshot_root.exists():
        return [], ()

    snapshots: list[UpdateSnapshotDirectory] = []
    skipped_invalid: list[str] = []
    for child in snapshot_root.iterdir():
        if not child.is_dir():
            skipped_invalid.append(str(child))
            continue
        try:
            created_at = _snapshot_created_at(child)
            if created_at is None:
                skipped_invalid.append(str(child))
                continue
            snapshots.append(
                UpdateSnapshotDirectory(
                    path=child,
                    created_at=created_at,
                )
            )
        except OSError:
            skipped_invalid.append(str(child))
    return snapshots, tuple(skipped_invalid)


def prune_update_snapshots(
    snapshot_root: Path,
    *,
    current_run_id: str | None = None,
    max_snapshots: int | None = _DEFAULT_MAX_SNAPSHOTS,
    open_file_checker: OpenFileChecker | None = None,
) -> UpdateSnapshotPruneResult:
    max_snapshots = _positive_int_or_none(max_snapshots)
    open_file_checker = (
        _has_open_files if open_file_checker is None else open_file_checker
    )

    snapshots, skipped_invalid = collect_update_snapshots(snapshot_root)

    snapshots_by_age = sorted(
        snapshots,
        key=lambda snapshot: (snapshot.created_at, snapshot.path.name),
        reverse=True,
    )
    kept_paths = {snapshot.path for snapshot in snapshots}
    protected = {snapshot_root / current_run_id} if current_run_id else set()

    prune_paths: set[Path] = set()
    if max_snapshots is not None:
        for snapshot in snapshots_by_age[max_snapshots:]:
            if snapshot.path not in protected:
                prune_paths.add(snapshot.path)

    pruned: list[str] = []
    skipped_open: list[str] = []
    for path in sorted(prune_paths):
        if open_file_checker(path):
            skipped_open.append(str(path))
            continue
        shutil.rmtree(path)
        pruned.append(str(path))
        kept_paths.discard(path)

    kept = tuple(str(path) for path in sorted(kept_paths))
    return UpdateSnapshotPruneResult(
        snapshot_root=str(snapshot_root),
        kept=kept,
        pruned=tuple(pruned),
        skipped_open=tuple(skipped_open),
        skipped_invalid=skipped_invalid,
        max_snapshots=max_snapshots,
    )


def write_update_status_projection(
    path: Path,
    *,
    status: str,
    message: str,
    phase: str | None = None,
    error_type: str | None = None,
    exit_code: int | None = None,
    run_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "message": message,
        "at": time.time(),
    }
    if phase:
        payload["phase"] = phase
    if error_type:
        payload["error_type"] = error_type
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if run_id:
        payload["update_run_id"] = run_id
    existing = _read_json(path) if path.exists() else None
    if isinstance(existing, dict):
        for key in _PRESERVED_STATUS_KEYS:
            if key not in payload and key in existing:
                payload[key] = existing[key]

    if extra:
        phase_timing = extra.pop("phase_timing", None)
        payload.update(extra)
        if isinstance(phase_timing, dict):
            timings = payload.get("phase_timings")
            if not isinstance(timings, list):
                timings = []
            timings.append(phase_timing)
            payload["phase_timings"] = timings[-_MAX_PHASE_TIMINGS:]
            payload["last_phase_timing"] = phase_timing

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def read_orchestration_schema_info(hub_root: Path) -> OrchestrationSchemaInfo:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    current_schema = 0
    db_exists = db_path.exists()
    if db_exists:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='orch_schema_migrations'"
            ).fetchone()
            if row is not None:
                version_row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM orch_schema_migrations"
                ).fetchone()
                if version_row is not None:
                    current_schema = int(version_row[0] or 0)
        finally:
            conn.close()

    return OrchestrationSchemaInfo(
        db_path=str(db_path),
        db_exists=db_exists,
        current_schema=current_schema,
        supported_schema=ORCHESTRATION_SCHEMA_VERSION,
        snapshot_recommended=db_exists
        and current_schema < ORCHESTRATION_SCHEMA_VERSION,
    )


def snapshot_orchestration_db(
    hub_root: Path,
    *,
    snapshot_root: Path,
    run_id: str,
) -> OrchestrationDbSnapshot:
    info = read_orchestration_schema_info(hub_root)
    snapshot_dir = snapshot_root / run_id / "orchestration"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(info.db_path)
    copied: list[str] = []
    if db_path.exists():
        target = snapshot_dir / db_path.name
        source_conn = sqlite3.connect(str(db_path))
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        copied.append(db_path.name)

    snapshot = OrchestrationDbSnapshot(
        snapshot_dir=str(snapshot_dir),
        hub_root=str(hub_root),
        db_path=str(db_path),
        db_existed=info.db_exists,
        copied_files=tuple(copied),
        current_schema=info.current_schema,
        supported_schema=info.supported_schema,
        created_at=time.time(),
    )
    (snapshot_dir / _SNAPSHOT_METADATA).write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return snapshot


def snapshot_orchestration_db_transaction(
    hub_root: Path,
    *,
    snapshot_root: Path,
    run_id: str,
    max_snapshots: int | None = _DEFAULT_MAX_SNAPSHOTS,
) -> UpdateSnapshotTransaction:
    snapshot = snapshot_orchestration_db(
        hub_root,
        snapshot_root=snapshot_root,
        run_id=run_id,
    )
    prune = prune_update_snapshots(
        snapshot_root,
        current_run_id=run_id,
        max_snapshots=max_snapshots,
    )
    return UpdateSnapshotTransaction(snapshot=snapshot, prune=prune)


def restore_orchestration_db_snapshot(snapshot_dir: Path) -> OrchestrationDbSnapshot:
    metadata_path = snapshot_dir / _SNAPSHOT_METADATA
    metadata = _read_json(metadata_path)
    if metadata is None:
        raise RuntimeError(
            f"Missing orchestration DB snapshot metadata: {metadata_path}"
        )

    try:
        snapshot = OrchestrationDbSnapshot(
            snapshot_dir=str(snapshot_dir),
            hub_root=str(metadata["hub_root"]),
            db_path=str(metadata["db_path"]),
            db_existed=bool(metadata["db_existed"]),
            copied_files=tuple(str(item) for item in metadata.get("copied_files", ())),
            current_schema=int(metadata.get("current_schema") or 0),
            supported_schema=int(metadata.get("supported_schema") or 0),
            created_at=float(metadata.get("created_at") or 0.0),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid orchestration DB snapshot metadata: {metadata_path}"
        ) from exc

    db_path = Path(snapshot.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    for suffix in _ORCHESTRATION_DB_SUFFIXES:
        target = Path(f"{db_path}{suffix}")
        source = snapshot_dir / target.name
        if source.exists():
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()

    if not snapshot.db_existed:
        for suffix in _ORCHESTRATION_DB_SUFFIXES:
            target = Path(f"{db_path}{suffix}")
            if target.exists():
                target.unlink()

    return snapshot


def _json_arg(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--extra-json must be a JSON object")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update transaction helper for status and state snapshots."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Write update status projection.")
    status.add_argument("--path", required=True)
    status.add_argument("--status", required=True)
    status.add_argument("--message", required=True)
    status.add_argument("--phase")
    status.add_argument("--error-type")
    status.add_argument("--exit-code", type=int)
    status.add_argument("--run-id")
    status.add_argument("--extra-json")

    schema = sub.add_parser("schema-info", help="Read orchestration schema info.")
    schema.add_argument("--hub-root", required=True)

    snapshot = sub.add_parser("snapshot-db", help="Snapshot orchestration DB files.")
    snapshot.add_argument("--hub-root", required=True)
    snapshot.add_argument("--snapshot-root", required=True)
    snapshot.add_argument("--run-id", required=True)
    snapshot.add_argument(
        "--max-snapshots",
        type=int,
        default=_DEFAULT_MAX_SNAPSHOTS,
        help=(
            "Maximum update snapshot directories to retain. "
            "Use 0 to disable count pruning."
        ),
    )
    prune = sub.add_parser("prune-snapshots", help="Prune update snapshot history.")
    prune.add_argument("--snapshot-root", required=True)
    prune.add_argument("--current-run-id")
    prune.add_argument("--max-snapshots", type=int, default=_DEFAULT_MAX_SNAPSHOTS)

    restore = sub.add_parser("restore-db", help="Restore an orchestration DB snapshot.")
    restore.add_argument("--snapshot-dir", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "status":
        payload = write_update_status_projection(
            Path(args.path),
            status=args.status,
            message=args.message,
            phase=args.phase,
            error_type=args.error_type,
            exit_code=args.exit_code,
            run_id=args.run_id,
            extra=_json_arg(args.extra_json),
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "schema-info":
        info = read_orchestration_schema_info(Path(args.hub_root))
        print(json.dumps(info.to_dict(), sort_keys=True))
        return 0

    if args.command == "snapshot-db":
        transaction = snapshot_orchestration_db_transaction(
            Path(args.hub_root),
            snapshot_root=Path(args.snapshot_root),
            run_id=args.run_id,
            max_snapshots=args.max_snapshots,
        )
        print(json.dumps(transaction.to_dict(), sort_keys=True))
        return 0

    if args.command == "prune-snapshots":
        prune = prune_update_snapshots(
            Path(args.snapshot_root),
            current_run_id=args.current_run_id,
            max_snapshots=args.max_snapshots,
        )
        print(json.dumps(prune.to_dict(), sort_keys=True))
        return 0

    if args.command == "restore-db":
        snapshot = restore_orchestration_db_snapshot(Path(args.snapshot_dir))
        print(json.dumps(snapshot.to_dict(), sort_keys=True))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
