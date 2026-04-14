from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ...core.diagnostics import build_process_monitor_summary
from ...core.text_utils import _parse_iso_timestamp


@dataclass(frozen=True)
class StatusBlockContext:
    """Shared status fields that both chat surfaces can render."""

    agent: Optional[str] = None
    resume: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    approval_mode: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Any = None
    rate_limits: Optional[dict[str, Any]] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    extra_lines: tuple[str, ...] = ()


def _clean_line(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _append_status_line(lines: list[str], label: str, value: Any) -> None:
    text = _clean_line(value)
    if text is not None:
        lines.append(f"{label}: {text}")


def format_sandbox_policy(sandbox_policy: Any) -> str:
    if sandbox_policy is None:
        return "default"
    if isinstance(sandbox_policy, str):
        return sandbox_policy
    if isinstance(sandbox_policy, dict):
        sandbox_type = sandbox_policy.get("type")
        if isinstance(sandbox_type, str):
            suffix = ""
            if "networkAccess" in sandbox_policy:
                suffix = f", network={sandbox_policy.get('networkAccess')}"
            return f"{sandbox_type}{suffix}"
    return str(sandbox_policy)


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _compute_used_percent(entry: dict[str, Any]) -> Optional[float]:
    remaining = _coerce_number(entry.get("remaining"))
    limit = _coerce_number(entry.get("limit"))
    if remaining is None or limit is None or limit <= 0:
        return None
    used = (limit - remaining) / limit * 100
    return max(min(used, 100.0), 0.0)


def _format_percent(value: Any) -> Optional[str]:
    number = _coerce_number(value)
    if number is None:
        return None
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _rate_limit_window_minutes(
    entry: dict[str, Any],
    section: Optional[str] = None,
) -> Optional[int]:
    for key in (
        "window_minutes",
        "windowMinutes",
        "window_mins",
        "windowMins",
        "period_minutes",
        "periodMinutes",
        "duration_minutes",
        "durationMinutes",
    ):
        value = entry.get(key)
        number = _coerce_number(value)
        if number is not None:
            return max(int(round(number)), 1)
    window_seconds = _coerce_number(
        entry.get("window_seconds", entry.get("windowSeconds"))
    )
    if window_seconds is not None:
        return max(int(round(window_seconds / 60)), 1)
    if section in ("primary", "secondary"):
        return 300 if section == "primary" else 10080
    return None


def _format_rate_limit_window(window_minutes: Optional[int]) -> Optional[str]:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return None
    if window_minutes == 300:
        return "5h"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        parsed = _parse_iso_timestamp(value)
        if parsed is not None:
            return parsed
        try:
            return _coerce_datetime(float(value))
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _format_friendly_time(value: datetime) -> str:
    month = value.strftime("%b")
    day = value.day
    hour = value.strftime("%I").lstrip("0") or "12"
    minute = value.strftime("%M")
    ampm = value.strftime("%p").lower()
    return f"{month} {day}, {hour}:{minute}{ampm}"


def _extract_rate_limit_timestamp(rate_limits: dict[str, Any]) -> Optional[datetime]:
    candidates: list[tuple[int, datetime]] = []
    for section in ("primary", "secondary"):
        entry = rate_limits.get(section)
        if not isinstance(entry, dict):
            continue
        window_minutes = _rate_limit_window_minutes(entry, section) or 0
        for key in (
            "resets_at",
            "resetsAt",
            "reset_at",
            "resetAt",
            "refresh_at",
            "refreshAt",
            "updated_at",
            "updatedAt",
        ):
            if key in entry:
                timestamp = _coerce_datetime(entry.get(key))
                if timestamp is not None:
                    candidates.append((window_minutes, timestamp))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[1]
    for key in (
        "refreshed_at",
        "refreshedAt",
        "refresh_at",
        "refreshAt",
        "updated_at",
        "updatedAt",
        "timestamp",
        "time",
        "as_of",
        "asOf",
    ):
        if key in rate_limits:
            return _coerce_datetime(rate_limits.get(key))
    return None


def _format_rate_limit_refresh(rate_limits: dict[str, Any]) -> Optional[str]:
    refresh_dt = _extract_rate_limit_timestamp(rate_limits)
    if refresh_dt is None:
        return None
    return _format_friendly_time(refresh_dt.astimezone())


def format_rate_limit_lines(rate_limits: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(rate_limits, dict):
        return []
    parts: list[str] = []
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict):
            continue
        used_value = entry.get("used_percent", entry.get("usedPercent"))
        used = _coerce_number(used_value)
        if used is None:
            used = _compute_used_percent(entry)
        used_text = _format_percent(used)
        window_minutes = _rate_limit_window_minutes(entry, key)
        label = _format_rate_limit_window(window_minutes) or key
        if used_text:
            parts.append(f"[{label}: {used_text}]")
    if not parts:
        return []
    refresh_label = _format_rate_limit_refresh(rate_limits)
    if refresh_label:
        parts.append(f"[refresh: {refresh_label}]")
    return [f"Limits: {' '.join(parts)}"]


def extract_rate_limits(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    for key in ("rateLimits", "rate_limits", "limits"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    if "primary" in payload or "secondary" in payload:
        return payload
    return None


def build_status_block_lines(context: StatusBlockContext) -> list[str]:
    """Render the shared runtime status block used by chat surfaces."""

    lines: list[str] = []
    _append_status_line(lines, "Agent", context.agent)
    _append_status_line(lines, "Resume", context.resume)
    _append_status_line(lines, "Model", context.model)
    _append_status_line(lines, "Effort", context.effort)
    _append_status_line(lines, "Approval mode", context.approval_mode)
    _append_status_line(lines, "Approval policy", context.approval_policy)
    lines.append(f"Sandbox policy: {format_sandbox_policy(context.sandbox_policy)}")
    lines.extend(format_rate_limit_lines(context.rate_limits))
    _append_status_line(lines, "Active thread", context.thread_id)
    _append_status_line(lines, "Active turn", context.turn_id)
    lines.extend(line for line in context.extra_lines if _clean_line(line))
    return lines


def format_process_monitor_lines(
    summary: Optional[dict[str, Any]],
    *,
    include_history: bool = False,
    include_header: bool = True,
) -> list[str]:
    if not isinstance(summary, dict):
        return []
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return []
    lines: list[str] = []
    status = _clean_line(summary.get("status")) or "unknown"
    sample_count = _coerce_number(summary.get("sample_count"))
    cadence_seconds = _coerce_number(summary.get("cadence_seconds"))
    window_seconds = _coerce_number(summary.get("window_seconds"))
    header_bits = [f"Process monitor: {status}"]
    if include_header:
        detail_bits: list[str] = []
        if isinstance(window_seconds, float) and window_seconds > 0:
            detail_bits.append(f"window={int(round(window_seconds / 3600))}h")
        if isinstance(sample_count, float) and sample_count > 0:
            detail_bits.append(f"samples={int(sample_count)}")
        if isinstance(cadence_seconds, float) and cadence_seconds > 0:
            detail_bits.append(f"cadence={int(cadence_seconds)}s")
        if detail_bits:
            header_bits.append(f"({' '.join(detail_bits)})")
    lines.append(" ".join(header_bits))

    def _metric_line(label: str, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        current = _coerce_number(payload.get("current"))
        if current is None:
            return None
        line = f"{label}: {int(current) if current.is_integer() else current}"
        if include_history:
            average = _coerce_number(payload.get("average"))
            p95 = _coerce_number(payload.get("p95"))
            peak = _coerce_number(payload.get("peak"))
            history_bits: list[str] = []
            if average is not None:
                history_bits.append(f"avg {average:.1f}")
            if p95 is not None:
                history_bits.append(
                    f"tp95 {int(p95) if p95.is_integer() else f'{p95:.1f}'}"
                )
            if peak is not None:
                history_bits.append(
                    f"peak {int(peak) if peak.is_integer() else f'{peak:.1f}'}"
                )
            if history_bits:
                line += f" ({', '.join(history_bits)})"
        reason = _clean_line(payload.get("reason"))
        if reason and payload.get("abnormal") is True:
            line += " high"
        return line

    for label, key in (
        ("OpenCode", "opencode"),
        ("App server", "app_server"),
        ("Total", "total"),
    ):
        rendered = _metric_line(label, metrics.get(key))
        if rendered:
            lines.append(rendered)

    latest = summary.get("latest")
    if isinstance(latest, dict):
        lifecycle = latest.get("opencode_lifecycle")
        counts = lifecycle.get("counts") if isinstance(lifecycle, dict) else None
        if isinstance(counts, dict) and counts:
            active = int(counts.get("active") or 0)
            stale = int(counts.get("stale") or 0)
            spawned = int(counts.get("spawned_local") or 0)
            reuse = int(counts.get("registry_reuse") or 0)
            lines.append(
                "OpenCode lifecycle: "
                f"active={active} stale={stale} spawned_local={spawned} "
                f"registry_reuse={reuse}"
            )
        ownership = latest.get("ownership")
        if isinstance(ownership, dict):
            opencode = ownership.get("opencode")
            app_server = ownership.get("app_server")
            ownership_parts: list[str] = []
            if isinstance(opencode, dict):
                ownership_parts.append(
                    "opencode "
                    + ", ".join(
                        f"{key}={int(value)}"
                        for key, value in sorted(opencode.items())
                        if _coerce_number(value)
                    )
                )
            if isinstance(app_server, dict):
                ownership_parts.append(
                    "app-server "
                    + ", ".join(
                        f"{key}={int(value)}"
                        for key, value in sorted(app_server.items())
                        if _coerce_number(value)
                    )
                )
            ownership_parts = [
                part for part in ownership_parts if not part.endswith(" ")
            ]
            if ownership_parts:
                lines.append("Ownership: " + " | ".join(ownership_parts))
    return lines


def build_process_monitor_lines_for_root(
    root: Any,
    *,
    include_history: bool,
    capture_if_stale: bool = True,
) -> list[str]:
    if root is None:
        return []
    try:
        summary = build_process_monitor_summary(
            root,
            capture_if_stale=capture_if_stale,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not include_history and summary.get("status") == "ok":
        return []
    return format_process_monitor_lines(
        summary,
        include_history=include_history,
    )
