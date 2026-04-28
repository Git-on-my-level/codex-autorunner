from __future__ import annotations

import json
from collections import Counter
from html import escape

from _autooptimize import (
    atomic_write_text,
    build_paths,
    build_status_payload,
    format_metric_value,
    load_iterations,
    load_run,
    maybe_write_png,
    validate_state,
)


def main() -> int:
    paths = build_paths()
    errors = validate_state(paths)
    if errors:
        raise RuntimeError("; ".join(errors))

    run = load_run(paths)
    rows = load_iterations(paths)
    payload = build_status_payload(run, rows)
    markdown = build_summary_markdown(payload, rows)
    svg = build_summary_svg(payload, rows)

    atomic_write_text(paths.summary_md_path, markdown)
    atomic_write_text(paths.summary_svg_path, svg)
    wrote_png = maybe_write_png(paths.summary_svg_path, paths.summary_png_path)

    print(
        json.dumps(
            {
                "summary_md": str(paths.summary_md_path),
                "summary_svg": str(paths.summary_svg_path),
                "summary_png": str(paths.summary_png_path) if wrote_png else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_summary_markdown(payload: dict, rows: list[dict]) -> str:
    metric = payload["metric"]
    baseline = payload["baseline"]
    best = payload["best"]
    counts = Counter(row["decision"] for row in rows)
    guard_counts = payload.get("guard_status_counts") or {}
    lines = [
        "# AutoOptimize Summary",
        "",
        f"- Goal: {payload['goal']}",
        f"- Metric: {metric.get('name')} ({metric.get('direction')}, unit={metric.get('unit') or 'n/a'})",
        f"- Baseline: {format_metric_value(baseline.get('value') if isinstance(baseline, dict) else None, metric.get('unit'))}",
        f"- Best: {format_metric_value(best.get('value') if isinstance(best, dict) else None, metric.get('unit'))}",
        f"- Iterations: {payload['iterations_count']}",
        f"- Last decision: {payload['last_decision'] or 'n/a'}",
    ]
    if payload["improvement_absolute"] is not None:
        delta_text = f"{payload['improvement_absolute']:.3f}"
        if metric.get("unit"):
            delta_text += f" {metric.get('unit')}"
        if payload["improvement_percent"] is not None:
            delta_text += f" ({payload['improvement_percent']:.2f}%)"
        lines.append(f"- Improvement vs baseline: {delta_text}")
    if guard_counts:
        parts = [f"{name}={guard_counts[name]}" for name in sorted(guard_counts)]
        lines.append(f"- Guard status counts: {', '.join(parts)}")
    if payload["stop_condition_hints"]:
        lines.extend(["", "## Stop-condition hints", ""])
        for hint in payload["stop_condition_hints"]:
            lines.append(f"- {hint}")
    lines.extend(
        [
            "",
            "## Decision counts",
            "",
            f"- keep: {counts.get('keep', 0)}",
            f"- discard: {counts.get('discard', 0)}",
            f"- pivot: {counts.get('pivot', 0)}",
            f"- blocked: {counts.get('blocked', 0)}",
            "",
            "## Iterations",
            "",
        ]
    )
    if not rows:
        lines.append("- No iterations recorded.")
    for row in rows:
        milestone = f" | milestone={row['milestone']}" if row.get("milestone") else ""
        lines.append(
            f"- Iteration {row['iteration']}: {row['decision']} | value={row['metric_value']}"
            f"{' ' + row['unit'] if row.get('unit') else ''} | ticket={row['ticket']}{milestone}"
        )
    lines.append("")
    return "\n".join(lines)


def build_summary_svg(payload: dict, rows: list[dict]) -> str:
    metric = payload["metric"]
    baseline = payload["baseline"]
    points = []
    if isinstance(baseline, dict):
        points.append(
            {
                "label": "B",
                "value": float(baseline["value"]),
                "decision": "baseline",
                "milestone": None,
            }
        )
    for row in rows:
        points.append(
            {
                "label": f"I{row['iteration']}",
                "value": float(row["metric_value"]),
                "decision": row["decision"],
                "milestone": row.get("milestone"),
            }
        )

    chart_left = 70
    chart_top = 250
    chart_width = 860
    chart_height = 210
    width = 1000
    height = 580
    values = [point["value"] for point in points] or [0.0]
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 1.0
        max_value += 1.0
    padding = max((max_value - min_value) * 0.1, 0.5)
    min_value -= padding
    max_value += padding

    def x_for(index: int) -> float:
        if len(points) <= 1:
            return chart_left + chart_width / 2
        return chart_left + (chart_width * index / (len(points) - 1))

    def y_for(value: float) -> float:
        ratio = (value - min_value) / (max_value - min_value)
        return chart_top + chart_height - (ratio * chart_height)

    polyline = " ".join(
        f"{x_for(index):.1f},{y_for(point['value']):.1f}"
        for index, point in enumerate(points)
    )
    counts = Counter(row["decision"] for row in rows)
    guard_counts = payload.get("guard_status_counts") or {}
    best = payload["best"] if isinstance(payload["best"], dict) else {}
    best_text = format_metric_value(best.get("value"), metric.get("unit"))
    delta_text = "n/a"
    if payload["improvement_absolute"] is not None:
        delta_text = f"{payload['improvement_absolute']:.3f}"
        if metric.get("unit"):
            delta_text += f" {metric.get('unit')}"
        if payload["improvement_percent"] is not None:
            delta_text += f" ({payload['improvement_percent']:.2f}%)"
    guard_text = (
        " ".join(f"{name}={guard_counts[name]}" for name in sorted(guard_counts))
        if guard_counts
        else "none"
    )

    marker_fragments = []
    for index, point in enumerate(points):
        x = x_for(index)
        y = y_for(point["value"])
        marker_fragments.append(render_marker(x, y, point["decision"]))
        marker_fragments.append(
            f'<text x="{x:.1f}" y="{chart_top + chart_height + 24}" text-anchor="middle" '
            'font-size="12" fill="#334155">'
            f"{escape(point['label'])}</text>"
        )
        if point.get("milestone"):
            marker_fragments.append(
                f'<text x="{x:.1f}" y="{y - 14:.1f}" text-anchor="middle" '
                'font-size="11" fill="#7c3aed">'
                f"{escape(str(point['milestone']))}</text>"
            )

    hints = payload.get("stop_condition_hints") or []
    hint_text = " | ".join(str(item) for item in hints[:2]) if hints else "none"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" fill="#f8fafc"/>
  <rect x="24" y="24" width="{width - 48}" height="{height - 48}" rx="18" fill="#ffffff" stroke="#dbe4f0"/>
  <text x="50" y="72" font-size="28" font-weight="700" fill="#0f172a">AutoOptimize</text>
  <text x="50" y="104" font-size="16" fill="#334155">{escape(str(payload['goal']))}</text>
  <text x="50" y="136" font-size="14" fill="#475569">Metric: {escape(str(metric.get('name')))} ({escape(str(metric.get('direction')))}, unit={escape(str(metric.get('unit') or 'n/a'))})</text>
  <text x="50" y="166" font-size="14" fill="#475569">Baseline: {escape(format_metric_value(baseline.get('value') if isinstance(baseline, dict) else None, metric.get('unit')))}</text>
  <text x="50" y="196" font-size="14" fill="#475569">Best: {escape(best_text)}</text>
  <text x="50" y="226" font-size="14" fill="#475569">Improvement: {escape(delta_text)}</text>
  <text x="610" y="72" font-size="14" fill="#475569">Iterations: {payload['iterations_count']}</text>
  <text x="610" y="98" font-size="14" fill="#475569">keep={counts.get('keep', 0)} discard={counts.get('discard', 0)} pivot={counts.get('pivot', 0)} blocked={counts.get('blocked', 0)}</text>
  <text x="610" y="124" font-size="14" fill="#475569">Last decision: {escape(str(payload['last_decision'] or 'n/a'))}</text>
  <text x="610" y="150" font-size="14" fill="#475569">Guard status: {escape(guard_text)}</text>
  <text x="610" y="176" font-size="14" fill="#475569">Stop hints: {escape(hint_text)}</text>
  <rect x="{chart_left}" y="{chart_top}" width="{chart_width}" height="{chart_height}" fill="#f8fafc" stroke="#cbd5e1"/>
  <line x1="{chart_left}" y1="{chart_top}" x2="{chart_left}" y2="{chart_top + chart_height}" stroke="#cbd5e1"/>
  <line x1="{chart_left}" y1="{chart_top + chart_height}" x2="{chart_left + chart_width}" y2="{chart_top + chart_height}" stroke="#cbd5e1"/>
  <text x="{chart_left - 10}" y="{chart_top + 10}" text-anchor="end" font-size="12" fill="#64748b">{max_value:.2f}</text>
  <text x="{chart_left - 10}" y="{chart_top + chart_height}" text-anchor="end" font-size="12" fill="#64748b">{min_value:.2f}</text>
  <polyline fill="none" stroke="#0f766e" stroke-width="3" points="{polyline}"/>
  {''.join(marker_fragments)}
</svg>
"""


def render_marker(x: float, y: float, decision: str) -> str:
    if decision == "baseline":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#0f172a"/>'
    if decision == "keep":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#16a34a"/>'
    if decision == "discard":
        return f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" fill="#dc2626"/>'
    if decision == "pivot":
        return (
            f'<polygon points="{x:.1f},{y - 7:.1f} {x + 7:.1f},{y:.1f} '
            f'{x:.1f},{y + 7:.1f} {x - 7:.1f},{y:.1f}" fill="#d97706"/>'
        )
    return (
        f'<polygon points="{x:.1f},{y - 7:.1f} {x + 7:.1f},{y + 7:.1f} '
        f'{x - 7:.1f},{y + 7:.1f}" fill="#64748b"/>'
    )


if __name__ == "__main__":
    raise SystemExit(main())
