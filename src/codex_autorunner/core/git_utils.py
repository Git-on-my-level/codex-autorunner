"""
Centralized Git utilities for consistent git operations across the codebase.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .locks import file_lock
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


def git_linked_worktree_git_dir(repo_root: Path) -> Optional[Path]:
    """Return the linked-worktree gitdir when ``repo_root`` is a non-main worktree."""
    git_path = repo_root.resolve() / ".git"
    if not git_path.exists() or git_path.is_dir():
        return None
    try:
        raw = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.lower().startswith("gitdir:"):
        return None
    git_dir = Path(raw.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (repo_root.resolve() / git_dir).resolve()
    if git_dir.parent.name != "worktrees":
        return None
    return git_dir.resolve()


def git_linked_worktree_common_dir(repo_root: Path) -> Optional[Path]:
    git_dir = git_linked_worktree_git_dir(repo_root)
    if git_dir is None:
        return None
    return git_dir.parent.parent.resolve()


def git_linked_worktree_base_root(repo_root: Path) -> Optional[Path]:
    common_git_dir = git_linked_worktree_common_dir(repo_root)
    if common_git_dir is None:
        return None
    return common_git_dir.parent.resolve()


def _resolved_git_directory_from_gitfile(repo_root: Path) -> Optional[Path]:
    """Resolve the git directory when ``.git`` is a gitfile (not a directory)."""
    git_path = repo_root.resolve() / ".git"
    if not git_path.exists() or git_path.is_dir():
        return None
    try:
        raw = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.lower().startswith("gitdir:"):
        return None
    git_dir = Path(raw.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (repo_root.resolve() / git_dir).resolve()
    return git_dir.resolve()


def git_mutation_lock_path(repo_root: Path) -> Path:
    git_path = repo_root.resolve() / ".git"
    if git_path.is_dir():
        return git_path / "codex-autorunner-git-mutation.lock"
    common_git_dir = git_linked_worktree_common_dir(repo_root)
    if common_git_dir is not None:
        return common_git_dir / "codex-autorunner-git-mutation.lock"
    resolved_git_dir = _resolved_git_directory_from_gitfile(repo_root)
    if resolved_git_dir is not None:
        return resolved_git_dir / "codex-autorunner-git-mutation.lock"
    return git_path / "codex-autorunner-git-mutation.lock"


@contextmanager
def git_mutation_lock(repo_root: Path) -> Iterator[None]:
    with file_lock(git_mutation_lock_path(repo_root)):
        yield


def git_failure_detail(proc: Any) -> str:
    return (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"


def resolve_ref_sha(repo_root: Path, ref: str) -> str:
    try:
        proc = run_git(["rev-parse", "--verify", ref], repo_root, check=False)
    except GitError as exc:
        raise ValueError(f"git rev-parse failed for {ref}: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"Unable to resolve ref {ref}: {git_failure_detail(proc)}")
    sha = (proc.stdout or "").strip()
    if not sha:
        raise ValueError(f"Unable to resolve ref {ref}: empty output")
    return sha


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


def git_submodule_paths(repo_root: Path) -> list[str]:
    """Return configured submodule paths for the repository."""
    if not (repo_root / ".gitmodules").exists():
        return []
    try:
        proc = run_git(
            [
                "config",
                "--file",
                ".gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            repo_root,
            check=False,
        )
    except GitError:
        return []
    if proc.returncode != 0:
        return []

    paths: list[str] = []
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        path = parts[1].strip()
        if path:
            paths.append(path)
    return paths


def git_has_submodules(repo_root: Path) -> bool:
    """Return True when the repository declares at least one submodule."""
    return bool(git_submodule_paths(repo_root))


def _status_path_from_porcelain_line(line: str) -> str:
    path = line[3:].strip() if len(line) > 3 else ""
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    return path


def describe_newt_reject_reasons(
    repo_root: Path, *, max_examples_per_bucket: int = 2
) -> list[str]:
    """
    Summarize the working tree blockers that prevent /newt from running.

    Returns:
        Human-readable reason lines suitable for a compact UI list.
    """
    status = git_status_porcelain(repo_root)
    if status is None:
        return ["Unable to inspect git status for this workspace."]
    if not status.strip():
        return []

    submodule_paths = set(git_submodule_paths(repo_root))
    buckets: list[tuple[str, list[str]]] = [
        ("merge conflict", []),
        ("submodule change", []),
        ("staged tracked change", []),
        ("unstaged tracked change", []),
        ("untracked path", []),
    ]
    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

    for raw_line in status.splitlines():
        line = raw_line.rstrip()
        if len(line) < 2:
            continue
        code = line[:2]
        path = _status_path_from_porcelain_line(line)
        if code == "??":
            buckets[4][1].append(path)
            continue
        if code in conflict_codes:
            buckets[0][1].append(path)
            continue
        if path in submodule_paths:
            buckets[1][1].append(path)
            continue
        if code[0] not in {" ", "?"}:
            buckets[2][1].append(path)
        if code[1] not in {" ", "?"}:
            buckets[3][1].append(path)

    reasons: list[str] = []
    for label, paths in buckets:
        count = len(paths)
        if count <= 0:
            continue
        suffix = "" if count == 1 else "s"
        reason = f"{count} {label}{suffix}"
        examples = [item for item in paths if item][:max_examples_per_bucket]
        if examples:
            rendered = ", ".join(f"`{item}`" for item in examples)
            reason += f", including {rendered}"
        reasons.append(reason)
    return reasons


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


def reset_branch_from_origin_main(repo_root: Path, branch_name: str) -> str:
    """
    Reset/create a local branch from the origin default branch in-place.

    This is intended for chat /newt flows that should stay in-place rather than
    creating a new worktree.

    Returns:
        The resolved origin default branch name.
    """
    branch = branch_name.strip()
    if not branch:
        raise GitError("branch name cannot be empty", returncode=2)

    with git_mutation_lock(repo_root):
        status = run_git(
            ["status", "--porcelain"], repo_root, timeout_seconds=30, check=True
        )
        if (status.stdout or "").strip():
            raise GitError(
                "working tree has uncommitted changes; commit or stash before /newt",
                returncode=1,
            )

        run_git(
            ["fetch", "--prune", "origin"],
            repo_root,
            timeout_seconds=120,
            check=True,
        )

        default_branch = git_default_branch(repo_root)
        if not default_branch:
            raise GitError("unable to resolve origin default branch", returncode=1)

        run_git(
            ["checkout", "-B", branch, f"origin/{default_branch}"],
            repo_root,
            timeout_seconds=60,
            check=True,
        )
        sync_submodules_to_head(repo_root)
    return default_branch


def reset_worktree_to_head(repo_root: Path) -> None:
    """Discard tracked changes in the current worktree."""
    run_git(["reset", "--hard", "HEAD"], repo_root, timeout_seconds=60, check=True)
    if not git_has_submodules(repo_root):
        return
    run_git(
        ["submodule", "foreach", "--recursive", "git reset --hard"],
        repo_root,
        timeout_seconds=120,
        check=True,
    )
    sync_submodules_to_head(repo_root)


def clean_untracked_worktree(repo_root: Path) -> None:
    """Remove untracked paths, including nested git dirs, from the worktree."""
    run_git(["clean", "-ffd"], repo_root, timeout_seconds=60, check=True)
    if not git_has_submodules(repo_root):
        return
    run_git(
        ["submodule", "foreach", "--recursive", "git clean -ffd"],
        repo_root,
        timeout_seconds=120,
        check=True,
    )


def sync_submodules_to_head(repo_root: Path) -> None:
    """Align submodule working trees to the commits recorded in the superproject."""
    if not git_has_submodules(repo_root):
        return
    run_git(
        ["submodule", "update", "--recursive", "--checkout", "--force"],
        repo_root,
        timeout_seconds=180,
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
