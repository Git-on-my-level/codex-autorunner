"""Git checkpoint utilities for ticket runners.

This module provides git checkpoint operations extracted from Engine.
"""

import logging
from pathlib import Path
from typing import Optional

from .config import RepoConfig
from .git_utils import GitError, run_git

_logger = logging.getLogger(__name__)


def maybe_git_commit(
    repo_root: Path,
    state_path: Path,
    run_id: int,
    config: RepoConfig,
) -> Optional[str]:
    """
    Create a git commit for workspace docs if configured and working tree is clean.

    Returns an error string if commit failed, else None.
    """
    if not config.git_auto_commit:
        return None

    paths = []
    for key in ("active_context", "decisions", "spec"):
        try:
            paths.append(config.doc_path(key))
        except KeyError:
            pass
    add_paths = [str(p.relative_to(repo_root)) for p in paths if p.exists()]
    if not add_paths:
        return None

    try:
        add_proc = run_git(["add", *add_paths], repo_root, check=False)
        if add_proc.returncode != 0:
            detail = (
                add_proc.stderr or add_proc.stdout or ""
            ).strip() or f"exit {add_proc.returncode}"
            return f"git add failed: {detail}"
    except GitError as exc:
        return f"git add failed: {exc}"

    msg = config.git_commit_message_template.replace("{run_id}", str(run_id)).replace(
        "#{run_id}", str(run_id)
    )

    try:
        commit_proc = run_git(
            ["commit", "-m", msg],
            repo_root,
            check=False,
            timeout_seconds=120,
        )
        if commit_proc.returncode != 0:
            detail = (
                commit_proc.stderr or commit_proc.stdout or ""
            ).strip() or f"exit {commit_proc.returncode}"
            return f"git commit failed: {detail}"
    except GitError as exc:
        return f"git commit failed: {exc}"

    return None
