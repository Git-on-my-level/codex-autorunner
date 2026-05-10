from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ...tickets.files import list_ticket_paths, read_ticket
from ...tickets.frontmatter import (
    parse_markdown_frontmatter,
    render_markdown_frontmatter,
)
from ...tickets.lint import parse_ticket_index
from ..domain.refs import ScopeRef, TicketRef
from ..ports.scope_resolver import ScopeResolver
from ..ports.ticket_store import TicketRecord, TicketStatus

_DONE_STATUS: dict[bool, TicketStatus] = {
    True: TicketStatus.DONE,
    False: TicketStatus.PENDING,
}


class FilesystemTicketStore:
    def __init__(self, scope_resolver: ScopeResolver) -> None:
        self._resolver = scope_resolver

    def _ticket_dir(self, scope: ScopeRef) -> Optional[Path]:
        resolved = self._resolver.resolve(scope)
        if resolved.workspace_root is None:
            return None
        return Path(resolved.workspace_root) / ".codex-autorunner" / "tickets"

    async def create(self, record: TicketRecord) -> TicketRecord:
        ticket_dir = self._ticket_dir(record.ref.scope)
        if ticket_dir is None:
            raise ValueError(
                f"Cannot create ticket for scope without workspace_root: "
                f"{record.ref.scope}"
            )
        ticket_dir.mkdir(parents=True, exist_ok=True)

        existing = list_ticket_paths(ticket_dir)
        next_idx = 1
        if existing:
            indices = [
                idx for p in existing if (idx := parse_ticket_index(p.name)) is not None
            ]
            if indices:
                next_idx = max(indices) + 1

        filename = f"TICKET-{next_idx:03d}.md"
        filepath = ticket_dir / filename

        frontmatter = {
            "ticket_id": record.ref.ticket_id,
            "title": record.title,
            "agent": record.agent or "user",
            "done": record.status == TicketStatus.DONE,
        }

        body = record.description or ""
        content = render_markdown_frontmatter(frontmatter, body)
        filepath.write_text(content, encoding="utf-8")

        return record

    async def get(self, ref: TicketRef) -> Optional[TicketRecord]:
        ticket_dir = self._ticket_dir(ref.scope)
        if ticket_dir is None or not ticket_dir.exists():
            return None

        for path in list_ticket_paths(ticket_dir):
            doc, _errors = read_ticket(path)
            if doc is not None and doc.frontmatter.ticket_id == ref.ticket_id:
                return TicketRecord(
                    ref=ref,
                    title=doc.frontmatter.title or "",
                    status=_DONE_STATUS.get(doc.frontmatter.done, TicketStatus.PENDING),
                    agent=doc.frontmatter.agent,
                    description=doc.body.strip(),
                )
        return None

    async def list_by_scope(self, scope: ScopeRef) -> List[TicketRecord]:
        ticket_dir = self._ticket_dir(scope)
        if ticket_dir is None or not ticket_dir.exists():
            return []

        records: list[TicketRecord] = []
        for path in list_ticket_paths(ticket_dir):
            doc, _errors = read_ticket(path)
            if doc is None:
                continue
            tref = TicketRef(scope=scope, ticket_id=doc.frontmatter.ticket_id)
            records.append(
                TicketRecord(
                    ref=tref,
                    title=doc.frontmatter.title or "",
                    status=_DONE_STATUS.get(doc.frontmatter.done, TicketStatus.PENDING),
                    agent=doc.frontmatter.agent,
                    description=doc.body.strip(),
                )
            )
        return records

    async def update_status(
        self, ref: TicketRef, status: TicketStatus
    ) -> Optional[TicketRecord]:
        ticket_dir = self._ticket_dir(ref.scope)
        if ticket_dir is None or not ticket_dir.exists():
            return None

        for path in list_ticket_paths(ticket_dir):
            doc, _errors = read_ticket(path)
            if doc is None or doc.frontmatter.ticket_id != ref.ticket_id:
                continue

            raw = path.read_text(encoding="utf-8")
            data, body = parse_markdown_frontmatter(raw)
            data["done"] = status == TicketStatus.DONE
            new_content = render_markdown_frontmatter(data, body)
            path.write_text(new_content, encoding="utf-8")

            return TicketRecord(
                ref=ref,
                title=doc.frontmatter.title or "",
                status=status,
                agent=doc.frontmatter.agent,
                description=doc.body.strip(),
            )
        return None

    async def delete(self, ref: TicketRef) -> bool:
        ticket_dir = self._ticket_dir(ref.scope)
        if ticket_dir is None or not ticket_dir.exists():
            return False

        for path in list_ticket_paths(ticket_dir):
            doc, _errors = read_ticket(path)
            if doc is None or doc.frontmatter.ticket_id != ref.ticket_id:
                continue
            path.unlink()
            return True
        return False


__all__ = ["FilesystemTicketStore"]
