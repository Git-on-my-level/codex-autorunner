from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional

from ..config import TemplateRepoConfig
from ..git_utils import GitError, run_git
from ..state_roots import resolve_hub_templates_root


class RepoNotConfiguredError(Exception):
    def __init__(self, repo_id: str, *, detail: Optional[str] = None) -> None:
        message = f"Template repo not configured: {repo_id}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.repo_id = repo_id
        self.detail = detail


class TemplateNotFoundError(Exception):
    def __init__(self, repo_id: str, path: str, ref: str) -> None:
        super().__init__(f"Template not found: repo_id={repo_id} path={path} ref={ref}")
        self.repo_id = repo_id
        self.path = path
        self.ref = ref


class RefNotFoundError(Exception):
    def __init__(self, repo_id: str, ref: str) -> None:
        super().__init__(f"Ref not found: repo_id={repo_id} ref={ref}")
        self.repo_id = repo_id
        self.ref = ref


class NetworkUnavailableError(Exception):
    def __init__(
        self,
        repo_id: str,
        ref: str,
        path: str,
        *,
        detail: Optional[str] = None,
    ) -> None:
        message = (
            "Template fetch failed and cache is unavailable: "
            f"repo_id={repo_id} ref={ref} path={path}"
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.repo_id = repo_id
        self.ref = ref
        self.path = path
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class TemplateRef:
    repo_id: str
    path: str
    ref: Optional[str]


@dataclasses.dataclass(frozen=True)
class FetchedTemplate:
    repo_id: str
    url: str
    trusted: bool
    path: str
    ref: str
    commit_sha: str
    blob_sha: str
    content: str


def parse_template_ref(raw: str) -> TemplateRef:
    """Parse canonical template reference strings: REPO_ID:PATH[@REF]."""
    if ":" not in raw:
        raise ValueError("template ref must be formatted as REPO_ID:PATH[@REF]")
    repo_id, remainder = raw.split(":", 1)
    if not repo_id:
        raise ValueError("template ref missing repo_id")
    if not remainder:
        raise ValueError("template ref missing path")

    path: str
    ref: Optional[str]
    if "@" in remainder:
        path, ref = remainder.rsplit("@", 1)
        if not ref:
            raise ValueError("template ref missing ref after '@'")
    else:
        path, ref = remainder, None

    if not path:
        raise ValueError("template ref missing path")

    return TemplateRef(repo_id=repo_id, path=path, ref=ref)


def ensure_git_mirror(repo: TemplateRepoConfig, hub_root: Path) -> Path:
    templates_root = resolve_hub_templates_root(hub_root)
    mirror_path = templates_root / "git" / f"{repo.id}.git"
    if mirror_path.exists():
        _ensure_origin_remote(mirror_path, repo.url)
        return mirror_path

    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--bare", str(mirror_path)], mirror_path.parent, check=True)
    _ensure_origin_remote(mirror_path, repo.url)
    return mirror_path


def _ensure_origin_remote(mirror_path: Path, url: str) -> None:
    try:
        proc = run_git(["remote", "get-url", "origin"], mirror_path, check=False)
    except GitError:
        proc = None
    if proc and proc.returncode == 0:
        current = (proc.stdout or "").strip()
        if current and current != url:
            run_git(["remote", "set-url", "origin", url], mirror_path, check=True)
    else:
        run_git(["remote", "add", "origin", url], mirror_path, check=True)
    _configure_mirror_remote(mirror_path)


def _configure_mirror_remote(mirror_path: Path) -> None:
    run_git(
        ["config", "remote.origin.fetch", "+refs/*:refs/*"],
        mirror_path,
        check=True,
    )
    run_git(["config", "remote.origin.mirror", "true"], mirror_path, check=True)


def fetch_template(
    *,
    repo: TemplateRepoConfig,
    hub_root: Path,
    template_ref: str,
    fetch_timeout_seconds: int = 30,
) -> FetchedTemplate:
    parsed = parse_template_ref(template_ref)
    if parsed.repo_id != repo.id:
        raise RepoNotConfiguredError(
            parsed.repo_id,
            detail=f"expected repo_id {repo.id}",
        )

    ref = parsed.ref or repo.default_ref
    mirror_path = ensure_git_mirror(repo, hub_root)

    fetch_error: Optional[str] = None
    try:
        run_git(
            ["fetch", "--prune", "origin"],
            mirror_path,
            timeout_seconds=fetch_timeout_seconds,
            check=True,
        )
    except GitError as exc:
        fetch_error = str(exc)

    try:
        commit_sha = _resolve_commit(mirror_path, repo.id, ref)
        blob_sha = _resolve_blob(mirror_path, commit_sha, parsed.path, repo.id, ref)
        content = _read_blob(mirror_path, blob_sha)
    except (RefNotFoundError, TemplateNotFoundError) as exc:
        if fetch_error:
            raise NetworkUnavailableError(
                repo.id,
                ref,
                parsed.path,
                detail=fetch_error,
            ) from exc
        raise

    return FetchedTemplate(
        repo_id=repo.id,
        url=repo.url,
        trusted=repo.trusted,
        path=parsed.path,
        ref=ref,
        commit_sha=commit_sha,
        blob_sha=blob_sha,
        content=content,
    )


def _resolve_commit(mirror_path: Path, repo_id: str, ref: str) -> str:
    try:
        proc = run_git(
            ["rev-parse", f"{ref}^{{commit}}"],
            mirror_path,
            check=True,
        )
    except GitError as exc:
        raise RefNotFoundError(repo_id, ref) from exc
    return (proc.stdout or "").strip()


def _resolve_blob(
    mirror_path: Path,
    commit_sha: str,
    path: str,
    repo_id: str,
    ref: str,
) -> str:
    try:
        proc = run_git(
            ["ls-tree", commit_sha, "--", path],
            mirror_path,
            check=True,
        )
    except GitError as exc:
        raise TemplateNotFoundError(repo_id, path, ref) from exc

    raw = (proc.stdout or "").strip()
    if not raw:
        raise TemplateNotFoundError(repo_id, path, ref)

    # Format: "<mode> <type> <sha>\t<path>"
    parts = raw.split()
    if len(parts) < 3:
        raise TemplateNotFoundError(repo_id, path, ref)
    return parts[2]


def _read_blob(mirror_path: Path, blob_sha: str) -> str:
    proc = run_git(["cat-file", "-p", blob_sha], mirror_path, check=True)
    return proc.stdout or ""
