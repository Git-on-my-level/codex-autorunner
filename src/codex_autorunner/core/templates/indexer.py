from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterator, Optional

from ..config import HubConfig, TemplateRepoConfig
from ..git_utils import run_git
from .git_mirror import ensure_git_mirror


@dataclasses.dataclass(frozen=True)
class TemplateInfo:
    """Information about a discovered template."""

    repo_id: str
    path: str
    name: str
    summary: str
    ref: str


def _parse_template_summary(content: str) -> str:
    """Extract a summary from template content.

    Prefers the title from frontmatter, then the first heading, then first line.
    """
    lines = content.split("\n")

    in_frontmatter = False
    frontmatter_lines: list[str] = []
    body_lines: list[str] = []
    in_body = False

    for line in lines:
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            else:
                in_frontmatter = False
                in_body = True
                continue

        if in_frontmatter:
            frontmatter_lines.append(line)
        elif in_body:
            body_lines.append(line)

    for fm_line in frontmatter_lines:
        if fm_line.strip().startswith("title:"):
            title = fm_line.split("title:", 1)[1].strip().strip('"').strip("'")
            if title:
                return title

    for line in body_lines:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        elif line:
            return line[:100]

    return ""


def _list_template_files(git_dir: Path, ref: str = "HEAD") -> Iterator[Path]:
    """List all markdown files in the template repo."""
    try:
        result = run_git(
            ["ls-tree", "-r", "--name-only", ref],
            cwd=git_dir,
        )
        if result.returncode != 0:
            return

        for path_str in result.stdout.split("\n"):
            path_str = path_str.strip()
            if path_str and path_str.endswith(".md"):
                yield Path(path_str)
    except Exception:
        return


def _get_file_content(git_dir: Path, path: Path, ref: str = "HEAD") -> Optional[str]:
    """Get the content of a file at a specific ref."""
    try:
        result = run_git(
            ["show", f"{ref}:{path}"],
            cwd=git_dir,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def index_templates(
    hub_config: HubConfig,
    hub_root: Path,
) -> list[TemplateInfo]:
    """Index all available templates from configured template repos.

    Returns a list of TemplateInfo objects for each discovered template.
    """
    templates: list[TemplateInfo] = []

    if not hub_config.templates.enabled:
        return templates

    for repo in hub_config.templates.repos:
        repo_templates = _index_single_repo(repo, hub_root)
        templates.extend(repo_templates)

    return templates


def _index_single_repo(repo: TemplateRepoConfig, hub_root: Path) -> list[TemplateInfo]:
    """Index templates from a single repository."""
    templates: list[TemplateInfo] = []

    try:
        git_dir = ensure_git_mirror(repo, hub_root)
    except Exception:
        return templates

    if not git_dir.exists():
        return templates

    ref = repo.default_ref or "HEAD"

    for template_path in _list_template_files(git_dir, ref):
        content = _get_file_content(git_dir, template_path, ref)
        if content is None:
            continue

        if template_path.name in ("README.md", "CONTRIBUTING.md", "LICENSE.md"):
            continue

        summary = _parse_template_summary(content)

        info = TemplateInfo(
            repo_id=repo.id,
            path=str(template_path),
            name=template_path.stem,
            summary=summary,
            ref=ref,
        )
        templates.append(info)

    return templates


def get_template_by_ref(
    hub_config: HubConfig,
    hub_root: Path,
    template_ref: str,
) -> Optional[TemplateInfo]:
    """Get a specific template by its reference (e.g., 'blessed:path/to/template.md').

    Returns None if not found.
    """
    if ":" not in template_ref:
        return None

    repo_id, path = template_ref.split(":", 1)

    if "@" in path:
        path, ref = path.rsplit("@", 1)
    else:
        ref = "HEAD"

    for repo in hub_config.templates.repos:
        if repo.id != repo_id:
            continue

        try:
            git_dir = ensure_git_mirror(repo, hub_root)
        except Exception:
            return None

        if not git_dir.exists():
            return None

        content = _get_file_content(git_dir, Path(path), ref)
        if content is None:
            return None

        summary = _parse_template_summary(content)

        return TemplateInfo(
            repo_id=repo_id,
            path=path,
            name=Path(path).stem,
            summary=summary,
            ref=ref,
        )

    return None


def search_templates(
    hub_config: HubConfig,
    hub_root: Path,
    query: str,
) -> list[TemplateInfo]:
    """Search templates by query (substring match on path, name, or summary)."""
    query_lower = query.lower()
    all_templates = index_templates(hub_config, hub_root)

    results = []
    for template in all_templates:
        if (
            query_lower in template.path.lower()
            or query_lower in template.name.lower()
            or query_lower in template.summary.lower()
        ):
            results.append(template)

    return results
