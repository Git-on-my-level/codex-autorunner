from __future__ import annotations

import dataclasses
import heapq
import re
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
    strip_depends_on: bool
    reconcile_depends_on: str
    depends_on_summary: dict[str, Any]
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
            "strip_depends_on": self.strip_depends_on,
            "reconcile_depends_on": self.reconcile_depends_on,
            "depends_on_summary": self.depends_on_summary,
            "created": self.created,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "lint_errors": list(self.lint_errors),
            "items": [item.to_dict() for item in self.items],
        }


def _depends_on_note(value: Any) -> Optional[str]:
    """Render a stable, low-noise note for depends_on values from external ticket packs."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        if parts:
            return ", ".join(parts)
    rendered = str(value).strip()
    return rendered or None


_TICKET_REF_RE = re.compile(r"^TICKET-(\d{1,})$", re.IGNORECASE)


def _parse_depends_on_refs(value: Any) -> tuple[list[int], list[str]]:
    refs: list[int] = []
    unsupported: list[str] = []
    raw_values: list[Any]
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = [value]

    for item in raw_values:
        if isinstance(item, int):
            if item >= 1:
                refs.append(item)
            else:
                unsupported.append(str(item))
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            if text.isdigit():
                refs.append(int(text))
                continue
            m = _TICKET_REF_RE.match(text)
            if m:
                refs.append(int(m.group(1)))
                continue
            unsupported.append(text)
            continue
        unsupported.append(str(item))
    return refs, unsupported


def _build_depends_summary(
    entries: list[tuple[str, int, str]],
) -> tuple[dict[str, Any], Optional[list[int]]]:
    summary: dict[str, Any] = {
        "tickets_with_depends_on": 0,
        "dependency_edges": 0,
        "ordering_conflicts": [],
        "ambiguous_reasons": [],
        "proposed_order": [],
        "has_depends_on": False,
        "reconcilable": False,
    }
    if not entries:
        return summary, None

    # Parse dependency references from raw frontmatter before we strip keys.
    depends_map: dict[int, list[int]] = {}
    index_to_positions: dict[int, list[int]] = {}
    for pos, (_source_name, original_index, raw) in enumerate(entries):
        index_to_positions.setdefault(original_index, []).append(pos)
        data, _body = parse_markdown_frontmatter(raw)
        if not isinstance(data, dict) or "depends_on" not in data:
            continue
        summary["tickets_with_depends_on"] = int(summary["tickets_with_depends_on"]) + 1
        summary["has_depends_on"] = True
        refs, unsupported = _parse_depends_on_refs(data.get("depends_on"))
        if unsupported:
            for item in unsupported:
                summary["ambiguous_reasons"].append(
                    f"TICKET-{original_index:03d}: unsupported depends_on reference '{item}'."
                )
        if refs:
            depends_map[pos] = refs

    if not depends_map:
        return summary, None

    for idx, positions in index_to_positions.items():
        if len(positions) > 1:
            summary["ambiguous_reasons"].append(
                f"Duplicate ticket index TICKET-{idx:03d} in import pack; depends_on references are ambiguous."
            )

    adjacency: dict[int, set[int]] = {pos: set() for pos in range(len(entries))}
    indegree: dict[int, int] = {pos: 0 for pos in range(len(entries))}
    edges: list[tuple[int, int]] = []
    for pos, refs in depends_map.items():
        source_idx = entries[pos][1]
        for dep_idx in refs:
            targets = index_to_positions.get(dep_idx)
            if not targets:
                summary["ambiguous_reasons"].append(
                    f"TICKET-{source_idx:03d} depends_on TICKET-{dep_idx:03d}, but that ticket is not in the pack."
                )
                continue
            if len(targets) != 1:
                summary["ambiguous_reasons"].append(
                    f"TICKET-{source_idx:03d} depends_on TICKET-{dep_idx:03d}, but multiple matching tickets exist."
                )
                continue
            dep_pos = targets[0]
            if dep_pos == pos:
                summary["ambiguous_reasons"].append(
                    f"TICKET-{source_idx:03d} depends_on itself."
                )
                continue
            if pos not in adjacency[dep_pos]:
                adjacency[dep_pos].add(pos)
                indegree[pos] += 1
                edges.append((dep_pos, pos))

    summary["dependency_edges"] = len(edges)

    current_order = list(range(len(entries)))
    current_rank = {pos: rank for rank, pos in enumerate(current_order)}
    conflicts: list[str] = []
    for dep_pos, target_pos in edges:
        if current_rank[dep_pos] > current_rank[target_pos]:
            dep_idx = entries[dep_pos][1]
            target_idx = entries[target_pos][1]
            conflicts.append(
                f"TICKET-{target_idx:03d} depends_on TICKET-{dep_idx:03d}, but appears earlier by filename index."
            )
    summary["ordering_conflicts"] = conflicts

    # Deterministic topo sort: tie-break by current pack order.
    heap: list[tuple[int, int]] = []
    for pos in range(len(entries)):
        if indegree[pos] == 0:
            heapq.heappush(heap, (current_rank[pos], pos))
    proposed: list[int] = []
    while heap:
        _, pos = heapq.heappop(heap)
        proposed.append(pos)
        for nxt in sorted(adjacency[pos], key=lambda p: current_rank[p]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, (current_rank[nxt], nxt))

    if len(proposed) != len(entries):
        summary["ambiguous_reasons"].append(
            "Dependency graph contains a cycle; cannot derive deterministic ticket order."
        )
        return summary, None

    summary["proposed_order"] = [entries[pos][0] for pos in proposed]
    summary["reconcilable"] = len(conflicts) > 0
    return summary, proposed


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
    strip_depends_on: bool = True,
    reconcile_depends_on: str = "warn",
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

    if reconcile_depends_on not in {"off", "warn", "auto"}:
        errors.append(
            "Invalid reconcile_depends_on mode; expected one of: off, warn, auto."
        )

    depends_on_summary, proposed_order = _build_depends_summary(entries)
    depends_on_summary["reconcile_mode"] = reconcile_depends_on

    ordered_entries = entries
    if (
        reconcile_depends_on == "auto"
        and proposed_order is not None
        and depends_on_summary.get("reconcilable")
    ):
        ordered_entries = [entries[pos] for pos in proposed_order]
        depends_on_summary["reconciled"] = True
    else:
        depends_on_summary["reconciled"] = False

    if renumber is not None:
        start = renumber.get("start", 1)
        step = renumber.get("step", 1)
        target_slots = [start + (step * idx) for idx in range(len(ordered_entries))]
    else:
        target_slots = [entry[1] for entry in entries]

    indices = target_slots

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
    for (source_name, original_index, raw), target_index in zip(
        ordered_entries, indices
    ):
        item = TicketImportItem(
            source=source_name,
            target=None,
            index=target_index,
            original_index=original_index,
            status="ready",
        )
        data, body = parse_markdown_frontmatter(raw)

        depends_note = None
        if strip_depends_on and isinstance(data, dict) and "depends_on" in data:
            depends_note = _depends_on_note(data.get("depends_on"))
            try:
                data = dict(data)
                data.pop("depends_on", None)
            except Exception:
                pass
            item.warnings.append(
                "Removed frontmatter.depends_on (CAR executes tickets in filename order)."
            )

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
        if depends_note:
            # Keep the original intent without reintroducing unsupported frontmatter.
            note = f"<!-- CAR ticket-pack note: depends_on={depends_note} -->\n"
            if body.startswith("\n"):
                # Insert after the first newline so the remaining body stays byte-for-byte.
                body = f"{body[:1]}{note}{body[1:]}"
            else:
                body = f"\n\n{note}{body}"
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
        strip_depends_on=strip_depends_on,
        reconcile_depends_on=reconcile_depends_on,
        depends_on_summary=depends_on_summary,
        created=created,
        skipped=0,
        errors=errors,
        lint_errors=lint_errors,
        items=items,
    )
