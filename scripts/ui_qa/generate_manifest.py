#!/usr/bin/env python3
"""Emit a Playwright demo manifest for `make ui-qa-screens`.

Repo UI lives under ``/repos/{repo_id}/`` with ``?tab=`` for the tab strip (see
``static_src/tabs.ts``). Paths like ``/tickets`` at the hub root are not the
in-repo shell and are misleading for screenshots.

Environment:
  UI_QA_HUB_ROOT   Hub root (default: cwd)
  UI_QA_MANIFEST   Output path (default: .codex-autorunner/render/hub_ui_screens.gen.yaml)
  UI_QA_REPO_ID    Force a repo id (skips reading .codex-autorunner/manifest.yml)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

_MANIFEST_VERSION = 1
_POST_LOAD_WAIT_MS = 1500

#: Relative to hub web root; query selects the repo-shell tab.
_REPO_TABS: tuple[tuple[str, str], ...] = (
    ("tickets", "02-tickets.png"),
    ("inbox", "03-inbox.png"),
    ("contextspace", "04-contextspace.png"),
    ("terminal", "05-terminal.png"),
    ("analytics", "06-analytics.png"),
    ("archive", "07-archive.png"),
)


def _step_goto(url: str) -> dict[str, Any]:
    return {"action": "goto", "url": url, "wait_until": "load"}


def _step_wait() -> dict[str, Any]:
    return {"action": "wait_ms", "ms": _POST_LOAD_WAIT_MS}


def _step_shot(name: str) -> dict[str, Any]:
    return {"action": "screenshot", "output": name}


def build_steps(repo_id: Optional[str]) -> list[dict[str, Any]]:
    """Return demo manifest step rows for hub home plus optional per-tab repo shots."""
    steps: list[dict[str, Any]] = []
    for step in (
        _step_goto("/"),
        _step_wait(),
        _step_shot("01-hub-home.png"),
    ):
        steps.append(step)
    if not repo_id:
        return steps
    base = f"/repos/{repo_id}/"
    for tab, out in _REPO_TABS:
        u = f"{base}?tab={tab}"
        for step in (_step_goto(u), _step_wait(), _step_shot(out)):
            steps.append(step)
    return steps


def resolve_repo_id(hub_root: Path) -> Optional[str]:
    override = (os.environ.get("UI_QA_REPO_ID") or "").strip()
    if override:
        return override
    from codex_autorunner.core.config_builders import load_hub_config
    from codex_autorunner.manifest import load_manifest

    cfg = load_hub_config(hub_root)
    manifest = load_manifest(cfg.manifest_path, cfg.root)
    if not manifest.repos:
        return None
    return manifest.repos[0].id


def build_manifest_dict(repo_id: Optional[str]) -> dict[str, Any]:
    return {"version": _MANIFEST_VERSION, "steps": build_steps(repo_id)}


def main() -> int:
    hub_root = Path(os.environ.get("UI_QA_HUB_ROOT", ".")).resolve()
    out = Path(
        os.environ.get(
            "UI_QA_MANIFEST",
            str(hub_root / ".codex-autorunner/render/hub_ui_screens.gen.yaml"),
        )
    )
    repo_id = resolve_repo_id(hub_root)
    if not repo_id:
        print(
            "ui-qa: no repo in manifest; only capturing hub home. "
            "Add a repo to .codex-autorunner/manifest.yml or set UI_QA_REPO_ID.",
            file=sys.stderr,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_manifest_dict(repo_id)
    out.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
