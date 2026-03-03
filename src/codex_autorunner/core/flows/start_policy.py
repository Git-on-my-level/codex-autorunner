from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...tickets.files import list_ticket_paths, read_ticket
from ...tickets.ingest_state import INGEST_STATE_FILENAME
from ...tickets.lint import lint_ticket_directory, parse_ticket_index

NO_TICKETS_START_ERROR = (
    "No tickets found under .codex-autorunner/tickets. "
    "Use /api/flows/ticket_flow/bootstrap to seed tickets."
)


@dataclass(frozen=True)
class TicketStartPolicy:
    ticket_paths: tuple[Path, ...]
    invalid_filenames: tuple[str, ...]
    duplicate_indices: tuple[str, ...]
    frontmatter: tuple[str, ...]

    @property
    def has_tickets(self) -> bool:
        return bool(self.ticket_paths)

    @property
    def lint_errors(self) -> tuple[str, ...]:
        return (
            *self.invalid_filenames,
            *self.duplicate_indices,
            *self.frontmatter,
        )


def evaluate_ticket_start_policy(ticket_dir: Path) -> TicketStartPolicy:
    if not ticket_dir.exists():
        return TicketStartPolicy(
            ticket_paths=(),
            invalid_filenames=(),
            duplicate_indices=(),
            frontmatter=(),
        )

    if not ticket_dir.is_dir():
        return TicketStartPolicy(
            ticket_paths=(),
            invalid_filenames=(f"{ticket_dir}: Ticket path must be a directory.",),
            duplicate_indices=(),
            frontmatter=(),
        )

    ticket_root = ticket_dir.parent
    invalid_filenames: list[str] = []
    for path in sorted(ticket_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name in {"AGENTS.md", INGEST_STATE_FILENAME}:
            continue
        if parse_ticket_index(path.name) is None:
            try:
                rel_path = path.relative_to(ticket_root)
            except ValueError:
                rel_path = path
            invalid_filenames.append(
                f"{rel_path}: Invalid ticket filename; expected "
                "TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
            )

    duplicate_indices = list(lint_ticket_directory(ticket_dir))
    ticket_paths = tuple(list_ticket_paths(ticket_dir))

    frontmatter: list[str] = []
    for path in ticket_paths:
        _, ticket_errors = read_ticket(path)
        for err in ticket_errors:
            frontmatter.append(f"{path.relative_to(path.parent.parent)}: {err}")

    return TicketStartPolicy(
        ticket_paths=ticket_paths,
        invalid_filenames=tuple(invalid_filenames),
        duplicate_indices=tuple(duplicate_indices),
        frontmatter=tuple(frontmatter),
    )
