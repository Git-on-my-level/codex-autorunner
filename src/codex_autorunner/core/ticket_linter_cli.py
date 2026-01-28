from __future__ import annotations

from pathlib import Path
from textwrap import dedent

LINTER_BASENAME = "lint_tickets.py"
LINTER_REL_PATH = Path(".codex-autorunner/bin") / LINTER_BASENAME

# Self-contained portable linter (PyYAML optional but preferred).
_SCRIPT = dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"Portable ticket frontmatter linter (no project venv required).

    - Validates ticket filenames (TICKET-<number>[suffix].md, e.g. TICKET-001-foo.md)
    - Parses YAML frontmatter for each .codex-autorunner/tickets/TICKET-*.md
    - Validates required keys: agent (string) and done (bool)
    - Exits non-zero on any error
    \"\"\"

    from __future__ import annotations

    import re
    import sys
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


    def _ticket_paths(tickets_dir: Path) -> Tuple[List[Path], List[str]]:
        \"\"\"Return sorted ticket paths along with filename lint errors.\"\"\"

        tickets: List[tuple[int, Path]] = []
        errors: List[str] = []
        for path in sorted(tickets_dir.iterdir()):
            if not path.is_file():
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
        tickets.sort(key=lambda pair: pair[0])
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


    def _lint_frontmatter(data: dict[str, Any]) -> List[str]:
        errors: List[str] = []

        agent = data.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            errors.append("frontmatter.agent is required and must be a non-empty string.")

        done = data.get("done")
        if not isinstance(done, bool):
            errors.append("frontmatter.done is required and must be a boolean.")

        return errors


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


    def main() -> int:
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

        for path in ticket_paths:
            errors.extend(lint_ticket(path))

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

        sys.stdout.write(f\"OK: {len(ticket_paths)} ticket(s) linted.\\n\")
        return 0


    if __name__ == \"__main__\":  # pragma: no cover
        sys.exit(main())
    """
)


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
