from __future__ import annotations

import dataclasses
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import yaml

from ..core.utils import atomic_write
from .files import list_ticket_paths, read_ticket, safe_relpath
from .frontmatter import parse_markdown_frontmatter, split_markdown_frontmatter
from .lint import lint_ticket_directory, lint_ticket_frontmatter, parse_ticket_index


class TicketPackImportError(Exception):
    """Raised when a ticket pack import fails before writing."""


@dataclasses.dataclass
class TicketImportItem:
    source: str
    target: Optional[str]
    index: Optional[int]
    original_index: Optional[int]
    status: str
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "index": self.index,
            "original_index": self.original_index,
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclasses.dataclass
class TicketImportReport:
    repo_id: str
    repo_root: str
    ticket_dir: str
    zip_path: str
    dry_run: bool
    lint: bool
    renumber: Optional[dict[str, int]]
    assign_agent: Optional[str]
    clear_model_pin: bool
    apply_template: Optional[str]
    created: int
    skipped: int
    errors: list[str]
    lint_errors: list[str]
    items: list[TicketImportItem]

    def ok(self) -> bool:
        if self.errors or self.lint_errors:
            return False
        return all(item.status != "error" for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_root": self.repo_root,
            "ticket_dir": self.ticket_dir,
            "zip_path": self.zip_path,
            "dry_run": self.dry_run,
            "lint": self.lint,
            "renumber": self.renumber,
            "assign_agent": self.assign_agent,
            "clear_model_pin": self.clear_model_pin,
            "apply_template": self.apply_template,
            "created": self.created,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "lint_errors": list(self.lint_errors),
            "items": [item.to_dict() for item in self.items],
        }


def load_template_frontmatter(content: str) -> dict[str, Any]:
    fm_yaml, _body = split_markdown_frontmatter(content)
    if not fm_yaml:
        raise TicketPackImportError("Template is missing YAML frontmatter.")
    try:
        data = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        raise TicketPackImportError(
            f"Template frontmatter is invalid YAML: {exc}"
        ) from exc
    if not isinstance(data, dict) or not data:
        raise TicketPackImportError("Template frontmatter must be a YAML mapping.")
    return data


def _render_ticket(frontmatter: dict[str, Any], body: str) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---{body}"


def _collect_zip_tickets(zip_path: Path) -> list[tuple[str, int, str]]:
    entries: list[tuple[str, int, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            posix = PurePosixPath(info.filename)
            name = posix.name
            idx = parse_ticket_index(name)
            if idx is None:
                continue
            try:
                raw = zf.read(info).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TicketPackImportError(
                    f"Ticket {info.filename} is not valid UTF-8: {exc}"
                ) from exc
            entries.append((info.filename, idx, raw))
    entries.sort(key=lambda item: (item[1], item[0]))
    return entries


def _lint_ticket_dir(ticket_dir: Path) -> list[str]:
    errors: list[str] = []
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return errors

    ticket_root = ticket_dir.parent
    for path in sorted(ticket_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name == "AGENTS.md":
            continue
        if parse_ticket_index(path.name) is None:
            rel_path = safe_relpath(path, ticket_root)
            errors.append(
                f"{rel_path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
            )

    errors.extend(lint_ticket_directory(ticket_dir))

    ticket_paths = list_ticket_paths(ticket_dir)
    for path in ticket_paths:
        _, ticket_errors = read_ticket(path)
        for err in ticket_errors:
            errors.append(f"{path.relative_to(path.parent.parent)}: {err}")

    return errors


def import_ticket_pack(
    *,
    repo_id: str,
    repo_root: Path,
    ticket_dir: Path,
    zip_path: Path,
    renumber: Optional[dict[str, int]] = None,
    assign_agent: Optional[str] = None,
    clear_model_pin: bool = False,
    template_ref: Optional[str] = None,
    template_frontmatter: Optional[dict[str, Any]] = None,
    lint: bool = True,
    dry_run: bool = False,
) -> TicketImportReport:
    items: list[TicketImportItem] = []
    errors: list[str] = []
    lint_errors: list[str] = []

    if lint and ticket_dir.exists():
        lint_errors.extend(_lint_ticket_dir(ticket_dir))

    try:
        entries = _collect_zip_tickets(zip_path)
    except (OSError, zipfile.BadZipFile, TicketPackImportError) as exc:
        errors.append(str(exc))
        entries = []

    if not entries:
        errors.append("No ticket files found in zip (expected TICKET-###.md).")

    if renumber is not None:
        start = renumber.get("start", 1)
        step = renumber.get("step", 1)
        indices = [start + (step * idx) for idx in range(len(entries))]
    else:
        indices = [entry[1] for entry in entries]

    if indices:
        if any(idx < 1 for idx in indices):
            errors.append("Renumbered ticket indices must be >= 1.")
        if len(set(indices)) != len(indices):
            errors.append("Renumbered ticket indices contain duplicates.")

    existing_indices: set[int] = set()
    if ticket_dir.exists() and ticket_dir.is_dir():
        for path in ticket_dir.iterdir():
            if not path.is_file():
                continue
            idx = parse_ticket_index(path.name)
            if idx is not None:
                existing_indices.add(idx)

    if indices:
        conflicts = sorted(set(indices).intersection(existing_indices))
        if conflicts:
            conflict_list = ", ".join([f"{idx:03d}" for idx in conflicts])
            errors.append(
                "Ticket indices already exist in destination: " f"{conflict_list}."
            )

    width = 3
    if existing_indices or indices:
        width = max(3, max(len(str(i)) for i in list(existing_indices) + indices))

    pending_writes: list[tuple[Path, str]] = []
    for (source_name, original_index, raw), target_index in zip(entries, indices):
        item = TicketImportItem(
            source=source_name,
            target=None,
            index=target_index,
            original_index=original_index,
            status="ready",
        )
        data, body = parse_markdown_frontmatter(raw)
        _frontmatter, fm_errors = lint_ticket_frontmatter(data)
        if fm_errors:
            item.status = "error"
            item.errors.extend(fm_errors)
            items.append(item)
            continue

        merged: dict[str, Any] = {}
        if template_frontmatter:
            merged.update(template_frontmatter)
        if isinstance(data, dict):
            merged.update(data)

        if assign_agent:
            merged["agent"] = assign_agent
        if clear_model_pin:
            merged.pop("model", None)
            merged.pop("reasoning", None)

        _frontmatter2, fm_errors2 = lint_ticket_frontmatter(merged)
        if fm_errors2:
            item.status = "error"
            item.errors.extend(fm_errors2)
            items.append(item)
            continue

        assert _frontmatter2 is not None
        rendered = _render_ticket(merged, body)
        filename = f"TICKET-{target_index:0{width}d}.md"
        target_path = ticket_dir / filename
        item.target = safe_relpath(target_path, repo_root)
        item.status = "ready"
        items.append(item)

        pending_writes.append((target_path, rendered))

    created = 0
    if (
        not errors
        and not lint_errors
        and not any(item.status == "error" for item in items)
    ):
        created = len(pending_writes)
        if not dry_run:
            ticket_dir.mkdir(parents=True, exist_ok=True)
            for target_path, rendered in pending_writes:
                atomic_write(target_path, rendered)

    return TicketImportReport(
        repo_id=repo_id,
        repo_root=str(repo_root),
        ticket_dir=str(ticket_dir),
        zip_path=str(zip_path),
        dry_run=dry_run,
        lint=lint,
        renumber=renumber,
        assign_agent=assign_agent,
        clear_model_pin=clear_model_pin,
        apply_template=template_ref,
        created=created,
        skipped=0,
        errors=errors,
        lint_errors=lint_errors,
        items=items,
    )
