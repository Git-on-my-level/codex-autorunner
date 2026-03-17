from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from ..ticket_helper_script_common import portable_ticket_validation_source

LINTER_BASENAME = "lint_tickets.py"
LINTER_REL_PATH = Path(".codex-autorunner/bin") / LINTER_BASENAME
_COMMON_VALIDATION = portable_ticket_validation_source()

# Self-contained portable linter (PyYAML optional but preferred).
_SCRIPT = dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"Portable ticket frontmatter linter (no project venv required).

    - Validates ticket filenames (TICKET-<number>[suffix].md, e.g. TICKET-001-foo.md)
    - Parses YAML frontmatter for each .codex-autorunner/tickets/TICKET-*.md
    - Validates required keys: ticket_id, agent, and done
    - Validates agent ids against the runtime agents that seeded this repo
    - Exits non-zero on any error
    \"\"\"

    from __future__ import annotations

    import argparse
    import re
    import sys
    import uuid
    from pathlib import Path
    from typing import Any, List, Optional, Tuple

    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "PyYAML is required to lint tickets. Install with:\\n"
            "  python3 -m pip install --user pyyaml\\n"
        )
        sys.exit(2)


    _TICKET_NAME_RE = re.compile(r"^TICKET-(\\d{3,})(?:[^/]*)\\.md$", re.IGNORECASE)
    __COMMON_VALIDATION__


    def _ticket_paths(tickets_dir: Path) -> Tuple[List[Path], List[str]]:
        \"\"\"Return sorted ticket paths along with filename lint errors.\"\"\"

        tickets: List[tuple[int, Path]] = []
        errors: List[str] = []
        index_to_paths: dict[int, List[Path]] = {}
        for path in sorted(tickets_dir.iterdir()):
            if not path.is_file():
                continue
            if path.name in _IGNORED_NON_TICKET_FILENAMES:
                continue
            match = _TICKET_NAME_RE.match(path.name)
            if not match:
                errors.append(
                    f\"{path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)\"
                )
                continue
            try:
                idx = int(match.group(1))
            except ValueError:
                errors.append(
                    f\"{path}: Invalid ticket filename; ticket number must be digits (e.g. 001)\"
                )
                continue
            tickets.append((idx, path))
            # Track paths by index to detect duplicates
            if idx not in index_to_paths:
                index_to_paths[idx] = []
            index_to_paths[idx].append(path)
        tickets.sort(key=lambda pair: pair[0])

        # Check for duplicate indices
        for idx, paths in index_to_paths.items():
            if len(paths) > 1:
                paths_str = ", ".join([str(p) for p in paths])
                errors.append(
                    f\"Duplicate ticket index {idx:03d}: multiple files share the same index ({paths_str}). \"
                    \"Rename or remove duplicates to ensure deterministic ordering.\"
                )

        return [p for _, p in tickets], errors


    def _split_frontmatter(text: str) -> Tuple[Optional[str], List[str]]:
        if not text:
            return None, ["Empty file; missing YAML frontmatter."]

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None, ["Missing YAML frontmatter (expected leading '---')."]

        end_idx: Optional[int] = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() in ("---", "..."):
                end_idx = idx
                break

        if end_idx is None:
            return None, ["Frontmatter is not closed (missing trailing '---')."]

        fm_yaml = "\\n".join(lines[1:end_idx])
        return fm_yaml, []


    def _parse_yaml(fm_yaml: Optional[str]) -> Tuple[dict[str, Any], List[str]]:
        if fm_yaml is None:
            return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]

        try:
            loaded = yaml.safe_load(fm_yaml)
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            return {}, [f"YAML parse error: {exc}"]

        if loaded is None:
            return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]

        if not isinstance(loaded, dict):
            return {}, ["Invalid YAML frontmatter (expected a mapping)."]

        return loaded, []

    def lint_ticket(path: Path) -> List[str]:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return [f"{path}: Unable to read file ({exc})."]

        fm_yaml, fm_errors = _split_frontmatter(raw)
        if fm_errors:
            return [f"{path}: {msg}" for msg in fm_errors]

        data, parse_errors = _parse_yaml(fm_yaml)
        if parse_errors:
            return [f"{path}: {msg}" for msg in parse_errors]

        lint_errors = _lint_frontmatter(data)
        return [f"{path}: {msg}" for msg in lint_errors]


    def _read_ticket_id(path: Path) -> Optional[str]:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        fm_yaml, fm_errors = _split_frontmatter(raw)
        if fm_errors:
            return None
        data, parse_errors = _parse_yaml(fm_yaml)
        if parse_errors:
            return None
        return _sanitize_ticket_id(data.get("ticket_id"))


    def _render_ticket(data: dict[str, Any], body: str) -> str:
        fm_yaml = yaml.safe_dump(data, sort_keys=False).rstrip()
        return f"---\\n{fm_yaml}\\n---\\n{body}"


    def _fix_ticket_id(path: Path) -> Tuple[bool, List[str]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return False, [f"{path}: Unable to read file ({exc})."]

        fm_yaml, fm_errors = _split_frontmatter(raw)
        if fm_errors:
            return False, [f"{path}: {msg}" for msg in fm_errors]

        data, parse_errors = _parse_yaml(fm_yaml)
        if parse_errors:
            return False, [f"{path}: {msg}" for msg in parse_errors]

        ticket_id = data.get("ticket_id")
        if isinstance(ticket_id, str) and _TICKET_ID_RE.match(ticket_id.strip()):
            return False, []

        lines = raw.splitlines()
        end_idx = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() in ("---", "..."):
                end_idx = idx
                break
        if end_idx is None:
            return False, [f"{path}: Frontmatter is not closed (missing trailing '---')."]
        body = "\\n".join(lines[end_idx + 1 :])
        data = dict(data)
        data["ticket_id"] = f"tkt_{uuid.uuid4().hex}"
        rendered = _render_ticket(data, body)
        if rendered != raw:
            path.write_text(rendered, encoding="utf-8")
            return True, []
        return False, []


    def main(argv: Optional[List[str]] = None) -> int:
        parser = argparse.ArgumentParser(description="Lint CAR ticket frontmatter.")
        parser.add_argument(
            "--fix-ticket-ids",
            action="store_true",
            help="Backfill missing or invalid ticket_id values before linting.",
        )
        args = parser.parse_args(argv)

        script_dir = Path(__file__).resolve().parent
        tickets_dir = script_dir.parent / "tickets"

        if not tickets_dir.exists():
            sys.stderr.write(
                f"Tickets directory not found: {tickets_dir}\\n"
                "Run from a Codex Autorunner repo with .codex-autorunner/tickets present.\\n"
            )
            return 2

        errors: List[str] = []
        ticket_paths, name_errors = _ticket_paths(tickets_dir)
        errors.extend(name_errors)

        fixed = 0
        if args.fix_ticket_ids:
            for path in ticket_paths:
                changed, fix_errors = _fix_ticket_id(path)
                if changed:
                    fixed += 1
                errors.extend(fix_errors)

        for path in ticket_paths:
            errors.extend(lint_ticket(path))

        ticket_id_to_paths: dict[str, List[Path]] = {}
        for path in ticket_paths:
            ticket_id = _read_ticket_id(path)
            if not ticket_id:
                continue
            ticket_id_to_paths.setdefault(ticket_id, []).append(path)

        for ticket_id, paths in ticket_id_to_paths.items():
            if len(paths) > 1:
                paths_str = ", ".join(str(path) for path in paths)
                errors.append(
                    f"Duplicate ticket_id {ticket_id!r}: multiple files share the same logical ticket identity ({paths_str}). "
                    "Backfill or rewrite one of the ticket_ids so ticket-owned state remains unambiguous."
                )

        if not ticket_paths:
            if errors:
                for msg in errors:
                    sys.stderr.write(msg + "\\n")
                return 1
            sys.stderr.write(f"No tickets found in {tickets_dir}\\n")
            return 1

        if errors:
            for msg in errors:
                sys.stderr.write(msg + "\\n")
            return 1

        if fixed:
            sys.stdout.write(f"Backfilled ticket_id in {fixed} ticket(s).\\n")
        sys.stdout.write(f\"OK: {len(ticket_paths)} ticket(s) linted.\\n\")
        return 0


    if __name__ == \"__main__\":  # pragma: no cover
        sys.exit(main())
    """
).replace("__COMMON_VALIDATION__", _COMMON_VALIDATION)


def ensure_ticket_linter(repo_root: Path, *, force: bool = False) -> Path:
    """
    Ensure a portable ticket frontmatter linter exists under .codex-autorunner/bin.
    The file is always considered generated; it may be refreshed when the content changes.
    """

    linter_path = repo_root / LINTER_REL_PATH
    linter_path.parent.mkdir(parents=True, exist_ok=True)

    existing = None
    if linter_path.exists():
        try:
            existing = linter_path.read_text(encoding="utf-8")
        except OSError:
            existing = None
    if not force and existing == _SCRIPT:
        return linter_path

    linter_path.write_text(_SCRIPT, encoding="utf-8")
    # Ensure executable bit for user.
    mode = linter_path.stat().st_mode
    linter_path.chmod(mode | 0o111)
    return linter_path
