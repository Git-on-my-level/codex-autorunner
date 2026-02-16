import json
import re

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.self_describe import SCHEMA_ID, SCHEMA_VERSION

runner = CliRunner()


def test_describe_json_output_valid_json(repo):
    """Test that car describe --json emits valid JSON."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    output = result.output
    parsed = json.loads(output)

    assert "schema_id" in parsed
    assert "schema_version" in parsed
    assert "car_version" in parsed
    assert "repo_root" in parsed
    assert "initialized" in parsed
    assert "agents" in parsed
    assert "surfaces" in parsed
    assert "features" in parsed
    assert "templates" in parsed

    assert parsed["schema_id"] == SCHEMA_ID
    assert parsed["schema_version"] == SCHEMA_VERSION
    template_apply = parsed["templates"]["commands"]["apply"]
    assert "car templates apply <repo_id>:<path>[@<ref>]" in template_apply


def test_describe_json_output_matches_schema_keys(repo):
    """Test that JSON output contains all schema-required keys."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    required_keys = [
        "schema_id",
        "schema_version",
        "car_version",
        "repo_root",
        "initialized",
        "agents",
        "surfaces",
        "features",
    ]

    for key in required_keys:
        assert key in parsed, f"Missing required key: {key}"


def test_describe_json_does_not_contain_secrets(repo):
    """Test that JSON output does not contain known secret patterns."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    output = result.output

    secret_patterns = [
        r"sk-[a-zA-Z0-9]{20,}",
        r"-----BEGIN [A-Z]+ PRIVATE KEY-----",
        r"-----BEGIN CERTIFICATE-----",
        r"ghp_[a-zA-Z0-9]{36,}",
        r"gho_[a-zA-Z0-9]{36,}",
        r"glpat-[a-zA-Z0-9\-]{20,}",
        r"AKIA[0-9A-Z]{16}",
    ]

    for pattern in secret_patterns:
        assert not re.search(pattern, output), f"Found secret pattern: {pattern}"


def test_describe_json_redacts_secret_values_in_config(repo):
    """Test that secret values in config are redacted."""
    secret_config = repo / "codex-autorunner.yml"
    secret_config.write_text(
        """
agents:
  custom:
    api_key: "sk-1234567890123456789012345678901234567890"
    password: "super-secret-password"
"""
    )

    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    output = result.output
    assert "sk-1234567890123456789012345678901234567890" not in output
    assert "super-secret-password" not in output


def test_describe_human_output_is_readable(repo):
    """Test that car describe without --json outputs human-readable text."""
    result = runner.invoke(app, ["describe", "--repo", str(repo)])

    assert result.exit_code == 0

    output = result.output

    assert "CAR Version:" in output or "Codex Autorunner" in output
    assert "Repo Root:" in output or "Repo Root:" in output
    assert "Initialized:" in output
    assert "Active Surfaces:" in output or "Surfaces:" in output
    assert "Agents:" in output
    assert "Template Apply:" in output


def test_describe_schema_option(repo):
    """Test that car describe --schema prints schema info."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--schema"])

    assert result.exit_code == 0

    output = result.output

    assert SCHEMA_ID in output
    assert SCHEMA_VERSION in output
    assert "Schema Path:" in output or "Runtime Schema Path:" in output


def test_describe_without_repo_defaults_to_cwd(repo):
    """Test that car describe without --repo defaults to current directory."""
    import os

    original_cwd = os.getcwd()
    try:
        os.chdir(repo)
        result = runner.invoke(app, ["describe", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "schema_id" in parsed
    finally:
        os.chdir(original_cwd)


def test_describe_env_knobs_are_names_only(repo):
    """Test that env knobs are listed by name only, not values."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    if "env_knobs" in parsed:
        for knob in parsed["env_knobs"]:
            assert isinstance(knob, str)
            assert not any(
                c in knob for c in ["=", ":", "'", '"', "sk-", "ghp_", "gho_"]
            )


def test_describe_on_uninitialized_repo(repo):
    """Test that car describe works on uninitialized repo."""
    car_dir = repo / ".codex-autorunner"
    if car_dir.exists():
        import shutil

        shutil.rmtree(car_dir)

    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    parsed = json.loads(result.output)
    assert parsed.get("initialized") is False
