#!/usr/bin/env python3

"""Update vendor/protocols/opencode_openapi.json with current OpenCode OpenAPI spec.

Usage:
    python scripts/update_vendor_opencode_openapi.py

This script starts an OpenCode server, fetches the OpenAPI spec from /doc,
and saves it to vendor/protocols/opencode_openapi.json, which serves as the
source-of-truth protocol snapshot for OpenCode integration tests and CI drift detection.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional


def get_opencode_bin() -> Optional[str]:
    """Get OpenCode binary path from environment or PATH."""
    opencode_bin = os.environ.get("OPENCODE_BIN")
    if opencode_bin:
        return opencode_bin
    return shutil.which("opencode")


async def fetch_openapi(base_url: str, timeout: float = 30.0) -> dict:
    """Fetch OpenAPI spec from running OpenCode server."""
    import httpx

    doc_url = f"{base_url.rstrip('/')}/doc"

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(doc_url)
        response.raise_for_status()

        # Parse JSON
        try:
            spec = response.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Failed to parse OpenAPI JSON: {e}\n{response.text[:500]}"
            )

        return spec


@asynccontextmanager
async def opencode_server_context(
    tmp_path: Path, opencode_bin: str
) -> AsyncGenerator[str, None]:
    """Context manager that starts and stops an OpenCode server."""
    import time

    # Start server on random port
    command = [opencode_bin, "serve", "--hostname", "127.0.0.1", "--port", "0"]

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
    )

    try:
        # Parse stdout to get the base URL
        base_url = None
        start_time = time.monotonic()
        timeout = 60.0

        while time.monotonic() - start_time < timeout:
            line = proc.stdout.readline()
            if not line:
                # Process exited
                stderr = proc.stderr.read()
                raise RuntimeError(f"OpenCode server exited: {stderr}")

            # Look for URL in output (format varies)
            # Common patterns: "http://localhost:12345", "Server started at ..."
            if "http://" in line:
                # Extract URL
                import re

                match = re.search(r"https?://[^\s]+", line)
                if match:
                    base_url = match.group(0)
                    break

        if not base_url:
            raise RuntimeError(
                f"Timeout waiting for OpenCode server to start. Last output: {line}"
            )

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


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    vendor_dir = repo_root / "vendor" / "protocols"
    output_path = vendor_dir / "opencode_openapi.json"

    # Create vendor/protocols directory if it doesn't exist
    vendor_dir.mkdir(parents=True, exist_ok=True)

    opencode_bin = get_opencode_bin()
    if not opencode_bin:
        print(
            "Error: OpenCode binary not found. "
            "Set OPENCODE_BIN environment variable or install opencode.",
            file=sys.stderr,
        )
        return 1

    async def run() -> int:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            async with opencode_server_context(tmp_path, opencode_bin) as base_url:
                print(f"Fetching OpenAPI spec from {base_url}/doc...")

                spec = await fetch_openapi(base_url)

                # Write formatted JSON
                output_path.write_text(
                    json.dumps(spec, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                info = spec.get("info", {})
                print(f"✓ Updated {output_path.relative_to(repo_root)}")
                print(f"  Title: {info.get('title', 'unknown')}")
                print(f"  Version: {info.get('version', 'unknown')}")
                print(f"  Endpoints: {len(spec.get('paths', {}))}")

                return 0

    try:
        return asyncio.run(run())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
