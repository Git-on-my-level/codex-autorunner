from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_svelte_warning_baseline import SvelteWarning, parse_svelte_warnings


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_svelte_warning_baseline.py"
    )


def test_parse_svelte_warnings_uses_repo_relative_paths() -> None:
    repo_root = Path("/repo")
    output = """
/repo/src/codex_autorunner/web_frontend/src/lib/components/PageHero.svelte:101:7
Warn: Also define the standard property 'line-clamp' for compatibility (css)
      display: -webkit-box;
"""

    assert parse_svelte_warnings(output, repo_root=repo_root) == (
        SvelteWarning(
            path="src/codex_autorunner/web_frontend/src/lib/components/PageHero.svelte",
            message="Also define the standard property 'line-clamp' for compatibility (css)",
        ),
    )


def test_warning_baseline_script_fails_on_new_warning(tmp_path: Path) -> None:
    output_path = tmp_path / "svelte-output.txt"
    output_path.write_text(
        """
/repo/src/codex_autorunner/web_frontend/src/lib/components/NewThing.svelte:12:3
Warn: New frontend warning (svelte)
""",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"warnings": []}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            str(output_path),
            "--baseline",
            str(baseline_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "New Svelte warnings found" in result.stderr
    assert "New frontend warning" in result.stderr


def test_warning_baseline_script_accepts_known_warning(tmp_path: Path) -> None:
    output_path = tmp_path / "svelte-output.txt"
    output_path.write_text(
        """
/repo/src/codex_autorunner/web_frontend/src/lib/components/PageHero.svelte:101:7
Warn: Also define the standard property 'line-clamp' for compatibility (css)
""",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "warnings": [
                    {
                        "path": "/repo/src/codex_autorunner/web_frontend/src/lib/components/PageHero.svelte",
                        "message": "Also define the standard property 'line-clamp' for compatibility (css)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            str(output_path),
            "--baseline",
            str(baseline_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
