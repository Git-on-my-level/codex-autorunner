from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from ..core.utils import atomic_write
from .files import list_ticket_paths
from .frontmatter import split_markdown_frontmatter
from .lint import lint_ticket_frontmatter


def _coerce_done(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "yes", "y", "1"}:
            return True
        if cleaned in {"false", "no", "n", "0"}:
            return False
    return value


def _depends_on_note(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            return ", ".join(cleaned)
    rendered = str(value).strip()
    return rendered or None


def _split_with_recovery(raw: str) -> tuple[str | None, str]:
    fm_yaml, body = split_markdown_frontmatter(raw)
    if fm_yaml is not None:
        return fm_yaml, body
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, raw

    # Recover unclosed frontmatter by finding the longest valid YAML mapping prefix.
    best: int | None = None
    for i in range(2, len(lines) + 1):
        candidate = "\n".join(lines[1:i])
        try:
            loaded = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if isinstance(loaded, dict):
            best = i
    if best is None:
        return None, raw
    recovered_fm = "\n".join(lines[1:best])
    recovered_body = "\n".join(lines[best:])
    if recovered_body and not recovered_body.startswith("\n"):
        recovered_body = f"\n{recovered_body}"
    return recovered_fm, recovered_body


def _render_ticket(data: dict[str, Any], body: str) -> str:
    fm_yaml = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---{body}"


@dataclasses.dataclass
class TicketDoctorReport:
    checked: int = 0
    changed: int = 0
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    changed_files: list[str] = dataclasses.field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "changed": self.changed,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "changed_files": list(self.changed_files),
        }


def format_or_doctor_tickets(
    ticket_dir: Path,
    *,
    write: bool,
    fill_defaults: bool,
    default_agent: str,
) -> TicketDoctorReport:
    report = TicketDoctorReport()
    for path in list_ticket_paths(ticket_dir):
        report.checked += 1
        rel = str(path.relative_to(ticket_dir.parent))
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.errors.append(f"{rel}: failed to read file ({exc})")
            continue

        fm_yaml, body = _split_with_recovery(raw)
        if fm_yaml is None:
            report.errors.append(
                f"{rel}: missing YAML frontmatter delimiters (expected leading/trailing '---')."
            )
            continue
        try:
            loaded = yaml.safe_load(fm_yaml)
        except yaml.YAMLError as exc:
            report.errors.append(f"{rel}: invalid frontmatter YAML ({exc}).")
            continue
        if not isinstance(loaded, dict):
            report.errors.append(
                f"{rel}: frontmatter must be a YAML mapping (key/value object)."
            )
            continue

        changed = False
        data = dict(loaded)

        if "depends_on" in data:
            depends_note = _depends_on_note(data.get("depends_on"))
            data.pop("depends_on", None)
            changed = True
            report.warnings.append(
                f"{rel}: removed unsupported frontmatter.depends_on."
            )
            if depends_note:
                note = f"<!-- CAR ticket note: depends_on={depends_note} -->\n"
                if body.startswith("\n"):
                    body = f"{body[:1]}{note}{body[1:]}"
                else:
                    body = f"\n\n{note}{body}"

        if "agent" in data and isinstance(data["agent"], str):
            trimmed = data["agent"].strip()
            if trimmed and trimmed != data["agent"]:
                data["agent"] = trimmed
                changed = True
        if fill_defaults and "agent" not in data:
            data["agent"] = default_agent
            changed = True
            report.warnings.append(
                f"{rel}: inserted missing frontmatter.agent={default_agent!r}."
            )

        coerced_done = _coerce_done(data.get("done"))
        if coerced_done is not data.get("done"):
            data["done"] = coerced_done
            changed = True
        if fill_defaults and "done" not in data:
            data["done"] = False
            changed = True
            report.warnings.append(f"{rel}: inserted missing frontmatter.done=false.")

        _fm, errors = lint_ticket_frontmatter(data)
        if errors:
            report.errors.append(f"{rel}: " + "; ".join(errors))
            continue

        rendered = _render_ticket(data, body)
        if rendered != raw:
            changed = True
        if changed:
            report.changed += 1
            report.changed_files.append(rel)
            if write:
                atomic_write(path, rendered)

    return report
