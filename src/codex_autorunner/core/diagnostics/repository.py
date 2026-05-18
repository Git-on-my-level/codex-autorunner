from __future__ import annotations

import os
import subprocess
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

from ...voice.config import VoiceConfig
from ...voice.provider_catalog import (
    local_voice_provider_spec,
    missing_local_voice_runtime_commands,
)
from ..config import RepoConfig, load_repo_config
from ..locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock
from ..optional_dependencies import missing_optional_dependencies
from ..utils import resolve_executable
from .opencode import _append_opencode_lifecycle_checks
from .types import DoctorCheck, DoctorReport


def doctor(
    repo_root: Path,
    backend_orchestrator: Optional[Any] = None,
    check_id: Optional[str] = None,
) -> DoctorReport:
    """Run health checks on the repository.

    Args:
        repo_root: Repository root path.
        backend_orchestrator: Optional backend orchestrator for agent checks.
        check_id: Optional ID for specific check.

    Returns:
        DoctorReport with check results.
    """
    checks: list[DoctorCheck] = []

    # Check if in git repo
    try:
        from ..git_utils import run_git

        result = run_git(["rev-parse", "--is-inside-work-tree"], repo_root, check=False)
        if result.returncode != 0:
            checks.append(
                DoctorCheck(
                    name="Git repository",
                    passed=False,
                    message="Not a git repository",
                    check_id=check_id,
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Git repository",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        checks.append(
            DoctorCheck(
                name="Git repository",
                passed=False,
                message=f"Failed to check git: {e}",
                check_id=check_id,
            )
        )

    # Check config file
    config_path = repo_root / ".codex-autorunner" / "config.yml"
    if not config_path.exists():
        checks.append(
            DoctorCheck(
                name="Config file",
                passed=False,
                message=f"Config file not found: {config_path}",
                check_id=check_id,
            )
        )
    else:
        try:
            repo_config = load_repo_config(repo_root)
            checks.append(
                DoctorCheck(
                    name="Config file",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )
            _append_local_voice_dependency_check(
                checks, repo_config=repo_config, check_id=check_id
            )
            _append_opencode_lifecycle_checks(
                checks,
                repo_root=repo_root,
                repo_config=repo_config,
                backend_orchestrator=backend_orchestrator,
                check_id=check_id,
            )
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            checks.append(
                DoctorCheck(
                    name="Config file",
                    passed=False,
                    message=f"Failed to load: {e}",
                    check_id=check_id,
                )
            )

    # Check state directory
    state_root = repo_root / ".codex-autorunner"
    if not state_root.exists():
        checks.append(
            DoctorCheck(
                name="State directory",
                passed=False,
                message=f"State directory not found: {state_root}",
                severity="warning",
                check_id=check_id,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="State directory",
                passed=True,
                message="OK",
                severity="info",
                check_id=check_id,
            )
        )

    # Check for stale locks
    lock_path = state_root / "lock"
    if lock_path.exists():
        assessment = assess_lock(
            lock_path, expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS
        )
        if assessment.freeable:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=False,
                    message="Stale lock detected; run `car clear-stale-lock`",
                    severity="warning",
                    check_id=check_id,
                )
            )
        elif assessment.pid:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=True,
                    message=f"Active (pid={assessment.pid})",
                    severity="info",
                    check_id=check_id,
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Runner lock",
                    passed=True,
                    message="OK",
                    severity="info",
                    check_id=check_id,
                )
            )

    _append_render_dependency_checks(checks, check_id=check_id)

    return DoctorReport(checks)


def _append_local_voice_dependency_check(
    checks: list[DoctorCheck],
    *,
    repo_config: RepoConfig,
    check_id: Optional[str],
) -> None:
    voice_cfg = VoiceConfig.from_raw(repo_config.voice, env=os.environ)
    if not voice_cfg.enabled:
        return

    provider_spec = local_voice_provider_spec(voice_cfg.provider)
    if provider_spec is None:
        return
    provider, deps, extra = provider_spec

    missing_local_voice = missing_optional_dependencies(deps)
    missing_runtime_commands = missing_local_voice_runtime_commands(provider)
    if missing_local_voice:
        missing_desc = ", ".join(missing_local_voice)
        runtime_hint = ""
        if missing_runtime_commands:
            missing_runtime_desc = ", ".join(missing_runtime_commands)
            runtime_hint = (
                " Required runtime command(s) are also missing from PATH: "
                f"{missing_runtime_desc}."
            )
        checks.append(
            DoctorCheck(
                name="Voice dependencies",
                passed=False,
                message=(
                    f"Voice is enabled with {provider} but {missing_desc} is "
                    f"not installed.{runtime_hint}"
                ),
                severity="error",
                check_id=check_id or "voice.dependencies",
                fix=(
                    f"Install with `pip install codex-autorunner[{extra}]`."
                    + (
                        " Install ffmpeg and ensure it is on PATH (for macOS: "
                        "`brew install ffmpeg`)."
                        if "ffmpeg" in missing_runtime_commands
                        else ""
                    )
                ),
            )
        )
        return

    if missing_runtime_commands:
        missing_runtime_desc = ", ".join(missing_runtime_commands)
        checks.append(
            DoctorCheck(
                name="Voice dependencies",
                passed=False,
                message=(
                    f"Voice is enabled with {provider} but required runtime "
                    f"command(s) are missing from PATH: {missing_runtime_desc}."
                ),
                severity="error",
                check_id=check_id or "voice.dependencies",
                fix=(
                    "Install ffmpeg and ensure it is on PATH (for macOS: "
                    "`brew install ffmpeg`)."
                ),
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="Voice dependencies",
            passed=True,
            message=f"Voice local dependencies for {provider} are installed.",
            severity="info",
            check_id=check_id or "voice.dependencies",
        )
    )


def _append_render_dependency_checks(
    checks: list[DoctorCheck],
    *,
    check_id: Optional[str],
) -> None:
    render_check_id = check_id or "render.browser.dependencies"
    has_playwright = find_spec("playwright") is not None
    if not has_playwright:
        checks.append(
            DoctorCheck(
                name="Render browser dependencies",
                passed=True,
                message=(
                    "Playwright Python package is not installed; browser render "
                    "commands are unavailable."
                ),
                severity="warning",
                check_id=render_check_id,
                fix=(
                    "Install with `pip install codex-autorunner[browser]` (or "
                    "`pip install -e .[browser]` for local dev), then run "
                    "`python -m playwright install chromium`."
                ),
            )
        )
    else:
        chromium_path: Optional[str] = None
        chromium_error: Optional[str] = None
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            try:
                chromium_path = str(playwright.chromium.executable_path or "").strip()
            finally:
                playwright.stop()
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            chromium_error = str(exc).strip() or repr(exc)

        if chromium_path and Path(chromium_path).exists():
            checks.append(
                DoctorCheck(
                    name="Render browser dependencies",
                    passed=True,
                    message=f"Playwright and Chromium are available ({chromium_path}).",
                    severity="info",
                    check_id=render_check_id,
                )
            )
        else:
            detail = (
                f" ({chromium_error})"
                if chromium_error
                else " (Chromium browser binary missing)"
            )
            checks.append(
                DoctorCheck(
                    name="Render browser dependencies",
                    passed=True,
                    message=(
                        "Playwright is installed but Chromium runtime is unavailable."
                        f"{detail}"
                    ),
                    severity="warning",
                    check_id=render_check_id,
                    fix=(
                        "Install browser runtime with "
                        "`python -m playwright install chromium`."
                    ),
                )
            )

    mmdc = resolve_executable("mmdc")
    if mmdc:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=f"Mermaid CLI available at {mmdc}.",
                severity="info",
                check_id=check_id or "render.markdown.dependencies",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=(
                    "Mermaid CLI (mmdc) is not installed; `car render markdown` "
                    "diagram exports are unavailable."
                ),
                severity="warning",
                check_id=check_id or "render.markdown.dependencies",
                fix="Install Mermaid CLI (`npm i -g @mermaid-js/mermaid-cli`).",
            )
        )

    pandoc = resolve_executable("pandoc")
    if pandoc:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=f"Pandoc available at {pandoc}.",
                severity="info",
                check_id=check_id or "render.markdown.dependencies",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Render markdown dependencies",
                passed=True,
                message=(
                    "Pandoc is not installed; `car render markdown` document "
                    "exports are unavailable."
                ),
                severity="warning",
                check_id=check_id or "render.markdown.dependencies",
                fix="Install Pandoc and ensure it is on PATH.",
            )
        )


__all__ = ["doctor"]
