from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .....tickets.files import list_ticket_paths, read_ticket, safe_relpath
from .....tickets.frontmatter import (
    deterministic_ticket_id,
    parse_markdown_frontmatter,
    sanitize_ticket_id,
)
from .....tickets.lint import parse_ticket_index

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext


def _ticket_status(frontmatter: dict[str, object] | None, errors: list[str]) -> str:
    if errors:
        return "failed"
    if bool((frontmatter or {}).get("done")):
        return "done"
    return "idle"


def _ticket_number_sort_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _ticket_payload(
    *,
    hub_root: Path,
    workspace_root: Path,
    ticket_dir: Path,
    workspace_kind: str,
    workspace_id: str,
    repo_id: Optional[str],
    worktree_id: Optional[str],
    path: Path,
) -> dict[str, object]:
    resolved_path = path.resolve()
    try:
        if not resolved_path.is_relative_to(ticket_dir.resolve()):
            return {}
    except ValueError:
        return {}
    doc, errors = read_ticket(path)
    idx = getattr(doc, "index", None) or parse_ticket_index(path.name)
    parsed_frontmatter: dict[str, object] = {}
    parsed_body: str | None = None
    if doc is None:
        try:
            raw_body = path.read_text(encoding="utf-8")
            parsed_frontmatter, parsed_body = parse_markdown_frontmatter(raw_body)
        except (OSError, ValueError):
            parsed_frontmatter, parsed_body = {}, None

    frontmatter = asdict(doc.frontmatter) if doc else parsed_frontmatter
    source_ticket_id = sanitize_ticket_id(
        frontmatter.get("ticket_id")
    ) or deterministic_ticket_id(path)
    ticket_path = safe_relpath(path, workspace_root)
    global_ticket_id = f"{workspace_kind}:{workspace_id}:{source_ticket_id}"
    try:
        workspace_path = str(workspace_root.relative_to(hub_root))
    except ValueError:
        workspace_path = str(workspace_root)

    return {
        "id": global_ticket_id,
        "ticket_id": source_ticket_id,
        "source_ticket_id": source_ticket_id,
        "path": ticket_path,
        "ticket_path": ticket_path,
        "index": idx,
        "ticket_number": idx,
        "chat_key": f"ticket:{idx}:{source_ticket_id}" if idx else None,
        "frontmatter": frontmatter,
        "body": doc.body if doc else parsed_body,
        "errors": errors,
        "status": _ticket_status(frontmatter, errors),
        "workspace_kind": workspace_kind,
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "repo_id": repo_id,
        "worktree_id": worktree_id,
        "base_repo_id": repo_id if workspace_kind == "worktree" else None,
    }


def build_hub_ticket_router(context: HubAppContext) -> APIRouter:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/hub/tickets")
    async def list_hub_tickets(
        repo: Optional[str] = None,
        worktree: Optional[str] = None,
        status: Optional[str] = None,
    ):
        requested_status = (status or "").strip().lower() or None
        snapshots = context.supervisor.list_repos(use_cache=True)
        tickets: list[dict[str, object]] = []
        for snapshot in snapshots:
            workspace_kind = "worktree" if snapshot.kind == "worktree" else "repo"
            workspace_root = snapshot.path.resolve()
            ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
            if not ticket_dir.exists():
                continue
            repo_id = (
                snapshot.worktree_of if workspace_kind == "worktree" else snapshot.id
            )
            worktree_id = snapshot.id if workspace_kind == "worktree" else None
            if repo and repo_id != repo:
                continue
            if worktree and worktree_id != worktree:
                continue
            for path in list_ticket_paths(ticket_dir):
                payload = _ticket_payload(
                    hub_root=context.config.root,
                    workspace_root=workspace_root,
                    ticket_dir=ticket_dir,
                    workspace_kind=workspace_kind,
                    workspace_id=snapshot.id,
                    repo_id=repo_id,
                    worktree_id=worktree_id,
                    path=path,
                )
                if not payload:
                    continue
                if requested_status and str(payload.get("status")) != requested_status:
                    continue
                tickets.append(payload)

        tickets.sort(
            key=lambda item: (
                str(item.get("workspace_kind") or ""),
                str(item.get("workspace_id") or ""),
                _ticket_number_sort_value(item.get("ticket_number")),
                str(item.get("ticket_path") or ""),
            )
        )
        return {"tickets": tickets}

    return router
