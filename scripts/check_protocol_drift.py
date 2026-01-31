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

import asyncio
import json
import logging
import subprocess
import sys
import time
import re
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import AsyncGenerator

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


def generate_current_opencode_openapi() -> tuple[dict | None, str | None]:
    """Generate current OpenAPI spec by running OpenCode server.

    Returns:
        (spec, opencode_bin_path)
    """
    opencode_bin = get_opencode_bin()
    if not opencode_bin:
        return None, None

    async def fetch_openapi(base_url: str, timeout: float = 30.0) -> dict | None:
        """Fetch OpenAPI spec from running OpenCode server."""
        try:
            import httpx
        except ImportError:
            logging.error("httpx not available, install with: pip install httpx")
            return None

        doc_url = f"{base_url.rstrip('/')}/doc"

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.get(doc_url)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logging.error(f"Failed to fetch OpenAPI spec: {e}")
                return None

    @asynccontextmanager
    async def opencode_server_context(
        tmp_path: Path, opencode_bin: str
    ) -> AsyncGenerator[str | None, None]:
        """Context manager that starts and stops an OpenCode server."""
        # Start server on random port
        command = [opencode_bin, "serve", "--hostname", "127.0.0.1", "--port", "0"]

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        try:
            # Parse stdout to get the base URL
            base_url = None
            start_time = time.monotonic()
            timeout = 60.0
            last_line = ""

            while time.monotonic() - start_time < timeout:
                line = proc.stdout.readline()
                if not line:
                    # Process exited
                    if proc.stderr:
                        stderr = proc.stderr.read()
                        logging.error(f"OpenCode server exited: {stderr}")
                    yield None
                    return
                last_line = line

                # Look for URL in output (format varies)
                # Common patterns: "http://localhost:12345", "Server started at ..."
                if "http://" in line:
                    # Extract URL
                    match = re.search(r"https?://[^\s]+", line)
                    if match:
                        base_url = match.group(0)
                        break

            if not base_url:
                logging.error(
                    f"Timeout waiting for OpenCode server to start. Last output: {last_line}"
                )
                yield None
                return

            # Give server a moment to be ready
            await asyncio.sleep(1.0)

            yield base_url

        finally:
            # Terminate server
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    async def run_generation() -> dict | None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            async with opencode_server_context(tmp_path, opencode_bin) as base_url:
                if not base_url:
                    return None
                return await fetch_openapi(base_url)

    return asyncio.run(run_generation()), opencode_bin


def compare_opencode_openapi(vendor_path: Path) -> tuple[int, list[str]]:
    """Compare vendor OpenAPI spec with current server spec."""
    if not vendor_path.exists():
        return 2, [
            f"Vendor OpenAPI spec not found: {vendor_path}",
            "Run: python scripts/update_vendor_opencode_openapi.py",
        ]

    vendor_spec = json.loads(vendor_path.read_text(encoding="utf-8"))
    current_spec, opencode_bin = generate_current_opencode_openapi()

    if current_spec is None:
        return 0, [
            "OpenCode binary not found or server failed to start",
            "Skipping OpenCode OpenAPI check",
        ]

    vendor_version = (vendor_spec.get("info") or {}).get("version")
    current_version = (current_spec.get("info") or {}).get("version")

    if vendor_version and current_version and vendor_version != current_version:
        messages = [
            "OpenCode version mismatch; skipping OpenAPI drift check",
            f"  Vendor version:  {vendor_version}",
            f"  Current version: {current_version}",
        ]
        if opencode_bin:
            messages.append(f"  Binary path:    {opencode_bin}")
        messages.extend(
            [
                "",
                "Set OPENCODE_BIN to a binary matching the vendor snapshot, "
                "or regenerate the vendor snapshot with the current binary:",
                "  python scripts/update_vendor_opencode_openapi.py",
            ]
        )
        return 0, messages

    differences = compare_dicts("opencode", vendor_spec, current_spec)

    if differences:
        return 1, [
            "OpenCode OpenAPI drift detected:",
            *differences,
            "",
            "Run: python scripts/update_vendor_opencode_openapi.py",
            "Then commit: vendor/protocols/opencode_openapi.json",
        ]

    return 0, ["OpenCode OpenAPI: no drift"]


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
