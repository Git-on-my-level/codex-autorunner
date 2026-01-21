#!/usr/bin/env python3

"""Update vendor/protocols/codex.json with current Codex app-server schema.

Usage:
    python scripts/update_vendor_codex_schema.py

This script runs `codex app-server generate-json-schema` and saves the output
to vendor/protocols/codex.json, which serves as the source-of-truth protocol
snapshot for Codex integration tests and CI drift detection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def get_codex_bin() -> str | None:
    """Get Codex binary path from environment or PATH."""
    codex_bin = os.environ.get("CODEX_BIN")
    if codex_bin:
        return codex_bin
    from shutil import which

    return which("codex")


def generate_schema() -> dict:
    """Generate Codex app-server JSON schema."""
    codex_bin = get_codex_bin()
    if not codex_bin:
        raise RuntimeError(
            "Codex binary not found. Set CODEX_BIN environment variable or install codex."
        )

    # Check if generate-json-schema is supported
    try:
        result = subprocess.run(
            [codex_bin, "app-server", "generate-json-schema", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Codex app-server does not support generate-json-schema: {result.stderr}"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout checking Codex app-server help")
    except FileNotFoundError:
        raise RuntimeError(f"Codex binary not found: {codex_bin}")

    # Generate the schema
    try:
        result = subprocess.run(
            [codex_bin, "app-server", "generate-json-schema"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout generating Codex JSON schema")

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to generate Codex JSON schema: {result.stderr}\n{result.stdout}"
        )

    # Parse JSON output
    try:
        schema = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse Codex JSON schema: {e}\n{result.stdout[:500]}"
        )

    return schema


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    vendor_dir = repo_root / "vendor" / "protocols"
    output_path = vendor_dir / "codex.json"

    # Create vendor/protocols directory if it doesn't exist
    vendor_dir.mkdir(parents=True, exist_ok=True)

    try:
        schema = generate_schema()

        # Write formatted JSON
        output_path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        print(f"✓ Updated {output_path.relative_to(repo_root)}")
        print(f"  Schema version: {schema.get('title', 'unknown')}")
        print(f"  Endpoints: {len(schema.get('methods', []))}")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
