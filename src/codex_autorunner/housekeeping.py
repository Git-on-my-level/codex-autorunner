from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, cast

_MAX_ERROR_SAMPLES = 5


@dataclasses.dataclass(frozen=True)
class HousekeepingRule:
    name: str
    kind: str
    path: str
    glob: Optional[str] = None
    recursive: bool = False
    max_files: Optional[int] = None
    max_total_bytes: Optional[int] = None
    max_age_days: Optional[int] = None
    max_bytes: Optional[int] = None
    max_lines: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class HousekeepingConfig:
    enabled: bool
    interval_seconds: int
    min_file_age_seconds: int
    dry_run: bool
    rules: list[HousekeepingRule]


@dataclasses.dataclass
class HousekeepingRuleResult:
    name: str
    kind: str
    scanned_count: int = 0
    eligible_count: int = 0
    deleted_count: int = 0
    deleted_bytes: int = 0
    truncated_bytes: int = 0
    errors: int = 0
    error_samples: list[str] = dataclasses.field(default_factory=list)
    duration_ms: int = 0


@dataclasses.dataclass
class HousekeepingSummary:
    root: Path
    rules: list[HousekeepingRuleResult]


@dataclasses.dataclass(frozen=True)
class _FileInfo:
    path: Path
    size: int
    mtime: float


def parse_housekeeping_config(raw: Optional[dict]) -> HousekeepingConfig:
    raw = raw if isinstance(raw, dict) else {}
    enabled = bool(raw.get("enabled", False))
    interval_seconds = int(raw.get("interval_seconds", 3600))
    min_file_age_seconds = int(raw.get("min_file_age_seconds", 600))
    dry_run = bool(raw.get("dry_run", False))
    rules_raw = raw.get("rules")
    rules: list[HousekeepingRule] = []
    if isinstance(rules_raw, list):
        for idx, rule in enumerate(rules_raw):
            if not isinstance(rule, dict):
                continue
            name = str(rule.get("name") or f"rule_{idx}")
            rules.append(
                HousekeepingRule(
                    name=name,
                    kind=str(rule.get("kind", "directory")),
                    path=str(rule.get("path", "")),
                    glob=(
                        str(rule.get("glob")) if rule.get("glob") is not None else None
                    ),
                    recursive=bool(rule.get("recursive", False)),
                    max_files=_int_or_none(rule.get("max_files")),
                    max_total_bytes=_int_or_none(rule.get("max_total_bytes")),
                    max_age_days=_int_or_none(rule.get("max_age_days")),
                    max_bytes=_int_or_none(rule.get("max_bytes")),
                    max_lines=_int_or_none(rule.get("max_lines")),
                )
            )
    return HousekeepingConfig(
        enabled=enabled,
        interval_seconds=interval_seconds,
        min_file_age_seconds=min_file_age_seconds,
        dry_run=dry_run,
        rules=rules,
    )


def run_housekeeping_for_roots(
    config: HousekeepingConfig,
    roots: Iterable[Path],
    logger: Optional[logging.Logger] = None,
) -> list[HousekeepingSummary]:
    summaries: list[HousekeepingSummary] = []
    include_absolute = True
    for root in roots:
        summary = run_housekeeping_once(
            config,
            root,
            logger=logger,
            include_absolute=include_absolute,
        )
        summaries.append(summary)
        include_absolute = False
    return summaries


def run_housekeeping_once(
    config: HousekeepingConfig,
    root: Path,
    *,
    logger: Optional[logging.Logger] = None,
    include_absolute: bool = True,
) -> HousekeepingSummary:
    results: list[HousekeepingRuleResult] = []
    for rule in config.rules:
        if not rule.path:
            continue
        if not include_absolute and _is_absolute_path(rule.path):
            continue
        if rule.kind == "directory":
            result = _apply_directory_rule(config, rule, root)
        elif rule.kind == "file":
            result = _apply_file_rule(config, rule, root)
        else:
            continue
        results.append(result)
        if logger is not None:
            log_fields: dict[str, Any] = {
                "name": result.name,
                "kind": result.kind,
                "scanned_count": result.scanned_count,
                "eligible_count": result.eligible_count,
                "deleted_count": result.deleted_count,
                "deleted_bytes": result.deleted_bytes,
                "truncated_bytes": result.truncated_bytes,
                "errors": result.errors,
                "duration_ms": result.duration_ms,
                "dry_run": config.dry_run,
                "root": str(root),
            }
            if result.errors > 0 and result.error_samples:
                log_fields["error_samples"] = result.error_samples
            _log_event(
                logger,
                logging.INFO,
                "housekeeping.rule",
                **log_fields,
            )
    if logger is not None:
        _log_event(
            logger,
            logging.INFO,
            "housekeeping.run",
            root=str(root),
            rules=len(results),
            dry_run=config.dry_run,
        )
    return HousekeepingSummary(root=root, rules=results)


def _apply_directory_rule(
    config: HousekeepingConfig, rule: HousekeepingRule, root: Path
) -> HousekeepingRuleResult:
    start = time.monotonic()
    result = HousekeepingRuleResult(name=rule.name, kind=rule.kind)
    base = _resolve_rule_path(rule.path, root)
    if not base.exists():
        return result
    now = time.time()
    min_age = max(config.min_file_age_seconds, 0)
    files = _collect_files(base, rule, result)
    result.scanned_count = len(files)
    if not files:
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result
    eligible = [f for f in files if now - f.mtime >= min_age]
    result.eligible_count = len(eligible)
    deleted: set[Path] = set()
    deleted_bytes = 0
    errors = 0

    def delete_file(entry: _FileInfo) -> None:
        nonlocal deleted_bytes, errors
        if entry.path in deleted:
            return
        if not config.dry_run:
            try:
                entry.path.unlink()
            except OSError as e:
                errors += 1
                _add_error_sample(result, "unlink", entry.path, e)
                return
        deleted.add(entry.path)
        deleted_bytes += entry.size

    total_files = len(files)
    total_bytes = sum(f.size for f in files)

    if rule.max_age_days is not None:
        cutoff = now - (max(rule.max_age_days, 0) * 86400)
        for entry in sorted(eligible, key=lambda item: item.mtime):
            if entry.mtime <= cutoff:
                delete_file(entry)

    if deleted:
        files = [f for f in files if f.path not in deleted]
        eligible = [f for f in eligible if f.path not in deleted]
        total_files = len(files)
        total_bytes = sum(f.size for f in files)

    if rule.max_files is not None and total_files > rule.max_files:
        to_remove = total_files - rule.max_files
        for entry in sorted(eligible, key=lambda item: item.mtime):
            if to_remove <= 0:
                break
            delete_file(entry)
            to_remove -= 1

    if deleted:
        files = [f for f in files if f.path not in deleted]
        eligible = [f for f in eligible if f.path not in deleted]
        total_files = len(files)
        total_bytes = sum(f.size for f in files)

    if rule.max_total_bytes is not None and total_bytes > rule.max_total_bytes:
        overage = total_bytes - rule.max_total_bytes
        for entry in sorted(eligible, key=lambda item: item.mtime):
            if overage <= 0:
                break
            delete_file(entry)
            overage -= entry.size

    result.deleted_count = len(deleted)
    result.deleted_bytes = deleted_bytes
    result.errors = errors
    if deleted and not config.dry_run:
        _prune_empty_dirs(base)
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def _apply_file_rule(
    config: HousekeepingConfig, rule: HousekeepingRule, root: Path
) -> HousekeepingRuleResult:
    start = time.monotonic()
    result = HousekeepingRuleResult(name=rule.name, kind=rule.kind)
    path = _resolve_rule_path(rule.path, root)
    if not path.exists():
        return result
    try:
        stat = path.stat()
    except OSError as e:
        result.errors = 1
        _add_error_sample(result, "stat", path, e)
        return result
    if not path.is_file():
        return result
    result.scanned_count = 1
    now = time.time()
    min_age = max(config.min_file_age_seconds, 0)
    if now - stat.st_mtime < min_age:
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result
    result.eligible_count = 1
    if rule.max_lines is not None:
        truncated = _truncate_lines(
            path,
            rule.max_lines,
            dry_run=config.dry_run,
            result=result,
        )
        result.truncated_bytes += truncated
    if rule.max_bytes is not None:
        truncated = _truncate_bytes(
            path,
            rule.max_bytes,
            dry_run=config.dry_run,
            result=result,
        )
        result.truncated_bytes += truncated
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def _collect_files(
    base: Path, rule: HousekeepingRule, result: Optional[HousekeepingRuleResult] = None
) -> list[_FileInfo]:
    results: list[_FileInfo] = []
    glob_pattern = rule.glob or "*"
    iterator = base.rglob(glob_pattern) if rule.recursive else base.glob(glob_pattern)
    for path in iterator:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError as e:
            if result is not None:
                _add_error_sample(result, "stat", path, e)
            continue
        results.append(_FileInfo(path=path, size=stat.st_size, mtime=stat.st_mtime))
    return results


def _resolve_rule_path(path: str, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _is_absolute_path(path: str) -> bool:
    return Path(path).expanduser().is_absolute()


def _truncate_bytes(
    path: Path,
    max_bytes: int,
    *,
    dry_run: bool,
    result: Optional[HousekeepingRuleResult] = None,
) -> int:
    if max_bytes <= 0:
        return 0
    try:
        size = path.stat().st_size
    except OSError as e:
        if result is not None:
            result.errors += 1
            _add_error_sample(result, "truncate_bytes", path, e)
        return 0
    if size <= max_bytes:
        return 0
    truncated = size - max_bytes
    if dry_run:
        return truncated
    try:
        with path.open("rb") as handle:
            handle.seek(-max_bytes, os.SEEK_END)
            payload = handle.read()
        _atomic_write_bytes(path, payload)
        return truncated
    except OSError as e:
        if result is not None:
            result.errors += 1
            _add_error_sample(result, "truncate_bytes", path, e)
        return 0


def _truncate_lines(
    path: Path,
    max_lines: int,
    *,
    dry_run: bool,
    result: Optional[HousekeepingRuleResult] = None,
) -> int:
    if max_lines <= 0:
        return 0
    try:
        size = path.stat().st_size
    except OSError as e:
        if result is not None:
            result.errors += 1
            _add_error_sample(result, "truncate_lines", path, e)
        return 0
    lines: deque[bytes] = deque(maxlen=max_lines)
    try:
        with path.open("rb") as handle:
            for line in handle:
                lines.append(line)
    except OSError as e:
        if result is not None:
            result.errors += 1
            _add_error_sample(result, "truncate_lines", path, e)
        return 0
    payload = b"".join(lines)
    if len(payload) >= size:
        return 0
    if dry_run:
        return size - len(payload)
    try:
        _atomic_write_bytes(path, payload)
    except OSError as e:
        if result is not None:
            result.errors += 1
            _add_error_sample(result, "truncate_lines", path, e)
        return 0
    return size - len(payload)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("wb") as handle:
        handle.write(payload)
    tmp_path.replace(path)


def _prune_empty_dirs(base: Path) -> None:
    if not base.exists() or not base.is_dir():
        return
    for root, dirs, _files in os.walk(base, topdown=False):
        for name in dirs:
            candidate = Path(root) / name
            try:
                if not any(candidate.iterdir()):
                    candidate.rmdir()
            except OSError:
                continue


def _add_error_sample(
    result: HousekeepingRuleResult, operation: str, path: Path, exc: OSError
) -> None:
    if len(result.error_samples) >= _MAX_ERROR_SAMPLES:
        return
    exc_info = f"{type(exc).__name__}"
    if exc.strerror:
        exc_info += f": {exc.strerror}"
    result.error_samples.append(f"{operation} {path}: {exc_info}")


def _int_or_none(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _log_event(
    logger: logging.Logger, level: int, event: str, **fields: object
) -> None:
    class _LogEvent(Protocol):
        def __call__(
            self,
            logger: logging.Logger,
            level: int,
            event: str,
            *,
            exc: Optional[Exception] = None,
            **fields: Any,
        ) -> None: ...

    raw_exc = fields.pop("exc", None)
    exc_value = raw_exc if isinstance(raw_exc, Exception) else None
    if raw_exc is not None and exc_value is None:
        fields["exc_info"] = raw_exc
    try:
        from .core.logging_utils import log_event
    except Exception:
        logger.log(level, f"{event} {fields}")
        return
    log_event_typed = cast(_LogEvent, log_event)
    log_event_typed(logger, level, event, exc=exc_value, **fields)
