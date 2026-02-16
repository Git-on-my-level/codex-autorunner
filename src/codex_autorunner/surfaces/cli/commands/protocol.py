import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import typer

from ....core.utils import resolve_executable
from .utils import raise_exit

logger = logging.getLogger("codex_autorunner.protocol")


def _get_codex_bin() -> Optional[str]:
    """Get Codex binary path from environment or PATH."""
    env_path = os.environ.get("CODEX_BIN")
    if env_path:
        return env_path
    return resolve_executable("codex")


def _get_opencode_bin() -> Optional[str]:
    """Get OpenCode binary path from environment or PATH."""
    env_path = os.environ.get("OPENCODE_BIN")
    if env_path:
        return env_path
    return resolve_executable("opencode")


def _generate_codex_schema(codex_bin: str, tmp_dir: Path) -> dict:
    """Generate Codex app-server JSON schema."""
    result = subprocess.run(
        [codex_bin, "app-server", "generate-json-schema", "--out", str(tmp_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to generate Codex JSON schema: {result.stderr}\n{result.stdout}"
        )

    schema_path = tmp_dir / "codex_app_server_protocol.schemas.json"
    if not schema_path.exists():
        raise RuntimeError(
            f"Codex schema bundle not found: {schema_path}. Output: {result.stdout}"
        )

    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse Codex JSON schema: {e}") from e


async def _fetch_openapi_spec(base_url: str) -> dict:
    """Fetch OpenAPI spec from running OpenCode server."""
    import httpx

    doc_url = f"{base_url.rstrip('/')}/doc"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(doc_url)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]


async def _run_opencode_and_fetch(base_url: str, opencode_bin: str) -> dict:
    """Start OpenCode server and fetch OpenAPI spec."""
    proc = subprocess.Popen(
        [opencode_bin, "serve", "--hostname", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    try:
        server_url: Optional[str] = None
        start_time = time.monotonic()
        timeout = 60.0

        while time.monotonic() - start_time < timeout:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            if not line:
                assert proc.stderr is not None
                stderr = proc.stderr.read()
                raise RuntimeError(f"OpenCode server exited: {stderr}")

            if "http://" in line:
                match = re.search(r"https?://[^\s]+", line)
                if match:
                    server_url = match.group(0)
                    break

        if not server_url:
            raise RuntimeError("Timeout waiting for OpenCode server to start")

        await asyncio.sleep(1.0)
        return await _fetch_openapi_spec(server_url)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def register_protocol_commands(app: typer.Typer) -> None:
    @app.command(name="refresh")
    def refresh_schemas(
        codex: bool = typer.Option(
            True, "--codex/--no-codex", help="Refresh Codex app-server schema"
        ),
        opencode: bool = typer.Option(
            True, "--opencode/--no-opencode", help="Refresh OpenCode OpenAPI spec"
        ),
        target_dir: Optional[Path] = typer.Option(
            None,
            "--target-dir",
            help="Target directory for schemas (defaults to docs/protocol_schemas)",
        ),
    ) -> None:
        """Refresh protocol schema snapshots.

        Requires Codex and/or OpenCode binaries to be available.
        Set CODEX_BIN and OPENCODE_BIN environment variables if not in PATH.
        """
        repo_root = Path(__file__).resolve().parents[4]
        if target_dir is None:
            target_dir = repo_root / "docs" / "protocol_schemas"

        codex_dir = target_dir / "codex-app-server"
        opencode_dir = target_dir / "opencode"

        codex_bin = _get_codex_bin()
        opencode_bin = _get_opencode_bin()

        has_errors = False

        if codex:
            if not codex_bin:
                typer.echo(
                    "Codex binary not found. Set CODEX_BIN or install codex.",
                    err=True,
                )
                has_errors = True
            else:
                try:
                    with TemporaryDirectory() as tmp:
                        tmp_path = Path(tmp)
                        typer.echo(f"Generating Codex schema from {codex_bin}...")
                        schema = _generate_codex_schema(codex_bin, tmp_path)

                        version = "unknown"
                        if "title" in schema:
                            version = schema["title"].lower().replace(" ", "-")

                        version_dir = codex_dir / version
                        version_dir.mkdir(parents=True, exist_ok=True)

                        output_path = version_dir / "codex.json"
                        output_path.write_text(
                            json.dumps(schema, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8",
                        )
                        typer.echo(f"  Saved to {output_path.relative_to(repo_root)}")
                except Exception as e:
                    typer.echo(f"Error generating Codex schema: {e}", err=True)
                    has_errors = True

        if opencode:
            if not opencode_bin:
                typer.echo(
                    "OpenCode binary not found. Set OPENCODE_BIN or install opencode.",
                    err=True,
                )
                has_errors = True
            else:
                try:
                    typer.echo("Starting OpenCode server to fetch OpenAPI spec...")
                    spec = asyncio.run(
                        _run_opencode_and_fetch("http://127.0.0.1:0", opencode_bin)
                    )

                    version = (spec.get("info") or {}).get("version", "unknown")
                    if not version or version == "unknown":
                        from datetime import datetime

                        version = datetime.now().strftime("%Y%m%d-%H%M%S")

                    version_dir = opencode_dir / version
                    version_dir.mkdir(parents=True, exist_ok=True)

                    output_path = version_dir / "openapi.json"
                    output_path.write_text(
                        json.dumps(spec, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    typer.echo(f"  Saved to {output_path.relative_to(repo_root)}")
                except Exception as e:
                    typer.echo(f"Error generating OpenAPI spec: {e}", err=True)
                    has_errors = True

        if has_errors:
            raise_exit("Failed to refresh protocol schemas")
