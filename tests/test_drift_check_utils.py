from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from scripts.check_protocol_drift import compare_codex_schema
from scripts.drift_check_utils import (
    compare_nested_data,
    emit_cli_report,
    load_json_document,
    render_text_diff,
)


def test_load_json_document_missing_file_reports_label(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    with pytest.raises(
        FileNotFoundError, match=f"Vendor snapshot not found: {missing_path}"
    ):
        load_json_document(missing_path, label="Vendor snapshot")


def test_load_json_document_rejects_malformed_json(tmp_path: Path) -> None:
    bad_json = tmp_path / "vendor.json"
    bad_json.write_text("{not json", encoding="utf-8")

    with pytest.raises(
        ValueError, match=r"Vendor snapshot is not valid JSON: .*vendor\.json"
    ):
        load_json_document(bad_json, label="Vendor snapshot")


def test_compare_nested_data_reports_nested_changes() -> None:
    differences = compare_nested_data(
        "codex",
        {
            "info": {"title": "vendor", "version": "1"},
            "paths": [{"name": "alpha"}],
        },
        {
            "info": {"title": "current", "extra": True},
            "paths": [{"name": "beta"}],
            "servers": [],
        },
    )

    assert differences == [
        "  Added keys: servers",
        "  codex.info: added keys: extra",
        "  codex.info: removed keys: version",
        "  codex.info.title: value changed",
        "  codex.paths[0].name: value changed",
    ]


def test_render_text_diff_returns_unified_diff() -> None:
    diff = render_text_diff("alpha\nbeta\n", "alpha\ngamma\n", fromfile="a", tofile="b")

    assert diff == "--- a\n+++ b\n@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma"


def test_emit_cli_report_uses_configured_streams() -> None:
    success_stream = StringIO()
    failure_stream = StringIO()

    exit_code = emit_cli_report(
        success_message="all clear",
        issues=["first issue", "second issue"],
        failure_header="check failed",
        success_stream=success_stream,
        failure_stream=failure_stream,
        bullet_prefix="- ",
    )

    assert exit_code == 1
    assert success_stream.getvalue() == ""
    assert failure_stream.getvalue() == "check failed\n- first issue\n- second issue\n"


def test_compare_codex_schema_reports_malformed_snapshot(tmp_path: Path) -> None:
    vendor_path = tmp_path / "codex.json"
    vendor_path.write_text("{invalid", encoding="utf-8")

    code, messages = compare_codex_schema(vendor_path)

    assert code == 2
    assert messages[0].startswith("Vendor schema is not valid JSON:")
    assert messages[1:] == [
        "Run: make agent-compatibility-refresh",
        "Fallback: python scripts/update_vendor_codex_schema.py",
    ]
