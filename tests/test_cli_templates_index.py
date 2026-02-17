import json

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_templates_list_command_shows_templates(repo):
    """Test that car templates list shows available templates."""
    result = runner.invoke(app, ["templates", "list", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    output = result.output
    parsed = json.loads(output)

    assert "templates" in parsed
    assert "count" in parsed
    assert isinstance(parsed["templates"], list)


def test_templates_list_command_human_readable(repo):
    """Test that car templates list shows human-readable output."""
    result = runner.invoke(app, ["templates", "list", "--repo", str(repo)])

    assert result.exit_code == 0

    output = result.output

    assert "templates" in output.lower() or "template" in output.lower()


def test_templates_show_command(repo):
    """Test that car templates show works for a valid template."""
    list_result = runner.invoke(
        app, ["templates", "list", "--repo", str(repo), "--json"]
    )

    if list_result.exit_code == 0:
        parsed = json.loads(list_result.output)
        if parsed.get("count", 0) > 0:
            first_template = parsed["templates"][0]
            template_ref = f"{first_template['repo_id']}:{first_template['path']}@{first_template['ref']}"

            result = runner.invoke(
                app,
                ["templates", "show", template_ref, "--repo", str(repo), "--json"],
            )

            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert "repo_id" in parsed
            assert "name" in parsed
            assert "summary" in parsed


def test_templates_show_command_not_found(repo):
    """Test that car templates show returns error for unknown template."""
    result = runner.invoke(
        app,
        ["templates", "show", "blessed:nonexistent.md@main", "--repo", str(repo)],
    )

    assert result.exit_code != 0


def test_templates_search_command(repo):
    """Test that car templates search finds matching templates."""
    result = runner.invoke(
        app, ["templates", "search", "bug", "--repo", str(repo), "--json"]
    )

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    assert "templates" in parsed
    assert "query" in parsed
    assert parsed["query"] == "bug"


def test_templates_search_no_results(repo):
    """Test that car templates search returns empty for no matches."""
    result = runner.invoke(
        app,
        [
            "templates",
            "search",
            "xyznonexistentquery123",
            "--repo",
            str(repo),
            "--json",
        ],
    )

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    assert parsed["count"] == 0
    assert len(parsed["templates"]) == 0


def test_describe_includes_templates_info(repo):
    """Test that car describe --json includes templates info."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    assert "templates" in parsed
    templates = parsed["templates"]
    assert "enabled" in templates
    assert "root" in templates
    assert "repos" in templates
    assert "count" in templates


def test_describe_templates_count(repo):
    """Test that car describe shows template count."""
    result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    parsed = json.loads(result.output)

    templates = parsed["templates"]
    if templates.get("enabled"):
        assert "count" in templates
        assert templates["count"] >= 0
