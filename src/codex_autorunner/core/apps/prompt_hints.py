from __future__ import annotations

import logging
from pathlib import Path

from .install import (
    InstalledAppInfo,
    get_installed_app,
    installed_apps_root,
)

_logger = logging.getLogger(__name__)

MAX_TOOLS_PER_APP = 5
MAX_APPS = 10
MAX_HINT_BYTES = 2048

_APPS_STATE_REL = ".codex-autorunner/apps"


def _tolerant_list_installed_apps(repo_root: Path) -> list[InstalledAppInfo]:
    apps_root = installed_apps_root(repo_root)
    if not apps_root.exists():
        return []

    apps: list[InstalledAppInfo] = []
    for child in sorted(apps_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        if not (child / "app.lock.json").exists():
            continue
        try:
            info = get_installed_app(repo_root, child.name)
        except Exception as exc:
            _logger.debug("Skipping invalid installed app %s: %s", child.name, exc)
            continue
        if info is None:
            continue
        apps.append(info)
    return apps


def build_installed_apps_prompt_hint(repo_root: Path) -> str:
    try:
        installed = _tolerant_list_installed_apps(repo_root)
    except Exception as exc:
        _logger.warning("Failed to list installed apps for prompt hint: %s", exc)
        return ""

    if not installed:
        return ""

    lines: list[str] = ["Installed CAR apps:"]
    for app in installed[:MAX_APPS]:
        lines.append(f"- {app.app_id} v{app.app_version}")
        tool_ids = list(app.manifest.tools.keys())
        shown = tool_ids[:MAX_TOOLS_PER_APP]
        tool_examples = "; ".join(
            f"car apps run {app.app_id} {tid} -- ..." for tid in shown
        )
        if tool_examples:
            lines.append(f"  Tools: {tool_examples}")
        if len(tool_ids) > MAX_TOOLS_PER_APP:
            lines.append(f"  (... and {len(tool_ids) - MAX_TOOLS_PER_APP} more tools)")
        lines.append(f"  State: {_APPS_STATE_REL}/{app.app_id}/state/")
        lines.append(f"  Artifacts: {_APPS_STATE_REL}/{app.app_id}/artifacts/")

    hint = "\n".join(lines)
    if len(hint.encode("utf-8")) > MAX_HINT_BYTES:
        hint = hint[: MAX_HINT_BYTES - 3] + "..."
    return hint


__all__ = [
    "MAX_APPS",
    "MAX_HINT_BYTES",
    "MAX_TOOLS_PER_APP",
    "build_installed_apps_prompt_hint",
]
