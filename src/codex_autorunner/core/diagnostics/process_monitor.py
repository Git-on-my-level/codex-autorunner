from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..config_contract import ConfigError
from ..locks import file_lock
from ..runtime import summarize_opencode_lifecycle
from ..text_utils import _parse_iso_timestamp, lock_path_for
from ..utils import atomic_write
from .process_snapshot import ProcessOwnership, collect_processes, enrich_with_ownership

logger = logging.getLogger(__name__)

PROCESS_MONITOR_VERSION = 2
DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS = 120
DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS = 3 * 60 * 60
_DEFAULT_PROCESS_MONITOR_SAMPLE_LIMIT = 120
_PROCESS_MONITOR_FILENAME = "process-monitor.json"

_ABSOLUTE_ALERT_FLOORS = {
    "car_services": 12,
    "managed_runtimes": 12,
    "opencode": 12,
    "codex_app_server": 12,
    "total": 18,
}
_RELATIVE_ALERT_MINIMUMS = {
    "car_services": 6,
    "managed_runtimes": 6,
    "opencode": 6,
    "codex_app_server": 6,
    "total": 10,
}
_DELTA_ALERT_FLOORS = {
    "car_services": 3,
    "managed_runtimes": 3,
    "opencode": 3,
    "codex_app_server": 3,
    "total": 4,
}
_MIN_BASELINE_SAMPLES = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _monitor_path(root: Path) -> Path:
    return root / ".codex-autorunner" / "diagnostics" / _PROCESS_MONITOR_FILENAME


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            return default
    return default


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return int(values[0])
    bounded = min(max(percentile, 0.0), 100.0)
    rank = max(0, math.ceil((bounded / 100.0) * len(values)) - 1)
    ordered = sorted(int(value) for value in values)
    return ordered[min(rank, len(ordered) - 1)]


def _ownership_counts(snapshot_items: list[Any]) -> dict[str, int]:
    counts = {
        ownership.value: 0
        for ownership in (
            ProcessOwnership.MANAGED,
            ProcessOwnership.STALE_RECORD,
            ProcessOwnership.UNTRACKED_LIVE_PROCESS,
            ProcessOwnership.OWNER_MISSING,
        )
    }
    for item in snapshot_items:
        ownership = getattr(item, "ownership", None)
        if ownership is None:
            continue
        counts[ownership.value] = counts.get(ownership.value, 0) + 1
    return counts


def _merge_ownership_counts(*groups: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for group in groups:
        for key, value in group.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _sample_count(
    sample: dict[str, Any],
    key: str,
    *,
    legacy_keys: tuple[str, ...] = (),
    default: int = 0,
) -> int:
    for candidate in (key, *legacy_keys):
        if candidate in sample and sample.get(candidate) is not None:
            return _coerce_int(sample.get(candidate), default=default)
    return default


def capture_process_monitor_sample(root: Path) -> dict[str, Any]:
    resolved_root = Path(root).resolve()
    snapshot = collect_processes()
    snapshot = enrich_with_ownership(snapshot, resolved_root)

    opencode_ownership = _ownership_counts(snapshot.opencode_processes)
    codex_app_server_ownership = _ownership_counts(snapshot.app_server_processes)
    managed_runtime_ownership = _merge_ownership_counts(
        opencode_ownership,
        codex_app_server_ownership,
    )

    sample: dict[str, Any] = {
        "captured_at": _utc_iso(_utc_now()),
        "car_service_count": int(snapshot.car_service_count),
        "managed_runtime_count": int(snapshot.managed_runtime_count),
        "opencode_count": int(snapshot.opencode_count),
        "app_server_count": int(snapshot.codex_app_server_count),
        "codex_app_server_count": int(snapshot.codex_app_server_count),
        "total_count": int(snapshot.total_count),
        "ownership": {
            "managed_runtimes": managed_runtime_ownership,
            "opencode": opencode_ownership,
            "app_server": codex_app_server_ownership,
            "codex_app_server": codex_app_server_ownership,
        },
    }

    try:
        lifecycle = summarize_opencode_lifecycle(resolved_root)
    except (ConfigError, OSError, RuntimeError, TypeError, ValueError):
        lifecycle = {}
    if lifecycle:
        sample["opencode_lifecycle"] = {
            "server_scope": lifecycle.get("server_scope"),
            "counts": dict(lifecycle.get("counts") or {}),
        }

    return sample


@dataclass(frozen=True)
class ProcessMetricSummary:
    current: int
    average: float
    p95: int
    peak: int
    abnormal: bool
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "average": self.average,
            "p95": self.p95,
            "peak": self.peak,
            "abnormal": self.abnormal,
            "reason": self.reason,
        }


class ProcessMonitorStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._path = _monitor_path(self._root)
        self._lock_path = lock_path_for(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return self._default_payload()
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("Failed to read process monitor store %s", self._path)
            return self._default_payload()
        if not isinstance(payload, dict):
            return self._default_payload()
        if _coerce_int(payload.get("version")) != PROCESS_MONITOR_VERSION:
            return self._default_payload()
        samples = payload.get("samples")
        if not isinstance(samples, list):
            samples = []
        cadence_seconds = max(
            1,
            _coerce_int(
                payload.get("cadence_seconds"),
                default=DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
            ),
        )
        window_seconds = max(
            cadence_seconds,
            _coerce_int(
                payload.get("window_seconds"),
                default=DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
            ),
        )
        sample_limit = max(
            1,
            _coerce_int(
                payload.get("sample_limit"),
                default=_DEFAULT_PROCESS_MONITOR_SAMPLE_LIMIT,
            ),
        )
        return {
            "version": PROCESS_MONITOR_VERSION,
            "cadence_seconds": cadence_seconds,
            "window_seconds": window_seconds,
            "sample_limit": sample_limit,
            "samples": [entry for entry in samples if isinstance(entry, dict)],
        }

    def record_sample(
        self,
        sample: dict[str, Any],
        *,
        cadence_seconds: int = DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
        window_seconds: int = DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    ) -> dict[str, Any]:
        resolved_cadence = max(1, int(cadence_seconds))
        resolved_window = max(resolved_cadence, int(window_seconds))
        sample_limit = max(
            _DEFAULT_PROCESS_MONITOR_SAMPLE_LIMIT,
            int(math.ceil(resolved_window / resolved_cadence)) + 4,
        )
        normalized_sample = dict(sample)
        captured_at = _parse_iso_timestamp(normalized_sample.get("captured_at"))
        if captured_at is None:
            captured_at = _utc_now()
            normalized_sample["captured_at"] = _utc_iso(captured_at)
        cutoff = captured_at - timedelta(seconds=resolved_window)

        with file_lock(self._lock_path):
            payload = self.load()
            samples = [
                entry
                for entry in payload.get("samples") or []
                if isinstance(entry, dict)
                and (_parse_iso_timestamp(entry.get("captured_at")) or cutoff) >= cutoff
            ]
            samples.append(normalized_sample)
            samples = samples[-sample_limit:]
            payload = {
                "version": PROCESS_MONITOR_VERSION,
                "cadence_seconds": resolved_cadence,
                "window_seconds": resolved_window,
                "sample_limit": sample_limit,
                "samples": samples,
            }
            atomic_write(
                self._path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )
        return payload

    def recent_samples(
        self,
        *,
        window_seconds: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        payload = self.load()
        samples = [
            entry for entry in payload.get("samples") or [] if isinstance(entry, dict)
        ]
        resolved_window = max(
            1,
            int(
                window_seconds
                or payload.get("window_seconds")
                or DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS
            ),
        )
        cutoff = _utc_now() - timedelta(seconds=resolved_window)
        return [
            entry
            for entry in samples
            if (_parse_iso_timestamp(entry.get("captured_at")) or cutoff) >= cutoff
        ]

    def latest_sample(self) -> Optional[dict[str, Any]]:
        samples = self.recent_samples()
        if not samples:
            return None
        return samples[-1]

    @staticmethod
    def _default_payload() -> dict[str, Any]:
        return {
            "version": PROCESS_MONITOR_VERSION,
            "cadence_seconds": DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
            "window_seconds": DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
            "sample_limit": _DEFAULT_PROCESS_MONITOR_SAMPLE_LIMIT,
            "samples": [],
        }


def _summarize_metric(
    metric: str,
    latest_value: int,
    values: list[int],
) -> ProcessMetricSummary:
    if not values:
        values = [latest_value]
    average = sum(values) / len(values)
    p95 = _percentile(values, 95.0)
    peak = max(values) if values else latest_value
    baseline = values[:-1]
    absolute_floor = _ABSOLUTE_ALERT_FLOORS.get(metric, 12)
    relative_minimum = _RELATIVE_ALERT_MINIMUMS.get(metric, 6)
    delta_floor = _DELTA_ALERT_FLOORS.get(metric, 3)
    abnormal = False
    reason: Optional[str] = None
    if baseline and len(baseline) >= _MIN_BASELINE_SAMPLES:
        baseline_avg = sum(baseline) / len(baseline)
        baseline_p95 = _percentile(baseline, 95.0)
        if latest_value >= absolute_floor:
            abnormal = True
            reason = (
                f"current={latest_value} exceeds the absolute alert floor "
                f"of {absolute_floor}"
            )
        elif (
            latest_value >= relative_minimum
            and latest_value >= baseline_avg + delta_floor
            and latest_value >= baseline_p95 + 1
        ):
            abnormal = True
            reason = (
                f"current={latest_value} is above the trailing baseline "
                f"(avg={baseline_avg:.1f}, p95={baseline_p95})"
            )
    elif latest_value >= absolute_floor:
        abnormal = True
        reason = (
            f"current={latest_value} exceeds the bootstrap alert floor "
            f"of {absolute_floor}"
        )
    return ProcessMetricSummary(
        current=int(latest_value),
        average=float(round(average, 2)),
        p95=int(p95),
        peak=int(peak),
        abnormal=abnormal,
        reason=reason,
    )


def build_process_monitor_summary(
    root: Path,
    *,
    cadence_seconds: int = DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS,
    window_seconds: int = DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS,
    capture_if_stale: bool = True,
) -> dict[str, Any]:
    resolved_root = Path(root).resolve()
    store = ProcessMonitorStore(resolved_root)
    samples = store.recent_samples(window_seconds=window_seconds)
    latest = samples[-1] if samples else None
    latest_at = (
        _parse_iso_timestamp(latest.get("captured_at"))
        if isinstance(latest, dict)
        else None
    )
    if capture_if_stale and (
        latest_at is None
        or (_utc_now() - latest_at).total_seconds() >= max(1, cadence_seconds)
    ):
        payload = store.record_sample(
            capture_process_monitor_sample(resolved_root),
            cadence_seconds=cadence_seconds,
            window_seconds=window_seconds,
        )
        samples = [
            entry for entry in payload.get("samples") or [] if isinstance(entry, dict)
        ]
        latest = samples[-1] if samples else None
        latest_at = (
            _parse_iso_timestamp(latest.get("captured_at"))
            if isinstance(latest, dict)
            else None
        )

    opencode_values = [_coerce_int(entry.get("opencode_count")) for entry in samples]
    car_service_values = [
        _sample_count(entry, "car_service_count") for entry in samples
    ]
    managed_runtime_values = [
        _sample_count(
            entry,
            "managed_runtime_count",
            legacy_keys=("total_count",),
        )
        for entry in samples
    ]
    codex_app_server_values = [
        _sample_count(
            entry,
            "codex_app_server_count",
            legacy_keys=("app_server_count",),
        )
        for entry in samples
    ]
    total_values = [
        _sample_count(
            entry,
            "total_count",
            default=(
                _sample_count(entry, "car_service_count")
                + _sample_count(
                    entry,
                    "managed_runtime_count",
                    legacy_keys=("total_count",),
                )
            ),
        )
        for entry in samples
    ]
    latest_sample = latest if isinstance(latest, dict) else {}
    latest_car_services = _sample_count(latest_sample, "car_service_count")
    latest_managed_runtimes = _sample_count(
        latest_sample,
        "managed_runtime_count",
        legacy_keys=("total_count",),
    )
    latest_opencode = _coerce_int(latest_sample.get("opencode_count"))
    latest_codex_app_server = _sample_count(
        latest_sample,
        "codex_app_server_count",
        legacy_keys=("app_server_count",),
    )
    latest_total = _sample_count(
        latest_sample,
        "total_count",
        default=(latest_car_services + latest_managed_runtimes),
    )

    metrics = {
        "car_services": _summarize_metric(
            "car_services", latest_car_services, car_service_values
        ),
        "managed_runtimes": _summarize_metric(
            "managed_runtimes",
            latest_managed_runtimes,
            managed_runtime_values,
        ),
        "opencode": _summarize_metric("opencode", latest_opencode, opencode_values),
        "codex_app_server": _summarize_metric(
            "codex_app_server",
            latest_codex_app_server,
            codex_app_server_values,
        ),
        "total": _summarize_metric("total", latest_total, total_values),
    }
    primary_metrics = tuple(metrics.values())
    lifecycle_counts = dict(
        ((latest_sample.get("opencode_lifecycle") or {}).get("counts") or {})
    )
    stale_records = _coerce_int(lifecycle_counts.get("stale"))
    status = "warning" if any(metric.abnormal for metric in primary_metrics) else "ok"
    if stale_records > 0:
        status = "warning"
    reasons = [
        metric.reason for metric in primary_metrics if metric.abnormal and metric.reason
    ]
    if stale_records > 0:
        reasons.append(f"OpenCode lifecycle reports stale={stale_records}")
    metrics["app_server"] = metrics["codex_app_server"]

    return {
        "status": status,
        "root": str(resolved_root),
        "path": str(store.path),
        "cadence_seconds": max(1, int(cadence_seconds)),
        "window_seconds": max(1, int(window_seconds)),
        "sample_count": len(samples),
        "latest_at": _utc_iso(latest_at) if latest_at is not None else None,
        "metrics": {name: metric.to_dict() for name, metric in metrics.items()},
        "latest": {
            "car_service_count": latest_car_services,
            "managed_runtime_count": latest_managed_runtimes,
            "opencode_count": latest_opencode,
            "app_server_count": latest_codex_app_server,
            "codex_app_server_count": latest_codex_app_server,
            "total_count": latest_total,
            "ownership": {
                **dict(latest_sample.get("ownership") or {}),
                "app_server": dict(
                    (
                        dict(latest_sample.get("ownership") or {}).get(
                            "codex_app_server"
                        )
                        or {}
                    )
                ),
            },
            "opencode_lifecycle": dict(latest_sample.get("opencode_lifecycle") or {}),
        },
        "reasons": reasons,
    }


__all__ = [
    "DEFAULT_PROCESS_MONITOR_CADENCE_SECONDS",
    "DEFAULT_PROCESS_MONITOR_WINDOW_SECONDS",
    "PROCESS_MONITOR_VERSION",
    "ProcessMetricSummary",
    "ProcessMonitorStore",
    "build_process_monitor_summary",
    "capture_process_monitor_sample",
]
