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
import logging
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.protocol_utils import validate_binary_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_codex_bin() -> str:
    """Get Codex binary path from environment or PATH."""
    path = validate_binary_path("codex", "CODEX_BIN")
    return str(path)


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
        raise RuntimeError("Timeout checking Codex app-server help") from None
    except FileNotFoundError:
        raise RuntimeError(f"Codex binary not found: {codex_bin}") from None

    with TemporaryDirectory() as tmp_dir:
        try:
            result = subprocess.run(
                [codex_bin, "app-server", "generate-json-schema", "--out", tmp_dir],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Timeout generating Codex JSON schema") from None

        if result.returncode != 0:
            raise RuntimeError(
                "Failed to generate Codex JSON schema: "
                f"{result.stderr}\n{result.stdout}"
            )

        schema_path = Path(tmp_dir) / "codex_app_server_protocol.schemas.json"
        if not schema_path.exists():
            raise RuntimeError(
                f"Codex schema bundle not found: {schema_path}. Output: {result.stdout}"
            )

        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Failed to parse Codex JSON schema: {e}\n"
                f"{schema_path.read_text(encoding='utf-8')[:500]}"
            ) from e

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
        print(f"  Schema title: {schema.get('title', 'unknown')}")
        print(f"  Definitions: {len(schema.get('definitions', {}))}")

        return 0
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
