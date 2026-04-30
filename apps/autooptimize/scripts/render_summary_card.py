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
    metric = payload["metric"]
    baseline = payload["baseline"]
    best = payload["best"] if isinstance(payload["best"], dict) else None
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
    chart_left = 96
    chart_top = 248
    chart_width = 812
    chart_height = 238
    image = _new_image(width, height, (248, 250, 252))
    _fill_rect(image, width, 24, 24, width - 48, height - 48, (255, 255, 255))
    _rect(image, width, 24, 24, width - 48, height - 48, (219, 228, 240))

    title_color = (15, 23, 42)
    body_color = (51, 65, 85)
    muted_color = (100, 116, 139)
    accent_color = (15, 118, 110)
    _draw_text(image, width, 50, 54, "AUTOOPTIMIZE SUMMARY", title_color, scale=3)
    for line_index, line in enumerate(_wrap_text(str(payload["goal"]), 82)[:2]):
        _draw_text(
            image,
            width,
            50,
            92 + line_index * 18,
            line,
            body_color,
            scale=2,
        )

    baseline_text = format_metric_value(
        baseline.get("value") if isinstance(baseline, dict) else None,
        metric.get("unit"),
    )
    best_text = format_metric_value(
        best.get("value") if best else None, metric.get("unit")
    )
    delta_text = "N/A"
    if payload["improvement_absolute"] is not None:
        delta_text = f"{payload['improvement_absolute']:.0f}"
        if metric.get("unit"):
            delta_text += f" {metric.get('unit')}"
        if payload["improvement_percent"] is not None:
            delta_text += f" ({payload['improvement_percent']:.2f}%)"

    summary_items = [
        ("METRIC", str(metric.get("name") or "metric")),
        ("BASELINE", baseline_text),
        ("BEST", best_text),
        ("IMPROVEMENT", delta_text),
    ]
    for index, (label, value) in enumerate(summary_items):
        x = 50 + index * 230
        _draw_text(image, width, x, 140, label, muted_color, scale=1)
        _draw_text(image, width, x, 158, _truncate(value, 18), title_color, scale=2)

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
        ("KEEP", (22, 163, 74), counts.get("keep", 0)),
        ("DISCARD", (220, 38, 38), counts.get("discard", 0)),
        ("PIVOT", (217, 119, 6), counts.get("pivot", 0)),
        ("BLOCKED", (100, 116, 139), counts.get("blocked", 0)),
    ]
    legend_x = 96
    for index, (label, color, count) in enumerate(legend_items):
        x = legend_x + index * 142
        _fill_rect(image, width, x, 207, 14, 14, color)
        _draw_text(image, width, x + 22, 207, f"{label} {count}", body_color, scale=1)

    _draw_text(
        image,
        width,
        726,
        207,
        f"ITERATIONS {payload['iterations_count']}",
        body_color,
        scale=1,
    )

    _fill_rect(
        image, width, chart_left, chart_top, chart_width, chart_height, (248, 250, 252)
    )
    _rect(
        image, width, chart_left, chart_top, chart_width, chart_height, (203, 213, 225)
    )
    for grid_index in range(1, 5):
        y = chart_top + grid_index * chart_height // 5
        _line(image, width, chart_left, y, chart_left + chart_width, y, (226, 232, 240))
    _draw_text(
        image, width, 50, chart_top - 4, _short_number(max_value), muted_color, scale=1
    )
    _draw_text(
        image,
        width,
        50,
        chart_top + chart_height - 6,
        _short_number(min_value),
        muted_color,
        scale=1,
    )

    previous: tuple[int, int] | None = None
    for index, point in enumerate(points):
        x = int(round(x_for(index)))
        y = int(round(y_for(point["value"])))
        if previous is not None:
            _wide_line(image, width, previous[0], previous[1], x, y, (15, 118, 110))
        _marker(image, width, x, y, point["decision"])
        label = str(point["label"])
        value_label = _short_number(float(point["value"]))
        _draw_text(
            image,
            width,
            max(chart_left, x - 18),
            min(chart_top + chart_height + 22, y + 16),
            label,
            body_color,
            scale=1,
        )
        _draw_text(
            image,
            width,
            max(chart_left, x - 32),
            max(chart_top + 8, y - 22),
            value_label,
            title_color,
            scale=1,
        )
        previous = (x, y)

    hint = (
        "LOWER IS BETTER" if metric.get("direction") == "lower" else "HIGHER IS BETTER"
    )
    _draw_text(image, width, chart_left, 522, hint, accent_color, scale=1)
    guard_counts = payload.get("guard_status_counts") or {}
    guard_text = (
        " ".join(
            f"{name.upper()}={guard_counts[name]}" for name in sorted(guard_counts)
        )
        if guard_counts
        else "GUARDS NONE"
    )
    _draw_text(image, width, 690, 522, _truncate(guard_text, 34), muted_color, scale=1)

    return _encode_png(width, height, image)


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "+"


def _wrap_text(text: str, max_chars: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _short_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1000:
        return f"{value / 1000:.1f}K"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


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


def _draw_text(
    image: bytearray,
    width: int,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    *,
    scale: int = 1,
) -> None:
    cursor_x = x
    for char in str(text).upper():
        if char == " ":
            cursor_x += 4 * scale
            continue
        glyph = _FONT_5X7.get(char, _FONT_5X7.get("?"))
        if glyph is None:
            cursor_x += 6 * scale
            continue
        for row_index, row_bits in enumerate(glyph):
            for col_index, bit in enumerate(row_bits):
                if bit != "1":
                    continue
                _fill_rect(
                    image,
                    width,
                    cursor_x + col_index * scale,
                    y + row_index * scale,
                    scale,
                    scale,
                    color,
                )
        cursor_x += 6 * scale


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


_FONT_5X7: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "01100", "00100", "01000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "00100", "01000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "#": ("01010", "01010", "11111", "01010", "11111", "01010", "01010"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
}


if __name__ == "__main__":
    raise SystemExit(main())
