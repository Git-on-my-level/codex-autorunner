from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ...manifest import load_manifest


def format_pause_notification_source(
    *,
    workspace_root: Optional[Path],
    repo_id: Optional[str],
    hub_root: Optional[Path],
    manifest_path: Optional[Path],
    logger: Optional[logging.Logger] = None,
    debug_label: str = "chat.pause_notification.manifest_label_failed",
) -> str:
    workspace_label = str(workspace_root) if isinstance(workspace_root, Path) else ""
    repo_label = repo_id.strip() if isinstance(repo_id, str) else ""

    if hub_root and manifest_path and manifest_path.exists():
        try:
            manifest = load_manifest(manifest_path, hub_root)
            entry = (
                manifest.get_by_path(hub_root, workspace_root)
                if isinstance(workspace_root, Path)
                else None
            )
            if entry:
                repo_label = entry.id or repo_label
                if entry.display_name and entry.display_name != repo_label:
                    repo_label = f"{repo_label} ({entry.display_name})"
                if entry.kind == "worktree" and entry.worktree_of:
                    repo_label = f"{repo_label} [worktree of {entry.worktree_of}]"
        except (
            Exception
        ) as exc:  # intentional: defensive guard for best-effort label formatting from manifest
            if logger is not None:
                logger.debug(debug_label, exc_info=exc)

    if repo_label and workspace_label:
        return f"{repo_label} @ {workspace_label}"
    if repo_label:
        return repo_label
    if workspace_label:
        return workspace_label
    return "unknown workspace"


def format_pause_notification_text(
    *,
    run_id: str,
    dispatch_seq: str,
    content: str,
    source: Optional[str],
    resume_hint: str,
) -> str:
    body = content.strip() or "(no dispatch message)"
    header_lines = [
        f"Ticket flow paused (run {run_id}). Latest dispatch #{dispatch_seq}:"
    ]
    if isinstance(source, str) and source.strip():
        header_lines.append(f"Source: {source.strip()}")
    header = "\n".join(header_lines)
    footer = (
        f"Use {resume_hint.strip()} to continue."
        if isinstance(resume_hint, str) and resume_hint.strip()
        else ""
    )
    parts = [header, body]
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)
