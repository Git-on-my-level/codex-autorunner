from __future__ import annotations

import dataclasses
import re
import zipfile
from pathlib import Path
from typing import Iterable

import yaml

from ..agents.registry import validate_agent_id
from ..core.utils import atomic_write
from .doctor import TicketDoctorReport, format_or_doctor_tickets
from .files import list_ticket_paths
from .frontmatter import split_markdown_frontmatter
from .lint import parse_ticket_index

_FALSE_GOAL_FIXUPS = (
    ("done: falsegoal:", "done: false\ngoal:"),
    ("done: truegoal:", "done: true\ngoal:"),
)
_ASSIGNMENT_RE = re.compile(r"^\s*([a-zA-Z0-9_-]+)\s*:\s*([0-9,\s]+)\s*$")


class TicketPackSetupError(ValueError):
    """Raised when ticket pack setup input is invalid."""


@dataclasses.dataclass(frozen=True)
class TicketPackSetupReport:
    extracted_files: list[str]
    assigned_files: list[str]
    fmt_report: TicketDoctorReport
    doctor_report: TicketDoctorReport
    assignment_map: dict[int, str]

    def to_dict(self) -> dict:
        return {
            "extracted_files": list(self.extracted_files),
            "assigned_files": list(self.assigned_files),
            "fmt_report": self.fmt_report.to_dict(),
            "doctor_report": self.doctor_report.to_dict(),
            "assignment_map": {f"{k:03d}": v for k, v in self.assignment_map.items()},
        }


def parse_assignment_specs(specs: Iterable[str]) -> dict[int, str]:
    assignment_map: dict[int, str] = {}
    for raw_spec in specs:
        spec = (raw_spec or "").strip()
        if not spec:
            continue
        match = _ASSIGNMENT_RE.match(spec)
        if not match:
            raise TicketPackSetupError(
                f"Invalid --assign value {raw_spec!r}. Expected '<agent>:<n,n,...>'."
            )
        agent = match.group(1).strip()
        if agent != "user":
            try:
                validate_agent_id(agent)
            except ValueError as exc:
                raise TicketPackSetupError(str(exc)) from exc
        tickets_raw = match.group(2)
        parts = [part.strip() for part in tickets_raw.split(",") if part.strip()]
        if not parts:
            raise TicketPackSetupError(
                f"Invalid --assign value {raw_spec!r}. Ticket list is empty."
            )
        for part in parts:
            if not part.isdigit():
                raise TicketPackSetupError(
                    f"Invalid ticket number {part!r} in --assign {raw_spec!r}."
                )
            assignment_map[int(part)] = agent
    return assignment_map


def _normalize_frontmatter_quirks(raw: str) -> str:
    normalized = raw
    for old, new in _FALSE_GOAL_FIXUPS:
        normalized = normalized.replace(old, new)
    return normalized


def _extract_ticket_pack(zip_path: Path, ticket_dir: Path) -> list[str]:
    extracted: list[str] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                src_name = info.filename.replace("\\", "/")
                base_name = Path(src_name).name
                if not base_name:
                    continue
                if not base_name.lower().endswith(".md"):
                    continue
                if parse_ticket_index(base_name) is None:
                    continue
                if base_name in seen:
                    raise TicketPackSetupError(
                        f"Duplicate file name in zip: {base_name}."
                    )
                seen.add(base_name)
                raw = zf.read(info).decode("utf-8", errors="replace")
                atomic_write(ticket_dir / base_name, _normalize_frontmatter_quirks(raw))
                extracted.append(base_name)
    except zipfile.BadZipFile as exc:
        raise TicketPackSetupError(f"Invalid zip file: {exc}") from exc
    except OSError as exc:
        raise TicketPackSetupError(f"Failed to read zip file: {exc}") from exc
    if not extracted:
        raise TicketPackSetupError(
            "Ticket pack zip did not contain any markdown files."
        )
    return sorted(extracted)


def _render_ticket(data: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---{body}"


def _apply_assignments(ticket_dir: Path, assignment_map: dict[int, str]) -> list[str]:
    if not assignment_map:
        return []
    updated: list[str] = []
    for path in list_ticket_paths(ticket_dir):
        idx = parse_ticket_index(path.name)
        if idx is None or idx not in assignment_map:
            continue
        raw = path.read_text(encoding="utf-8")
        fm_yaml, body = split_markdown_frontmatter(raw)
        if fm_yaml is None:
            raise TicketPackSetupError(
                f"{path.name}: missing YAML frontmatter; cannot apply assignment."
            )
        try:
            loaded = yaml.safe_load(fm_yaml)
        except yaml.YAMLError as exc:
            raise TicketPackSetupError(
                f"{path.name}: invalid YAML frontmatter ({exc}); cannot apply assignment."
            ) from exc
        if not isinstance(loaded, dict):
            raise TicketPackSetupError(
                f"{path.name}: frontmatter must be a YAML mapping."
            )
        data = dict(loaded)
        data["agent"] = assignment_map[idx]
        rendered = _render_ticket(data, body)
        if rendered != raw:
            atomic_write(path, rendered)
            updated.append(str(path.relative_to(ticket_dir.parent)))
    return updated


def setup_ticket_pack(
    *,
    target_path: Path,
    zip_path: Path,
    assignment_specs: Iterable[str] = (),
    default_agent: str = "codex",
) -> TicketPackSetupReport:
    if not target_path.exists():
        raise TicketPackSetupError(f"Target path does not exist: {target_path}")
    if not target_path.is_dir():
        raise TicketPackSetupError(f"Target path must be a directory: {target_path}")
    if not zip_path.exists():
        raise TicketPackSetupError(f"Zip path does not exist: {zip_path}")
    if zip_path.is_dir():
        raise TicketPackSetupError(f"Zip path must be a file: {zip_path}")

    assignment_map = parse_assignment_specs(assignment_specs)

    ticket_dir = target_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    extracted_files = _extract_ticket_pack(zip_path, ticket_dir)
    fmt_report = format_or_doctor_tickets(
        ticket_dir,
        write=True,
        fill_defaults=False,
        default_agent=default_agent,
    )
    if fmt_report.errors:
        raise TicketPackSetupError("Ticket fmt failed: " + "; ".join(fmt_report.errors))

    assigned_files = _apply_assignments(ticket_dir, assignment_map)

    doctor_report = format_or_doctor_tickets(
        ticket_dir,
        write=True,
        fill_defaults=True,
        default_agent=default_agent,
    )
    if doctor_report.errors:
        raise TicketPackSetupError(
            "Ticket doctor failed: " + "; ".join(doctor_report.errors)
        )

    return TicketPackSetupReport(
        extracted_files=extracted_files,
        assigned_files=assigned_files,
        fmt_report=fmt_report,
        doctor_report=doctor_report,
        assignment_map=assignment_map,
    )
