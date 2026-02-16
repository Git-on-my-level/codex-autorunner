import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_PROTOCOL_DIR = REPO_ROOT / "vendor" / "protocols"
CODEX_SCHEMA_PATH = VENDOR_PROTOCOL_DIR / "codex.json"
OPENCODE_SCHEMA_PATH = VENDOR_PROTOCOL_DIR / "opencode_openapi.json"


class TestProtocolSchemaSnapshots:
    """Validate committed protocol schema snapshots."""

    @pytest.mark.parametrize("schema_file", [CODEX_SCHEMA_PATH])
    def test_codex_schema_is_valid_json(self, schema_file: Path):
        """Codex schema snapshots should be parseable JSON."""
        content = schema_file.read_text(encoding="utf-8")
        schema = json.loads(content)
        assert isinstance(schema, dict)

    @pytest.mark.parametrize("schema_file", [CODEX_SCHEMA_PATH])
    def test_codex_schema_has_expected_structure(self, schema_file: Path):
        """Codex schema should have expected top-level keys."""
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        assert "$schema" in schema
        assert "definitions" in schema

    @pytest.mark.parametrize("schema_file", [CODEX_SCHEMA_PATH])
    def test_codex_schema_has_known_types(self, schema_file: Path):
        """Codex schema should contain known type definitions."""
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        definitions = schema.get("definitions", {})
        definition_names = set(definitions.keys())
        expected_indicators = ["Turn", "Thread", "Message", "Session"]
        found = any(
            indicator in name
            for name in definition_names
            for indicator in expected_indicators
        )
        assert found, f"Expected to find types containing {expected_indicators}"

    @pytest.mark.parametrize("schema_file", [OPENCODE_SCHEMA_PATH])
    def test_openapi_spec_is_valid_json(self, schema_file: Path):
        """OpenAPI spec snapshots should be parseable JSON."""
        content = schema_file.read_text(encoding="utf-8")
        spec = json.loads(content)
        assert isinstance(spec, dict)

    @pytest.mark.parametrize("schema_file", [OPENCODE_SCHEMA_PATH])
    def test_openapi_spec_has_expected_structure(self, schema_file: Path):
        """OpenAPI spec should have expected top-level keys."""
        spec = json.loads(schema_file.read_text(encoding="utf-8"))
        assert "openapi" in spec
        assert "info" in spec
        assert "paths" in spec

    @pytest.mark.parametrize("schema_file", [OPENCODE_SCHEMA_PATH])
    def test_openapi_spec_has_known_endpoints(self, schema_file: Path):
        """OpenAPI spec should contain known endpoints."""
        spec = json.loads(schema_file.read_text(encoding="utf-8"))
        paths = spec.get("paths", {})
        path_list = list(paths.keys())
        assert "/global/health" in path_list, "Expected /global/health endpoint"
        assert "/session" in path_list, "Expected /session endpoint"

    def test_protocol_schema_dir_exists(self):
        """Vendor protocol schema directory should exist."""
        assert VENDOR_PROTOCOL_DIR.exists()
        assert VENDOR_PROTOCOL_DIR.is_dir()

    def test_codex_schemas_exist(self):
        """Canonical Codex schema snapshot should exist."""
        assert CODEX_SCHEMA_PATH.exists(), f"Expected snapshot at {CODEX_SCHEMA_PATH}"

    def test_opencode_schemas_exist(self):
        """Canonical OpenCode schema snapshot should exist."""
        assert (
            OPENCODE_SCHEMA_PATH.exists()
        ), f"Expected snapshot at {OPENCODE_SCHEMA_PATH}"
