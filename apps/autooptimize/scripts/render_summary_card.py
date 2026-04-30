from __future__ import annotations

import json
import struct
import zlib
from collections import Counter

from _autooptimize import (
    atomic_write_bytes,
    atomic_write_text,
    build_paths,
    build_status_payload,
    format_metric_value,
    load_iterations,
    load_run,
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
    png = build_summary_png(payload, rows)

    atomic_write_text(paths.summary_md_path, markdown)
    atomic_write_bytes(paths.summary_png_path, png)

    print(
        json.dumps(
            {
                "summary_md": str(paths.summary_md_path),
                "summary_png": str(paths.summary_png_path),
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


def build_summary_png(payload: dict, rows: list[dict]) -> bytes:
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

    width = 1000
    height = 580
    chart_left = 80
    chart_top = 185
    chart_width = 850
    chart_height = 300
    image = _new_image(width, height, (248, 250, 252))
    _fill_rect(image, width, 24, 24, width - 48, height - 48, (255, 255, 255))
    _rect(image, width, 24, 24, width - 48, height - 48, (219, 228, 240))
    _fill_rect(image, width, 50, 54, 250, 28, (15, 118, 110))
    _fill_rect(image, width, 50, 96, 520, 10, (203, 213, 225))
    _fill_rect(image, width, 50, 126, 420, 10, (226, 232, 240))

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

    counts = Counter(row["decision"] for row in rows)
    legend_items = [
        ((22, 163, 74), counts.get("keep", 0)),
        ((220, 38, 38), counts.get("discard", 0)),
        ((217, 119, 6), counts.get("pivot", 0)),
        ((100, 116, 139), counts.get("blocked", 0)),
    ]
    legend_x = 620
    for index, (color, count) in enumerate(legend_items):
        bar_height = max(8, count * 18)
        x = legend_x + index * 70
        _fill_rect(image, width, x, 110 - bar_height, 36, bar_height, color)
        _fill_rect(image, width, x, 122, 36, 6, color)

    _fill_rect(
        image, width, chart_left, chart_top, chart_width, chart_height, (248, 250, 252)
    )
    _rect(
        image, width, chart_left, chart_top, chart_width, chart_height, (203, 213, 225)
    )
    for grid_index in range(1, 5):
        y = chart_top + grid_index * chart_height // 5
        _line(image, width, chart_left, y, chart_left + chart_width, y, (226, 232, 240))

    previous: tuple[int, int] | None = None
    for index, point in enumerate(points):
        x = int(round(x_for(index)))
        y = int(round(y_for(point["value"])))
        if previous is not None:
            _wide_line(image, width, previous[0], previous[1], x, y, (15, 118, 110))
        _marker(image, width, x, y, point["decision"])
        previous = (x, y)

    return _encode_png(width, height, image)


def _new_image(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    return bytearray(color * (width * height))


def _set_pixel(
    image: bytearray,
    width: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
) -> None:
    if x < 0 or y < 0 or x >= width or y >= len(image) // (width * 3):
        return
    offset = (y * width + x) * 3
    image[offset : offset + 3] = bytes(color)


def _fill_rect(
    image: bytearray,
    width: int,
    x: int,
    y: int,
    rect_width: int,
    rect_height: int,
    color: tuple[int, int, int],
) -> None:
    for row in range(y, y + rect_height):
        for col in range(x, x + rect_width):
            _set_pixel(image, width, col, row, color)


def _rect(
    image: bytearray,
    width: int,
    x: int,
    y: int,
    rect_width: int,
    rect_height: int,
    color: tuple[int, int, int],
) -> None:
    _line(image, width, x, y, x + rect_width, y, color)
    _line(image, width, x, y + rect_height, x + rect_width, y + rect_height, color)
    _line(image, width, x, y, x, y + rect_height, color)
    _line(image, width, x + rect_width, y, x + rect_width, y + rect_height, color)


def _line(
    image: bytearray,
    width: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: tuple[int, int, int],
) -> None:
    dx = abs(x2 - x1)
    dy = -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    error = dx + dy
    while True:
        _set_pixel(image, width, x1, y1, color)
        if x1 == x2 and y1 == y2:
            break
        twice_error = 2 * error
        if twice_error >= dy:
            error += dy
            x1 += sx
        if twice_error <= dx:
            error += dx
            y1 += sy


def _wide_line(
    image: bytearray,
    width: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: tuple[int, int, int],
) -> None:
    for offset in (-1, 0, 1):
        _line(image, width, x1, y1 + offset, x2, y2 + offset, color)


def _fill_circle(
    image: bytearray,
    width: int,
    center_x: int,
    center_y: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    radius_squared = radius * radius
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius_squared:
                _set_pixel(image, width, x, y, color)


def _marker(image: bytearray, width: int, x: int, y: int, decision: str) -> None:
    colors = {
        "baseline": (15, 23, 42),
        "keep": (22, 163, 74),
        "discard": (220, 38, 38),
        "pivot": (217, 119, 6),
        "blocked": (100, 116, 139),
    }
    color = colors.get(decision, colors["blocked"])
    if decision == "discard":
        _fill_rect(image, width, x - 7, y - 7, 14, 14, color)
        return
    _fill_circle(image, width, x, y, 8, color)


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        start = y * stride
        rows.extend(pixels[start : start + stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    raise SystemExit(main())
