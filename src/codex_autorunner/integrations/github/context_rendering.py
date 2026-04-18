"""Context-file generation and formatting helpers for GitHub issues/PRs.

Extracted from GitHubService so that generic gh reads stay distinct from
context-rendering and file-generation concerns.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from ...core.injected_context import wrap_injected_context
from ...core.utils import atomic_write


def safe_text(value: Any, *, max_chars: int = 8000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def format_labels(labels: Any) -> str:
    if not isinstance(labels, list):
        return "none"
    names = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            names.append(str(name))
    return ", ".join(names) if names else "none"


def format_author(author: Any) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "unknown")
    return str(author or "unknown")


def format_issue_context(issue: dict, *, repo: str) -> list[str]:
    number = issue.get("number") or ""
    title = issue.get("title") or ""
    url = issue.get("url") or ""
    state = issue.get("state") or ""
    body = safe_text(issue.get("body") or "")
    labels = format_labels(issue.get("labels"))
    author = format_author(issue.get("author"))
    comments = issue.get("comments")
    comment_count = 0
    if isinstance(comments, dict):
        total = comments.get("totalCount")
        if isinstance(total, int):
            comment_count = total
        else:
            nodes = comments.get("nodes")
            edges = comments.get("edges")
            if isinstance(nodes, list):
                comment_count = len(nodes)
            elif isinstance(edges, list):
                comment_count = len(edges)
    elif isinstance(comments, list):
        comment_count = len(comments)

    return [
        "# GitHub Issue Context",
        f"Repo: {repo}",
        f"Issue: #{number} {title}".strip(),
        f"URL: {url}",
        f"State: {state}",
        f"Author: {author}",
        f"Labels: {labels}",
        f"Comments: {comment_count}",
        "",
        "Body:",
        body or "(no body)",
    ]


def format_review_location(path: Any, line: Any) -> str:
    path_val = str(path).strip() if path else ""
    if path_val and isinstance(line, int):
        return f"{path_val}:{line}"
    if path_val:
        return path_val
    if isinstance(line, int):
        return f"(unknown file):{line}"
    return "(unknown file)"


def format_review_threads(review_threads: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    thread_index = 0
    for thread in review_threads:
        if not isinstance(thread, dict):
            continue
        comments = thread.get("comments")
        if not isinstance(comments, list) or not comments:
            continue
        thread_index += 1
        status = "resolved" if thread.get("isResolved") else "unresolved"
        lines.append(f"- Thread {thread_index} ({status})")
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            author = format_author(comment.get("author"))
            created_at = comment.get("createdAt") or ""
            location = format_review_location(comment.get("path"), comment.get("line"))
            header = f"  - {location} {author}".strip()
            if created_at:
                header = f"{header} ({created_at})"
            lines.append(header)
            body = safe_text(comment.get("body") or "")
            if not body:
                lines.append("    (no body)")
            else:
                for line in body.splitlines():
                    lines.append(f"    {line}")
    return lines


def format_pr_context(
    pr: dict, *, repo: str, review_threads: Optional[list[dict[str, Any]]] = None
) -> list[str]:
    number = pr.get("number") or ""
    title = pr.get("title") or ""
    url = pr.get("url") or ""
    state = pr.get("state") or ""
    body = safe_text(pr.get("body") or "")
    labels = format_labels(pr.get("labels"))
    author = format_author(pr.get("author"))
    additions = pr.get("additions") or 0
    deletions = pr.get("deletions") or 0
    changed_files = pr.get("changedFiles") or 0
    files_raw = pr.get("files")
    files = (
        [entry for entry in files_raw if isinstance(entry, dict)]
        if isinstance(files_raw, list)
        else []
    )
    file_lines = []
    for entry in files[:200]:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path") or entry.get("name") or ""
        if not path:
            continue
        add = entry.get("additions")
        dele = entry.get("deletions")
        if isinstance(add, int) and isinstance(dele, int):
            file_lines.append(f"- {path} (+{add}/-{dele})")
        else:
            file_lines.append(f"- {path}")
    if len(files) > 200:
        file_lines.append(f"... ({len(files) - 200} more)")

    lines = [
        "# GitHub PR Context",
        f"Repo: {repo}",
        f"PR: #{number} {title}".strip(),
        f"URL: {url}",
        f"State: {state}",
        f"Author: {author}",
        f"Labels: {labels}",
        f"Stats: +{additions} -{deletions}; changed files: {changed_files}",
        "",
        "Body:",
        body or "(no body)",
        "",
        "Files:",
    ]
    lines.extend(file_lines or ["(no files)"])
    review_lines = (
        format_review_threads(review_threads)
        if isinstance(review_threads, list)
        else []
    )
    if review_lines:
        lines.extend(["", "Review Threads:"])
        lines.extend(review_lines)
    return lines


def repo_slug_dirname(slug: str) -> str:
    normalized = (slug or "").strip().lower()
    safe_base = re.sub(r"[^a-z0-9._-]+", "-", normalized.replace("/", "--")).strip(".-")
    if not safe_base:
        safe_base = "unknown-repo"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{safe_base[:80]}-{digest}"


def build_context_file_from_url(
    service: Any,
    url: str,
    *,
    allow_cross_repo: bool = False,
) -> Optional[dict]:
    from .service import parse_github_url

    parsed = parse_github_url(url)
    if not parsed:
        return None
    if not service.gh_available():
        return None
    if not service.gh_authenticated():
        return None
    slug, kind, number = parsed
    repo_slug = slug
    if not allow_cross_repo:
        repo = service.repo_info()
        if slug.lower() != repo.name_with_owner.lower():
            return None
        repo_slug = repo.name_with_owner

    if kind == "issue":
        issue_obj = service.issue_view(
            number=number,
            repo_slug=repo_slug if allow_cross_repo else None,
        )
        lines = format_issue_context(issue_obj, repo=repo_slug)
    else:
        pr_obj = service.pr_view(
            number=number,
            repo_slug=repo_slug if allow_cross_repo else None,
        )
        owner, repo_name = repo_slug.split("/", 1)
        review_threads = service.pr_review_threads(
            owner=owner, repo=repo_name, number=number
        )
        lines = format_pr_context(pr_obj, repo=repo_slug, review_threads=review_threads)

    rel_dir = Path(".codex-autorunner") / "github_context"
    if allow_cross_repo:
        rel_dir = rel_dir / repo_slug_dirname(repo_slug)
    abs_dir = service.repo_root / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{kind}-{int(number)}.md"
    rel_path = rel_dir / filename
    abs_path = service.repo_root / rel_path
    atomic_write(abs_path, "\n".join(lines).rstrip() + "\n")

    hint = wrap_injected_context(
        "Context: see "
        f"{rel_path.as_posix()} "
        "(gh available: true; use gh CLI for updates if asked)."
    )
    return {"path": rel_path.as_posix(), "hint": hint, "kind": kind}
