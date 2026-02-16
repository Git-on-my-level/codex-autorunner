"""Smoke test for self-description and templates functionality.

This test validates:
- car describe --json parses correctly
- Template listing works
- Template can be applied to a temp directory
- Context injection works in ticket frontmatter
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()

MAX_OUTPUT_SIZE = 5000


def truncate(s: str, max_size: int = MAX_OUTPUT_SIZE) -> str:
    """Truncate output to max_size, adding ellipsis if truncated."""
    if len(s) <= max_size:
        return s
    return s[: max_size - 3] + "..."


def run_command(cmd: list[str], cwd: Path) -> dict[str, Any]:
    """Run a command and return result."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return {
        "exit_code": result.returncode,
        "stdout": truncate(result.stdout),
        "stderr": truncate(result.stderr),
    }


def test_selfdescribe_smoke(hub_env, tmp_path: Path):
    """Run smoke test for self-description and templates."""
    repo = hub_env.repo_root

    results: dict[str, Any] = {
        "status": "pass",
        "checks": [],
    }

    def add_check(name: str, passed: bool, details: str = ""):
        results["checks"].append(
            {
                "name": name,
                "passed": passed,
                "details": details,
            }
        )
        if not passed:
            results["status"] = "fail"

    describe_output = None
    try:
        result = runner.invoke(app, ["describe", "--repo", str(repo), "--json"])
        if result.exit_code == 0:
            try:
                describe_output = json.loads(result.output)
                add_check(
                    "car describe --json parses",
                    True,
                    f"Schema ID: {describe_output.get('schema_id')}",
                )

                required_keys = [
                    "schema_id",
                    "schema_version",
                    "car_version",
                    "templates",
                ]
                all_keys_present = all(k in describe_output for k in required_keys)
                add_check(
                    "car describe has required keys",
                    all_keys_present,
                    f"Keys: {list(describe_output.keys())}",
                )

                if "templates" in describe_output:
                    tmpl = describe_output["templates"]
                    add_check(
                        "templates info present",
                        True,
                        f"Enabled: {tmpl.get('enabled')}, Count: {tmpl.get('count')}",
                    )
            except json.JSONDecodeError as e:
                add_check("car describe --json parses", False, str(e))
        else:
            add_check(
                "car describe --json parses", False, f"Exit code: {result.exit_code}"
            )
    except Exception as e:
        add_check("car describe --json parses", False, str(e))

    try:
        result = runner.invoke(
            app, ["templates", "list", "--repo", str(repo), "--json"]
        )
        if result.exit_code == 0:
            try:
                list_output = json.loads(result.output)
                count = list_output.get("count", 0)
                add_check(
                    "templates list works",
                    True,
                    f"Found {count} templates",
                )
            except json.JSONDecodeError as e:
                add_check("templates list works", False, str(e))
        else:
            add_check("templates list works", False, f"Exit code: {result.exit_code}")
    except Exception as e:
        add_check("templates list works", False, str(e))

    temp_ticket_dir = tmp_path / "smoke_tickets"
    temp_ticket_dir.mkdir(exist_ok=True)

    try:
        result = runner.invoke(
            app,
            [
                "templates",
                "apply",
                "blessed:snippets/ticket_skeleton.md@main",
                "--ticket-dir",
                str(temp_ticket_dir),
                "--next",
                "--repo",
                str(repo),
            ],
        )
        if result.exit_code == 0:
            ticket_files = list(temp_ticket_dir.glob("TICKET-*.md"))
            add_check(
                "template apply works",
                len(ticket_files) > 0,
                f"Created {len(ticket_files)} ticket(s)",
            )

            if ticket_files:
                content = ticket_files[0].read_text()
                has_frontmatter = content.startswith("---")
                add_check(
                    "applied ticket has frontmatter",
                    has_frontmatter,
                    f"Starts with ---: {has_frontmatter}",
                )
        else:
            add_check(
                "template apply works",
                False,
                f"Exit code: {result.exit_code}, Output: {truncate(result.output)}",
            )
    except Exception as e:
        add_check("template apply works", False, str(e))

    try:
        result = runner.invoke(
            app,
            [
                "templates",
                "search",
                "bug",
                "--repo",
                str(repo),
                "--json",
            ],
        )
        if result.exit_code == 0:
            try:
                search_output = json.loads(result.output)
                query = search_output.get("query", "")
                add_check(
                    "templates search works",
                    query == "bug",
                    f"Query: {query}, Results: {search_output.get('count', 0)}",
                )
            except json.JSONDecodeError as e:
                add_check("templates search works", False, str(e))
        else:
            add_check("templates search works", False, f"Exit code: {result.exit_code}")
    except Exception as e:
        add_check("templates search works", False, str(e))

    report_dir = repo / ".codex-autorunner" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "latest-selfdescribe-smoke.md"

    report_lines = [
        "# Self-Description + Templates Smoke Test Report",
        "",
        f"**Status**: {results['status'].upper()}",
        "",
        "## Checks",
        "",
    ]

    for check in results["checks"]:
        status_icon = "✓" if check["passed"] else "✗"
        report_lines.append(
            f"- {status_icon} **{check['name']}**: {check.get('details', '')}"
        )

    report_lines.extend(
        [
            "",
            "## CAR Describe Output",
            "",
            "```json",
            json.dumps(describe_output, indent=2) if describe_output else "N/A",
            "```",
            "",
        ]
    )

    report_content = "\n".join(report_lines)
    report_path.write_text(report_content, encoding="utf-8")

    assert results["status"] == "pass", f"Smoke test failed: {results['checks']}"
