#!/usr/bin/env python3

"""Shared helpers for contract and drift-check tooling."""

from __future__ import annotations

import difflib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO


def load_text_document(path: Path, *, label: str) -> str:
    """Load a UTF-8 text document with a consistent missing-file error."""
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path.read_text(encoding="utf-8")


def load_json_document(path: Path, *, label: str) -> Any:
    """Load JSON from disk with a consistent malformed-input error."""
    raw = load_text_document(path, label=label)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path} ({exc.msg})") from exc


def compare_nested_data(name: str, expected: Any, actual: Any) -> list[str]:
    """Compare nested JSON-like structures and return readable difference lines."""
    differences: list[str] = []

    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        expected_keys = set(expected.keys())
        actual_keys = set(actual.keys())
        added_keys = actual_keys - expected_keys
        removed_keys = expected_keys - actual_keys

        if added_keys:
            differences.append(_format_key_change(name, "Added", added_keys))
        if removed_keys:
            differences.append(_format_key_change(name, "Removed", removed_keys))

        for key in sorted(expected_keys & actual_keys):
            child_name = f"{name}.{key}"
            if expected[key] != actual[key]:
                differences.extend(
                    compare_nested_data(child_name, expected[key], actual[key])
                )
        return differences

    if _is_sequence(expected) and _is_sequence(actual):
        if len(expected) != len(actual):
            return [
                f"  {name}: list length changed from {len(expected)} to {len(actual)}"
            ]
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            if expected_item == actual_item:
                continue
            child_name = f"{name}[{index}]"
            differences.extend(
                compare_nested_data(child_name, expected_item, actual_item)
            )
        return differences

    return [f"  {name}: value changed"]


def render_text_diff(
    expected_text: str,
    actual_text: str,
    *,
    fromfile: str,
    tofile: str,
) -> str:
    """Render a unified diff for two multi-line strings."""
    return "\n".join(
        difflib.unified_diff(
            expected_text.splitlines(),
            actual_text.splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )


def emit_cli_report(
    *,
    success_message: str,
    issues: Sequence[str],
    failure_header: str | None = None,
    success_stream: TextIO = sys.stdout,
    failure_stream: TextIO = sys.stderr,
    bullet_prefix: str = "",
) -> int:
    """Print a standard success/failure report and return the exit code."""
    if issues:
        if failure_header:
            print(failure_header, file=failure_stream)
        for issue in issues:
            print(f"{bullet_prefix}{issue}", file=failure_stream)
        return 1

    print(success_message, file=success_stream)
    return 0


def _format_key_change(name: str, verb: str, keys: set[str]) -> str:
    key_list = ", ".join(sorted(keys))
    if "." not in name and "[" not in name:
        return f"  {verb} keys: {key_list}"
    return f"  {name}: {verb.lower()} keys: {key_list}"


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )
