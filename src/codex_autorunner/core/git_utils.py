"""
Centralized Git utilities for consistent git operations across the codebase.
"""

import subprocess
from pathlib import Path
from typing import List, Optional

from .utils import subprocess_env


class GitError(Exception):
    """Raised when a git operation fails."""

    def __init__(self, message: str, *, returncode: int = 1):
        super().__init__(message)
        self.returncode = returncode


def run_git(
    args: List[str],
    cwd: Path,
    *,
    timeout_seconds: int = 30,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """
    Run a git command with consistent error handling.

    Args:
        args: Git subcommand and arguments (e.g., ["status", "--porcelain"])
        cwd: Working directory for the command
        timeout_seconds: Timeout in seconds
        check: If True, raise GitError on non-zero exit code

    Returns:
        CompletedProcess with stdout/stderr as text
    """
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise GitError("git binary not found", returncode=127) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git command timed out: git {' '.join(args)}", returncode=124
        ) from exc

    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise GitError(f"git {args[0]} failed: {detail}", returncode=proc.returncode)

    return proc


def git_available(repo_root: Path) -> bool:
    """Check if the directory is inside a git repository."""
    if not (repo_root / ".git").exists():
        return False
    try:
        proc = run_git(["rev-parse", "--is-inside-work-tree"], repo_root)
    except GitError:
        return False
    return proc.returncode == 0


def git_head_sha(repo_root: Path) -> Optional[str]:
    """Get the current HEAD SHA, or None if unavailable."""
    try:
        proc = run_git(["rev-parse", "HEAD"], repo_root)
    except GitError:
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else None


def git_branch(repo_root: Path) -> Optional[str]:
    """Get the current branch name, or None if detached HEAD or unavailable."""
    try:
        proc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    except GitError:
        return None
    branch = (proc.stdout or "").strip()
    if proc.returncode != 0 or not branch:
        return None
    if branch == "HEAD":
        return None
    return branch


def git_is_clean(repo_root: Path) -> bool:
    """Check if the working tree has no uncommitted changes."""
    try:
        proc = run_git(["status", "--porcelain"], repo_root, check=False)
    except GitError:
        return False
    if proc.returncode != 0:
        return False
    return not bool((proc.stdout or "").strip())


def git_ls_files(repo_root: Path) -> List[str]:
    """
    List all tracked files in the repository.

    Returns:
        List of relative file paths
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            capture_output=True,
            env=subprocess_env(),
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    paths = [p for p in proc.stdout.split(b"\x00") if p]
    decoded: List[str] = []
    for p in paths:
        try:
            decoded.append(p.decode("utf-8"))
        except UnicodeDecodeError:
            decoded.append(p.decode("utf-8", errors="replace"))
    return decoded


def git_diff_name_status(
    repo_root: Path, from_ref: str, to_ref: str = "HEAD"
) -> Optional[str]:
    """
    Get diff --name-status output between two refs.

    Returns:
        The diff output as a string, or None on error
    """
    try:
        proc = run_git(["diff", "--name-status", f"{from_ref}..{to_ref}"], repo_root)
    except GitError:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def git_status_porcelain(repo_root: Path) -> Optional[str]:
    """
    Get status --porcelain output.

    Returns:
        The status output as a string, or None on error
    """
    try:
        proc = run_git(["status", "--porcelain"], repo_root)
    except GitError:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def git_upstream_status(repo_root: Path) -> Optional[dict]:
    """
    Get upstream tracking status for the current branch.

    Returns:
        Dict with has_upstream, ahead, behind, or None if git is unavailable.
    """
    if not git_available(repo_root):
        return None
    try:
        proc = run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            repo_root,
            check=False,
        )
    except GitError:
        return None
    if proc.returncode != 0:
        return {"has_upstream": False, "ahead": 0, "behind": 0}
    proc = run_git(
        ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
        repo_root,
        check=False,
    )
    if proc.returncode != 0:
        return {"has_upstream": True, "ahead": 0, "behind": 0}
    raw = (proc.stdout or "").strip()
    ahead = 0
    behind = 0
    if raw:
        parts = raw.split()
        if len(parts) >= 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except ValueError:
                ahead = 0
                behind = 0
    return {"has_upstream": True, "ahead": ahead, "behind": behind}


def git_default_branch(repo_root: Path) -> Optional[str]:
    """Resolve the default branch name for origin, or None if unavailable."""
    try:
        proc = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_root)
    except GitError:
        proc = None
    if proc and proc.returncode == 0:
        raw = (proc.stdout or "").strip()
        if raw:
            return raw.split("/")[-1]
    try:
        proc = run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"], repo_root)
    except GitError:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    if raw.startswith("origin/"):
        return raw.split("/", 1)[1]
    return raw


def reset_branch_from_origin_main(repo_root: Path, branch_name: str) -> None:
    """
    Reset/create a local branch from origin/main in the current worktree.

    This is intended for chat /newt flows that should stay in-place rather than
    creating a new worktree.
    """
    branch = branch_name.strip()
    if not branch:
        raise GitError("branch name cannot be empty", returncode=2)

    status = run_git(
        ["status", "--porcelain"], repo_root, timeout_seconds=30, check=True
    )
    if (status.stdout or "").strip():
        raise GitError(
            "working tree has uncommitted changes; commit or stash before /newt",
            returncode=1,
        )

    run_git(["fetch", "origin", "main"], repo_root, timeout_seconds=60, check=True)
    run_git(
        ["checkout", "-B", branch, "origin/main"],
        repo_root,
        timeout_seconds=60,
        check=True,
    )


def git_diff_stats(
    repo_root: Path, from_ref: Optional[str] = None, *, include_staged: bool = True
) -> Optional[dict]:
    """
    Get diff statistics (insertions/deletions) for changes.

    Args:
        repo_root: Repository root path
        from_ref: Compare against this ref (e.g., a commit SHA). If None, compares
                  working tree against HEAD.
        include_staged: When from_ref is None, include staged changes in the diff.

    Returns:
        Dict with insertions, deletions, files_changed, or None on error.
        Example: {"insertions": 47, "deletions": 12, "files_changed": 5}
    """
    try:
        if from_ref:
            # Compare from_ref to working tree (includes all changes: committed + staged + unstaged)
            proc = run_git(["diff", "--numstat", from_ref], repo_root)
        elif include_staged:
            # Working tree + staged vs HEAD
            proc = run_git(["diff", "--numstat", "HEAD"], repo_root)
        else:
            # Only unstaged changes
            proc = run_git(["diff", "--numstat"], repo_root)
    except GitError:
        return None
    if proc.returncode != 0:
        return None

    insertions = 0
    deletions = 0
    files_changed = 0

    for line in (proc.stdout or "").strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # Binary files show "-" for both counts
        add_str, del_str = parts[0], parts[1]
        if add_str != "-":
            try:
                insertions += int(add_str)
            except ValueError:
                pass
        if del_str != "-":
            try:
                deletions += int(del_str)
            except ValueError:
                pass
        files_changed += 1

    return {
        "insertions": insertions,
        "deletions": deletions,
        "files_changed": files_changed,
    }
