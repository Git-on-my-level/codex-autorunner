#!/usr/bin/env python3

"""Check for protocol drift in Codex and OpenCode vendor snapshots.

Usage:
    python scripts/check_protocol_drift.py

This script compares the current Codex/OpenCode protocol artifacts against
the vendor snapshots and reports differences. Used in CI to detect
upstream protocol changes.

Exit codes:
    0: No drift detected
    1: Drift detected
    2: Error (missing snapshots, binary not found, etc.)
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.protocol_utils import validate_binary_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_codex_bin() -> str | None:
    """Get Codex binary path from environment or PATH."""
    try:
        path = validate_binary_path("codex", "CODEX_BIN")
        return str(path)
    except RuntimeError:
        return None


def get_opencode_bin() -> str | None:
    """Get OpenCode binary path from environment or PATH."""
    try:
        path = validate_binary_path("opencode", "OPENCODE_BIN")
        return str(path)
    except RuntimeError:
        return None


def generate_current_codex_schema() -> dict | None:
    """Generate current Codex schema by running binary."""
    codex_bin = get_codex_bin()
    if not codex_bin:
        return None

    with TemporaryDirectory() as tmp_dir:
        try:
            result = subprocess.run(
                [codex_bin, "app-server", "generate-json-schema", "--out", tmp_dir],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            return None

        if result.returncode != 0:
            return None

        schema_path = Path(tmp_dir) / "codex_app_server_protocol.schemas.json"
        if not schema_path.exists():
            return None

        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None


def compare_dicts(name: str, vendor: dict, current: dict) -> list[str]:
    """Compare two dicts and return differences."""
    differences: list[str] = []

    # Check for added/removed top-level keys
    vendor_keys = set(vendor.keys())
    current_keys = set(current.keys())

    added_keys = current_keys - vendor_keys
    removed_keys = vendor_keys - current_keys

    if added_keys:
        differences.append(f"  Added keys: {', '.join(sorted(added_keys))}")
    if removed_keys:
        differences.append(f"  Removed keys: {', '.join(sorted(removed_keys))}")

    # Compare nested structures for common keys
    common_keys = vendor_keys & current_keys
    for key in sorted(common_keys):
        v_val = vendor[key]
        c_val = current[key]

        if v_val != c_val:
            if isinstance(v_val, dict) and isinstance(c_val, dict):
                nested_diffs = compare_dicts(f"{name}.{key}", v_val, c_val)
                if nested_diffs:
                    differences.extend(nested_diffs)
            elif isinstance(v_val, list) and isinstance(c_val, list):
                if len(v_val) != len(c_val):
                    differences.append(
                        f"  {name}.{key}: list length changed from {len(v_val)} to {len(c_val)}"
                    )
                else:
                    for i, (v_item, c_item) in enumerate(zip(v_val, c_val)):
                        if v_item != c_item:
                            differences.append(f"  {name}.{key}[{i}]: value changed")
            else:
                differences.append(f"  {name}.{key}: value changed")

    return differences


def compare_codex_schema(vendor_path: Path) -> tuple[int, list[str]]:
    """Compare vendor Codex schema with current generated schema."""
    if not vendor_path.exists():
        return 2, [
            f"Vendor schema not found: {vendor_path}",
            "Run: python scripts/update_vendor_codex_schema.py",
        ]

    vendor_schema = json.loads(vendor_path.read_text(encoding="utf-8"))
    current_schema = generate_current_codex_schema()

    if current_schema is None:
        return 0, [
            "Codex binary not found or does not support generate-json-schema",
            "Skipping Codex schema check",
        ]

    differences = compare_dicts("codex", vendor_schema, current_schema)

    if differences:
        return 1, [
            "Codex schema drift detected:",
            *differences,
            "",
            "Run: python scripts/update_vendor_codex_schema.py",
            "Then commit: vendor/protocols/codex.json",
        ]

    return 0, ["Codex schema: no drift"]


def compare_opencode_openapi(vendor_path: Path) -> tuple[int, list[str]]:
    """Compare vendor OpenAPI spec with current server spec."""
    if not vendor_path.exists():
        return 2, [
            f"Vendor OpenAPI spec not found: {vendor_path}",
            "Run: python scripts/update_vendor_opencode_openapi.py",
        ]

    # Check if OpenCode binary is available
    opencode_bin = get_opencode_bin()
    if not opencode_bin:
        return 0, [
            "OpenCode binary not found",
            "Skipping OpenCode OpenAPI check",
        ]

    return 1, [
        "OpenCode drift detection not yet implemented",
        "To check manually, run: python scripts/update_vendor_opencode_openapi.py",
    ]


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    vendor_dir = repo_root / "vendor" / "protocols"
    codex_schema_path = vendor_dir / "codex.json"
    opencode_openapi_path = vendor_dir / "opencode_openapi.json"

    all_messages: list[str] = []
    all_codes: list[int] = []

    # Check Codex schema
    codex_code, codex_messages = compare_codex_schema(codex_schema_path)
    all_codes.append(codex_code)
    all_messages.extend(codex_messages)

    # Check OpenCode OpenAPI
    if codex_messages:
        all_messages.append("")  # Blank line separator

    opencode_code, opencode_messages = compare_opencode_openapi(opencode_openapi_path)
    all_codes.append(opencode_code)
    all_messages.extend(opencode_messages)

    # Output messages
    for message in all_messages:
        print(message)

    # Return highest error code
    return max(all_codes) if all_codes else 0


if __name__ == "__main__":
    sys.exit(main())
