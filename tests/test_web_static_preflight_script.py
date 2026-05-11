from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.check_web_static_preflight import missing_static_for_paths


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_web_static_preflight.py"
    )


def test_check_script_feeds_deletions_and_renames_to_preflight() -> None:
    check_script = (
        Path(__file__).resolve().parents[1] / "scripts" / "check.sh"
    ).read_text(encoding="utf-8")

    assert "--diff-filter=ACMRD" in check_script


def test_missing_static_for_web_source_change() -> None:
    assert missing_static_for_paths(
        ["src/codex_autorunner/web_frontend/src/routes/+page.svelte"]
    ) == ("src/codex_autorunner/web_frontend/src/routes/+page.svelte",)


def test_missing_static_normalizes_changed_paths_with_shared_lane_helper() -> None:
    assert missing_static_for_paths(
        [".\\src\\codex_autorunner\\web_frontend\\src\\routes\\+page.svelte"]
    ) == ("src/codex_autorunner/web_frontend/src/routes/+page.svelte",)


def test_missing_static_for_deleted_web_source_path() -> None:
    assert missing_static_for_paths(
        ["src/codex_autorunner/web_frontend/src/routes/old/+page.svelte"]
    ) == ("src/codex_autorunner/web_frontend/src/routes/old/+page.svelte",)


def test_static_output_satisfies_web_source_change() -> None:
    assert (
        missing_static_for_paths(
            [
                "src/codex_autorunner/web_frontend/src/routes/+page.svelte",
                "src/codex_autorunner/web_static/_app/immutable/chunks/app.js",
            ]
        )
        == ()
    )


def test_web_frontend_tests_do_not_require_static_refresh() -> None:
    assert (
        missing_static_for_paths(
            ["src/codex_autorunner/web_frontend/src/routes/chats/page.test.ts"]
        )
        == ()
    )


def test_preflight_script_fails_with_actionable_message() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "src/codex_autorunner/web_frontend/src/routes/+page.svelte",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "pnpm run build" in result.stderr
    assert "src/codex_autorunner/web_static/" in result.stderr
