#!/usr/bin/env python3
"""Emit a Playwright demo manifest for `make ui-qa-screens`.

Full-page shots include **every** hub ``uiMock`` scenario (see
``static_src/uiMockScenarios.ts``) so QA screenshots stay deterministic. Extra
query params: ``uiMockStrip=1`` (clean URL in the bar); some scenarios add
``view=pma`` (see :data:`_SCENARIO_GOTO_TWEAKS`).

Repo UI lives under ``/repos/{repo_id}/`` with ``?tab=`` for the tab strip (see
``static_src/tabs.ts``). Paths like ``/tickets`` at the hub root are not the
in-repo shell and are misleading for screenshots.

Environment:
  UI_QA_HUB_ROOT   Hub root (default: cwd)
  UI_QA_MANIFEST   Output path (default: .codex-autorunner/render/hub_ui_screens.gen.yaml)
  UI_QA_REPO_ID    Force a repo id (skips reading .codex-autorunner/manifest.yml)
  UI_QA_UI_MOCKS   If ``0``/``false``, capture only a single unmocked ``/`` hub
                   shot (``01-hub-home.png``) for quick debugging. Default: all
                   ``uiMock`` scenarios are captured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import yaml

_MANIFEST_VERSION = 1
_POST_LOAD_WAIT_MS = 1500

#: Keep in sync with ``UI_MOCK_SCENARIO_ORDER`` in ``static_src/uiMockScenarios.ts``
#: (node import from generated ``uiMockScenarios.js`` overrides when available).
UI_MOCK_SCENARIO_ORDER_FALLBACK: tuple[str, ...] = (
    "empty",
    "healthy",
    "pma-healthy",
    "running",
    "worktrees-and-flow",
    "error-and-missing",
    "channel-directory",
    "usage-loading",
    "onboarding",
    "pma-agents-ok",
)

#: Per-scenario extra query params (path still ``/``).
_SCENARIO_GOTO_TWEAKS: dict[str, dict[str, str]] = {
    "pma-agents-ok": {"view": "pma"},
    # Force the same reset path docs recommend so the onboarding PMA shot stays
    # deterministic even when the browser already has PMA local state.
    "onboarding": {"view": "pma", "carOnboarding": "1"},
}

#: Relative to hub web root; query selects the repo-shell tab. Second value is
#: the filename *stem* (numbered prefix is prepended in :func:`build_steps`).
_REPO_TABS: tuple[tuple[str, str], ...] = (
    ("tickets", "tickets.png"),
    ("inbox", "inbox.png"),
    ("contextspace", "contextspace.png"),
    ("terminal", "terminal.png"),
    ("analytics", "analytics.png"),
    ("archive", "archive.png"),
)


def _env_ui_mocks_enabled() -> bool:
    raw = (os.environ.get("UI_QA_UI_MOCKS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _step_goto(url: str) -> dict[str, Any]:
    return {"action": "goto", "url": url, "wait_until": "load"}


def _step_wait() -> dict[str, Any]:
    return {"action": "wait_ms", "ms": _POST_LOAD_WAIT_MS}


def _step_shot(name: str) -> dict[str, Any]:
    return {"action": "screenshot", "output": name}


def _load_ui_mock_scenario_ids(hub_root: Path) -> list[str]:
    gen = (
        hub_root
        / "src"
        / "codex_autorunner"
        / "static"
        / "generated"
        / "uiMockScenarios.js"
    )
    if not gen.is_file():
        return list(UI_MOCK_SCENARIO_ORDER_FALLBACK)
    uri = gen.resolve().as_uri()
    code = (
        "import * as m from " + json.dumps(uri) + "; "
        "process.stdout.write(JSON.stringify(m.UI_MOCK_SCENARIO_ORDER));"
    )
    try:
        raw = subprocess.check_output(
            ["node", "--input-type=module", "-e", code],
            text=True,
            timeout=15,
        )
        return list(json.loads(raw))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(
            f"ui-qa: could not read UI_MOCK_SCENARIO_ORDER from {gen} ({exc!r}); "
            f"using built-in fallback list.",
            file=sys.stderr,
        )
        return list(UI_MOCK_SCENARIO_ORDER_FALLBACK)


def _hub_url_for_ui_mock(sid: str) -> str:
    pairs: list[tuple[str, str]] = [("uiMock", sid), ("uiMockStrip", "1")]
    for k, v in _SCENARIO_GOTO_TWEAKS.get(sid, {}).items():
        pairs.append((k, v))
    return "/?" + urlencode(pairs)


def build_steps(repo_id: Optional[str], hub_root: Path) -> list[dict[str, Any]]:
    """Return demo manifest step rows: all hub uiMock shots, then optional repo tabs."""
    steps: list[dict[str, Any]] = []
    if not _env_ui_mocks_enabled():
        for step in (
            _step_goto("/"),
            _step_wait(),
            _step_shot("01-hub-home.png"),
        ):
            steps.append(step)
    else:
        mock_ids = _load_ui_mock_scenario_ids(hub_root)
        for i, sid in enumerate(mock_ids, start=1):
            u = _hub_url_for_ui_mock(sid)
            for step in (
                _step_goto(u),
                _step_wait(),
                _step_shot(f"{i:02d}-ui-mock-{sid}.png"),
            ):
                steps.append(step)

    if not repo_id:
        return steps

    base = f"/repos/{repo_id}/"
    n0 = len([s for s in steps if s.get("action") == "screenshot"])
    for j, (tab, out_name) in enumerate(_REPO_TABS, start=1):
        u = f"{base}?tab={tab}"
        idx = n0 + j
        for step in (
            _step_goto(u),
            _step_wait(),
            _step_shot(f"{idx:02d}-repo-{out_name}"),
        ):
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


def build_manifest_dict(repo_id: Optional[str], hub_root: Path) -> dict[str, Any]:
    return {"version": _MANIFEST_VERSION, "steps": build_steps(repo_id, hub_root)}


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
            "ui-qa: no repo in manifest; only capturing hub (ui-mock) screenshots. "
            "Add a repo to .codex-autorunner/manifest.yml or set UI_QA_REPO_ID for repo tabs.",
            file=sys.stderr,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_manifest_dict(repo_id, hub_root)
    out.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
