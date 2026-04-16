from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .process_snapshot import (
    ProcessCategory,
    collect_processes,
    enrich_with_ownership,
)


@dataclass
class CpuSample:
    car_service_cpu_percent: float = 0.0
    car_service_rss_mb: float = 0.0
    opencode_cpu_percent: float = 0.0
    opencode_rss_mb: float = 0.0
    app_server_cpu_percent: float = 0.0
    app_server_rss_mb: float = 0.0
    aggregate_cpu_percent: float = 0.0
    aggregate_rss_mb: float = 0.0
    per_process: list[dict[str, Any]] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    bounded = min(max(pct, 0.0), 100.0)
    rank = max(0, math.ceil((bounded / 100.0) * len(values)) - 1)
    ordered = sorted(values)
    return ordered[min(rank, len(ordered) - 1)]


def sample_cpu_for_pids(
    pids: list[int],
) -> dict[int, tuple[float, float]]:
    if not pids:
        return {}
    pid_strs = ",".join(str(p) for p in pids)
    try:
        proc = subprocess.run(
            ["ps", "-p", pid_strs, "-o", "pid=", "-o", "%cpu=", "-o", "rss="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    result: dict[int, tuple[float, float]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            cpu_pct = float(parts[1])
            rss_kb = float(parts[2])
        except (ValueError, IndexError):
            continue
        result[pid] = (cpu_pct, rss_kb / 1024.0)
    return result


def collect_cpu_sample(
    *,
    repo_root: Optional[Path] = None,
    owned_categories: Optional[list[ProcessCategory]] = None,
    ps_output_getter: Optional[Callable[[], str]] = None,
    pid_cpu_getter: Optional[
        Callable[[list[int]], dict[int, tuple[float, float]]]
    ] = None,
) -> CpuSample:
    if owned_categories is None:
        owned_categories = [
            ProcessCategory.CAR_SERVICE,
            ProcessCategory.OPENCODE,
            ProcessCategory.APP_SERVER,
        ]

    snapshot = collect_processes(ps_output_getter=ps_output_getter)
    if repo_root is not None:
        snapshot = enrich_with_ownership(snapshot, repo_root)

    _sample_cpu = pid_cpu_getter or sample_cpu_for_pids

    category_procs: dict[ProcessCategory, list] = {
        ProcessCategory.CAR_SERVICE: snapshot.car_service_processes,
        ProcessCategory.OPENCODE: snapshot.opencode_processes,
        ProcessCategory.APP_SERVER: snapshot.app_server_processes,
    }

    all_owned_pids: list[int] = []
    per_category_pids: dict[ProcessCategory, list[int]] = {}
    for cat in owned_categories:
        procs = category_procs.get(cat, [])
        pids = [p.pid for p in procs]
        per_category_pids[cat] = pids
        all_owned_pids.extend(pids)

    cpu_by_pid = _sample_cpu(all_owned_pids) if all_owned_pids else {}

    def _cat_metrics(cat: ProcessCategory) -> tuple[float, float]:
        cpu_total = 0.0
        rss_total = 0.0
        for pid in per_category_pids.get(cat, []):
            if pid in cpu_by_pid:
                cpu_total += cpu_by_pid[pid][0]
                rss_total += cpu_by_pid[pid][1]
        return cpu_total, rss_total

    car_cpu, car_rss = _cat_metrics(ProcessCategory.CAR_SERVICE)
    opencode_cpu, opencode_rss = _cat_metrics(ProcessCategory.OPENCODE)
    app_server_cpu, app_server_rss = _cat_metrics(ProcessCategory.APP_SERVER)

    per_process: list[dict[str, Any]] = []
    for cat in owned_categories:
        for proc in category_procs.get(cat, []):
            cpu, rss = cpu_by_pid.get(proc.pid, (0.0, 0.0))
            per_process.append(
                {
                    "pid": proc.pid,
                    "command": proc.command,
                    "category": cat.value,
                    "cpu_percent": round(cpu, 3),
                    "rss_mb": round(rss, 3),
                }
            )

    return CpuSample(
        car_service_cpu_percent=round(car_cpu, 3),
        car_service_rss_mb=round(car_rss, 3),
        opencode_cpu_percent=round(opencode_cpu, 3),
        opencode_rss_mb=round(opencode_rss, 3),
        app_server_cpu_percent=round(app_server_cpu, 3),
        app_server_rss_mb=round(app_server_rss, 3),
        aggregate_cpu_percent=round(car_cpu + opencode_cpu + app_server_cpu, 3),
        aggregate_rss_mb=round(car_rss + opencode_rss + app_server_rss, 3),
        per_process=per_process,
    )


def aggregate_samples(
    samples: list[CpuSample],
) -> dict[str, Any]:
    if not samples:
        return {
            "car_owned_cpu_mean_percent": 0.0,
            "car_owned_cpu_max_percent": 0.0,
            "car_owned_cpu_p95_percent": 0.0,
            "car_owned_cpu_samples": 0,
            "car_owned_memory_mean_mb": 0.0,
            "car_owned_memory_max_mb": 0.0,
        }

    cpu_values = [s.aggregate_cpu_percent for s in samples]
    rss_values = [s.aggregate_rss_mb for s in samples]

    return {
        "car_owned_cpu_mean_percent": round(sum(cpu_values) / len(cpu_values), 3),
        "car_owned_cpu_max_percent": round(max(cpu_values), 3),
        "car_owned_cpu_p95_percent": round(_percentile(cpu_values, 95.0), 3),
        "car_owned_cpu_samples": len(samples),
        "car_owned_memory_mean_mb": round(sum(rss_values) / len(rss_values), 3),
        "car_owned_memory_max_mb": round(max(rss_values), 3),
    }


def compute_per_process_aggregates(
    samples: list[CpuSample],
) -> list[dict[str, Any]]:
    if not samples:
        return []

    by_key: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for sample in samples:
        for proc in sample.per_process:
            key = (proc["pid"], proc["category"])
            by_key.setdefault(key, []).append(proc)

    results: list[dict[str, Any]] = []
    for (_pid, category), proc_samples in by_key.items():
        cpu_values = [p["cpu_percent"] for p in proc_samples]
        rss_values = [p["rss_mb"] for p in proc_samples]
        command = proc_samples[0]["command"]
        results.append(
            {
                "alias": "",
                "command": command,
                "category": category,
                "cpu_mean_percent": round(sum(cpu_values) / len(cpu_values), 3),
                "cpu_max_percent": round(max(cpu_values), 3),
                "memory_mean_mb": round(sum(rss_values) / len(rss_values), 3),
                "memory_max_mb": round(max(rss_values), 3),
            }
        )
    return results


def evaluate_signoff(
    aggregate: dict[str, Any],
    *,
    hard_budget: Optional[dict[str, Any]] = None,
    comparison_budget: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    actual_cpu = aggregate.get("car_owned_cpu_mean_percent", 0.0)

    if hard_budget and hard_budget.get("enabled"):
        threshold = hard_budget.get("max_aggregate_cpu_percent")
        if threshold is not None:
            passed = actual_cpu <= threshold
            return {
                "passed": passed,
                "budget_type": "hard",
                "budget_threshold_percent": threshold,
                "actual_aggregate_cpu_percent": actual_cpu,
                "message": (
                    f"{'PASS' if passed else 'FAIL'}: aggregate CPU "
                    f"{actual_cpu:.3f}% vs hard budget {threshold}%"
                ),
            }

    if (
        comparison_budget
        and comparison_budget.get("max_aggregate_cpu_percent") is not None
    ):
        threshold = comparison_budget["max_aggregate_cpu_percent"]
        passed = actual_cpu <= threshold
        return {
            "passed": passed,
            "budget_type": "comparison",
            "budget_threshold_percent": threshold,
            "actual_aggregate_cpu_percent": actual_cpu,
            "message": (
                f"{'PASS' if passed else 'FAIL'}: aggregate CPU "
                f"{actual_cpu:.3f}% vs comparison budget {threshold}%"
            ),
        }

    return {
        "passed": True,
        "budget_type": "none",
        "budget_threshold_percent": None,
        "actual_aggregate_cpu_percent": actual_cpu,
        "message": f"No budget gate; aggregate CPU {actual_cpu:.3f}%",
    }


__all__ = [
    "CpuSample",
    "aggregate_samples",
    "collect_cpu_sample",
    "compute_per_process_aggregates",
    "evaluate_signoff",
    "sample_cpu_for_pids",
]
