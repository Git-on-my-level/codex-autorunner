from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_drift_check(*args: str) -> subprocess.CompletedProcess[str]:
    repo_root = _repo_root()
    script_path = repo_root / "scripts" / "check_destination_contract_drift.py"
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )


def test_destination_contract_drift_check_passes() -> None:
    result = _run_drift_check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout.lower()


def test_destination_contract_drift_check_detects_schema_mismatch(
    tmp_path: Path,
) -> None:
    repo_root = _repo_root()
    schema_doc = repo_root / "docs" / "reference" / "hub-manifest-schema.md"
    patched_schema = tmp_path / "hub-manifest-schema.md"
    patched_schema.write_text(
        schema_doc.read_text(encoding="utf-8").replace(
            "- full-dev", "- wrong-profile", 1
        ),
        encoding="utf-8",
    )

    result = _run_drift_check("--schema-doc", str(patched_schema))
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "drift" in output.lower()
