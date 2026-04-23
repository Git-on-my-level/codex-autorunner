from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


_repo_root_str = str(_repo_root())
if _repo_root_str not in sys.path:
    sys.path.insert(0, _repo_root_str)

from scripts.check_destination_contract_drift import run_check  # noqa: E402


def test_destination_contract_drift_check_passes() -> None:
    repo_root = _repo_root()
    issues = run_check(
        destinations_doc=repo_root / "docs" / "configuration" / "destinations.md",
        schema_doc=repo_root / "docs" / "reference" / "hub-manifest-schema.md",
    )
    assert not issues, "\n".join(issues)


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

    issues = run_check(
        destinations_doc=repo_root / "docs" / "configuration" / "destinations.md",
        schema_doc=patched_schema,
    )
    assert issues, "Expected drift issues but got none"
    assert any("drift" in issue.lower() for issue in issues), "\n".join(issues)
