import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_SCHEMAS_DIR = REPO_ROOT / "docs" / "protocol_schemas"


def _find_schema_versions(schema_type: str) -> list[Path]:
    """Find all version directories for a given schema type."""
    base_dir = PROTOCOL_SCHEMAS_DIR / schema_type
    if not base_dir.exists():
        return []
    return [d for d in base_dir.iterdir() if d.is_dir()]


def _find_schema_files(schema_type: str) -> list[Path]:
    """Find all schema files for a given schema type."""
    versions = _find_schema_versions(schema_type)
    files = []
    for version_dir in versions:
        if schema_type == "opencode":
            schema_file = version_dir / "openapi.json"
        else:
            schema_file = version_dir / "codex.json"
        if schema_file.exists():
            files.append(schema_file)
    return files


class TestProtocolSchemaSnapshots:
    """Validate committed protocol schema snapshots."""

    @pytest.mark.parametrize(
        "schema_file",
        _find_schema_files("codex-app-server"),
        ids=lambda p: p.parent.name,
    )
    def test_codex_schema_is_valid_json(self, schema_file: Path):
        """Codex schema snapshots should be parseable JSON."""
        content = schema_file.read_text(encoding="utf-8")
        schema = json.loads(content)
        assert isinstance(schema, dict)

    @pytest.mark.parametrize(
        "schema_file",
        _find_schema_files("codex-app-server"),
        ids=lambda p: p.parent.name,
    )
    def test_codex_schema_has_expected_structure(self, schema_file: Path):
        """Codex schema should have expected top-level keys."""
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        assert "$schema" in schema
        assert "definitions" in schema

    @pytest.mark.parametrize(
        "schema_file",
        _find_schema_files("codex-app-server"),
        ids=lambda p: p.parent.name,
    )
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

    @pytest.mark.parametrize(
        "schema_file", _find_schema_files("opencode"), ids=lambda p: p.parent.name
    )
    def test_openapi_spec_is_valid_json(self, schema_file: Path):
        """OpenAPI spec snapshots should be parseable JSON."""
        content = schema_file.read_text(encoding="utf-8")
        spec = json.loads(content)
        assert isinstance(spec, dict)

    @pytest.mark.parametrize(
        "schema_file", _find_schema_files("opencode"), ids=lambda p: p.parent.name
    )
    def test_openapi_spec_has_expected_structure(self, schema_file: Path):
        """OpenAPI spec should have expected top-level keys."""
        spec = json.loads(schema_file.read_text(encoding="utf-8"))
        assert "openapi" in spec
        assert "info" in spec
        assert "paths" in spec

    @pytest.mark.parametrize(
        "schema_file", _find_schema_files("opencode"), ids=lambda p: p.parent.name
    )
    def test_openapi_spec_has_known_endpoints(self, schema_file: Path):
        """OpenAPI spec should contain known endpoints."""
        spec = json.loads(schema_file.read_text(encoding="utf-8"))
        paths = spec.get("paths", {})
        path_list = list(paths.keys())
        assert "/global/health" in path_list, "Expected /global/health endpoint"
        assert "/session" in path_list, "Expected /session endpoint"

    def test_protocol_schemas_directory_exists(self):
        """Protocol schemas directory should exist."""
        assert PROTOCOL_SCHEMAS_DIR.exists()
        assert PROTOCOL_SCHEMAS_DIR.is_dir()

    def test_codex_schemas_exist(self):
        """At least one Codex schema snapshot should exist."""
        files = _find_schema_files("codex-app-server")
        assert len(files) > 0, "Expected at least one Codex schema snapshot"

    def test_opencode_schemas_exist(self):
        """At least one OpenCode schema snapshot should exist."""
        files = _find_schema_files("opencode")
        assert len(files) > 0, "Expected at least one OpenCode schema snapshot"
