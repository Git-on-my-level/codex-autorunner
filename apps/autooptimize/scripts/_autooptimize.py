from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class AutoOptimizePaths:
    state_dir: Path
    artifact_dir: Path
    run_path: Path
    iterations_path: Path
    lock_path: Path
    summary_md_path: Path
    summary_png_path: Path


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_paths() -> AutoOptimizePaths:
    state_dir = _require_env_path("CAR_APP_STATE_DIR")
    artifact_dir = _require_env_path("CAR_APP_ARTIFACT_DIR")
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return AutoOptimizePaths(
        state_dir=state_dir,
        artifact_dir=artifact_dir,
        run_path=state_dir / "run.json",
        iterations_path=state_dir / "iterations.jsonl",
        lock_path=state_dir / ".autooptimize.lock",
        summary_md_path=artifact_dir / "summary.md",
        summary_png_path=artifact_dir / "summary.png",
    )


def _require_env_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return Path(value).expanduser().resolve()


@contextmanager
def locked_state(paths: AutoOptimizePaths) -> Iterator[None]:
    paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with paths.lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    atomic_write_text(path, ("\n".join(lines) + "\n") if lines else "")


def load_run(paths: AutoOptimizePaths) -> dict[str, Any]:
    if not paths.run_path.exists():
        raise RuntimeError(f"Run state not found: {paths.run_path}")
    payload = json.loads(paths.run_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("run.json must contain a JSON object")
    return payload


def load_iterations(paths: AutoOptimizePaths) -> list[dict[str, Any]]:
    if not paths.iterations_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        paths.iterations_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        text = raw_line.strip()
        if not text:
            continue
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"iterations.jsonl line {line_no} must contain a JSON object"
            )
        rows.append(payload)
    return rows


def ensure_direction(direction: str) -> str:
    normalized = str(direction).strip().lower()
    if normalized not in {"higher", "lower"}:
        raise RuntimeError(
            f"Metric direction must be 'higher' or 'lower', got {direction!r}"
        )
    return normalized


def ensure_guard_status(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"pass", "fail", "not_run"}:
        raise RuntimeError(
            f"guard-status must be pass, fail, or not_run, got {value!r}"
        )
    return normalized


def ensure_decision(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"keep", "discard", "pivot", "blocked"}:
        raise RuntimeError(
            f"decision must be keep, discard, pivot, or blocked, got {value!r}"
        )
    return normalized


def compare_is_better(candidate: float, current: float, direction: str) -> bool:
    direction = ensure_direction(direction)
    return candidate > current if direction == "higher" else candidate < current


def coerce_float(value: Any, *, label: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric, got {value!r}") from exc
    if not math.isfinite(numeric):
        raise RuntimeError(f"{label} must be finite, got {value!r}")
    return numeric


def normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_metric_unit(run: dict[str, Any], unit: str | None) -> str | None:
    metric = run.get("primary_metric") or {}
    expected = normalize_optional_text(metric.get("unit"))
    provided = normalize_optional_text(unit)
    if expected and provided and expected != provided:
        raise RuntimeError(
            f"Metric unit mismatch: run expects {expected!r}, got {provided!r}"
        )
    if expected:
        return expected
    if provided:
        metric["unit"] = provided
        run["primary_metric"] = metric
    return provided


def compute_best_record(
    run: dict[str, Any], rows: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    direction = ensure_direction((run.get("primary_metric") or {}).get("direction"))
    baseline = run.get("baseline")
    best_record: dict[str, Any] | None = None
    if isinstance(baseline, dict):
        best_record = {
            "value": coerce_float(baseline.get("value"), label="baseline.value"),
            "unit": normalize_optional_text(baseline.get("unit")),
            "summary": normalize_optional_text(baseline.get("summary")),
            "source": "baseline",
            "iteration": None,
            "ticket": None,
            "created_at": baseline.get("created_at"),
        }

    for row in sorted(rows, key=lambda item: int(item.get("iteration", 0))):
        if ensure_decision(str(row.get("decision", ""))) != "keep":
            continue
        candidate_value = coerce_float(row.get("metric_value"), label="metric_value")
        candidate = {
            "value": candidate_value,
            "unit": normalize_optional_text(row.get("unit")),
            "summary": normalize_optional_text(row.get("summary")),
            "source": "iteration",
            "iteration": int(row["iteration"]),
            "ticket": normalize_optional_text(row.get("ticket")),
            "created_at": row.get("created_at"),
        }
        if best_record is None or compare_is_better(
            candidate_value,
            coerce_float(best_record.get("value"), label="best.value"),
            direction,
        ):
            best_record = candidate
    return best_record


def build_status_payload(
    run: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    metric = run.get("primary_metric") or {}
    direction = ensure_direction(metric.get("direction"))
    baseline = run.get("baseline")
    best = run.get("best")
    baseline_value = (
        coerce_float(baseline.get("value"), label="baseline.value")
        if isinstance(baseline, dict)
        else None
    )
    best_value = (
        coerce_float(best.get("value"), label="best.value")
        if isinstance(best, dict)
        else None
    )
    absolute_delta = (
        None
        if baseline_value is None or best_value is None
        else best_value - baseline_value
    )
    percent_delta = None
    if absolute_delta is not None and baseline_value not in (None, 0):
        percent_delta = (absolute_delta / baseline_value) * 100.0
    improvement_absolute = (
        None
        if absolute_delta is None
        else absolute_delta if direction == "higher" else -absolute_delta
    )
    improvement_percent = (
        None
        if percent_delta is None
        else percent_delta if direction == "higher" else -percent_delta
    )

    last_decision = rows[-1]["decision"] if rows else None
    guard_status_counts = dict(Counter(str(row.get("guard_status")) for row in rows))
    discard_tail = 0
    for row in reversed(rows):
        if row.get("decision") == "discard":
            discard_tail += 1
            continue
        break

    stop_conditions = run.get("stop_conditions") or {}
    hints: list[str] = []
    max_iterations = stop_conditions.get("max_iterations")
    if max_iterations is not None:
        remaining = int(max_iterations) - len(rows)
        if remaining <= 0:
            hints.append("max_iterations reached")
        else:
            hints.append(f"{remaining} iteration(s) remain before max_iterations")
    max_discards = stop_conditions.get("max_consecutive_discards")
    if max_discards is not None:
        remaining_discards = int(max_discards) - discard_tail
        if remaining_discards <= 0:
            hints.append("max_consecutive_discards reached")
        else:
            hints.append(
                f"{remaining_discards} consecutive discard(s) remain before stop threshold"
            )
    if baseline is None:
        hints.append("baseline not recorded yet")
    elif not rows:
        hints.append("no iterations recorded yet")

    return {
        "run_id": run.get("run_id"),
        "goal": run.get("goal"),
        "metric": metric,
        "baseline": baseline,
        "best": best,
        "absolute_delta": absolute_delta,
        "percent_delta": percent_delta,
        "improvement_absolute": improvement_absolute,
        "improvement_percent": improvement_percent,
        "iterations_count": len(rows),
        "last_decision": last_decision,
        "guard_status_counts": guard_status_counts,
        "stop_condition_hints": hints,
    }


def validate_state(paths: AutoOptimizePaths) -> list[str]:
    errors: list[str] = []
    try:
        run = load_run(paths)
    except Exception as exc:
        return [str(exc)]

    try:
        direction = ensure_direction((run.get("primary_metric") or {}).get("direction"))
    except Exception as exc:
        errors.append(str(exc))
        direction = "higher"

    baseline = run.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, dict):
            errors.append("run.json baseline must be null or an object")
        else:
            try:
                coerce_float(baseline.get("value"), label="baseline.value")
            except Exception as exc:
                errors.append(str(exc))
            expected_unit = normalize_optional_text(
                (run.get("primary_metric") or {}).get("unit")
            )
            baseline_unit = normalize_optional_text(baseline.get("unit"))
            if expected_unit and baseline_unit and baseline_unit != expected_unit:
                errors.append(
                    "run.json baseline unit does not match primary metric unit"
                )

    try:
        rows = load_iterations(paths)
    except Exception as exc:
        return errors + [str(exc)]

    seen_iterations: set[int] = set()
    for row in rows:
        try:
            iteration = int(row.get("iteration"))
        except (TypeError, ValueError):
            errors.append(f"Invalid iteration value in row: {row!r}")
            continue
        if iteration in seen_iterations:
            errors.append(f"Duplicate iteration number found: {iteration}")
        seen_iterations.add(iteration)
        try:
            coerce_float(row.get("metric_value"), label=f"iteration {iteration} value")
            ensure_decision(str(row.get("decision", "")))
            ensure_guard_status(str(row.get("guard_status", "")))
        except Exception as exc:
            errors.append(str(exc))
        expected_unit = normalize_optional_text(
            (run.get("primary_metric") or {}).get("unit")
        )
        row_unit = normalize_optional_text(row.get("unit"))
        if expected_unit and row_unit and row_unit != expected_unit:
            errors.append(
                f"Iteration {iteration} unit mismatch: expected {expected_unit!r}, got {row_unit!r}"
            )

    try:
        expected_best = compute_best_record(run, rows)
    except Exception as exc:
        errors.append(str(exc))
        expected_best = None
    actual_best = run.get("best")
    if expected_best is None and actual_best is not None:
        errors.append(
            "run.json best should be null when no baseline or kept iterations exist"
        )
    if expected_best is not None:
        if not isinstance(actual_best, dict):
            errors.append("run.json best is missing or invalid")
        else:
            try:
                actual_value = coerce_float(
                    actual_best.get("value"), label="best.value"
                )
            except Exception as exc:
                errors.append(str(exc))
            else:
                if not math.isclose(
                    actual_value,
                    expected_best["value"],
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                ):
                    errors.append(
                        f"run.json best value mismatch: expected {expected_best['value']}, got {actual_value}"
                    )
            if normalize_optional_text(
                actual_best.get("unit")
            ) != normalize_optional_text(expected_best.get("unit")):
                errors.append("run.json best unit does not match derived best unit")
            if actual_best.get("source") != expected_best.get("source"):
                errors.append("run.json best source does not match derived best source")
            if actual_best.get("iteration") != expected_best.get("iteration"):
                errors.append(
                    "run.json best iteration does not match derived best iteration"
                )

    try:
        paths.artifact_dir.mkdir(parents=True, exist_ok=True)
        probe_path = paths.artifact_dir / ".write-test"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink()
    except OSError as exc:
        errors.append(f"Artifacts directory is not writable: {exc}")

    if not errors:
        try:
            ensure_direction(direction)
        except Exception:
            pass
    return errors


def format_metric_value(value: float | None, unit: str | None) -> str:
    if value is None:
        return "n/a"
    unit_suffix = f" {unit}" if unit else ""
    return f"{value:.3f}{unit_suffix}"
