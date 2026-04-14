"""Shared Typer options for hub root selection.

Canonical flag is ``--path`` so hub-scoped commands do not also advertise ``--hub``.
"""

from __future__ import annotations

from typing import Any

import typer

HUB_ROOT_PATH_HELP = (
    'Directory containing the hub ".codex-autorunner/" tree (defaults to cwd walk-up).'
)


def hub_root_path_option(
    *,
    help_text: str | None = None,
) -> Any:
    """Return a fresh ``--path`` option (Typer/Click options are not safely reusable)."""
    return typer.Option(None, "--path", help=help_text or HUB_ROOT_PATH_HELP)


RENDER_HUB_CONTEXT_HELP = (
    'Hub root or path to ".codex-autorunner/config.yml" '
    "(defaults to cwd walk-up). "
    "Named ``--hub-root`` here because ``render`` already uses ``--path`` for the URL path."
)


def hub_render_context_option(
    *,
    help_text: str | None = None,
) -> Any:
    """Hub/config selector for ``car render`` (avoids clashing with URL ``--path``)."""
    return typer.Option(None, "--hub-root", help=help_text or RENDER_HUB_CONTEXT_HELP)
