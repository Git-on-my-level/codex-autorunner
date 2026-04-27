from __future__ import annotations

from pathlib import Path
from textwrap import dedent

LINTER_BASENAME = "lint_tickets.py"
LINTER_REL_PATH = Path(".codex-autorunner/bin") / LINTER_BASENAME
LINT_IMPL_BASENAME = "_ticket_lint_impl.py"
LINT_IMPL_REL_PATH = Path(".codex-autorunner/bin") / LINT_IMPL_BASENAME


def _portable_ticket_lint_source() -> str:
    source_path = Path(__file__).resolve().parents[1] / "tickets" / "portable_lint.py"
    content = source_path.read_text(encoding="utf-8")
    if not content.endswith("\n"):
        content += "\n"
    return content


_SCRIPT = dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"Canonical portable ticket frontmatter linter.

    `ticket_tool.py lint` is a compatibility wrapper around this same shared
    implementation.
    \"\"\"

    from __future__ import annotations

    import argparse
    import importlib.util
    import sys
    from pathlib import Path
    from typing import List, Optional


    def _load_shared_linter():
        module_path = Path(__file__).resolve().with_name("_ticket_lint_impl.py")
        spec = importlib.util.spec_from_file_location("_ticket_lint_impl", module_path)
        if spec is None or spec.loader is None:
            sys.stderr.write(
                f"Unable to load shared ticket lint implementation from {module_path}\\n"
            )
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - wrapper should surface import failures directly
            sys.stderr.write(
                f"Unable to import shared ticket lint implementation from {module_path}: {exc}\\n"
            )
            return None
        return getattr(module, "run_ticket_lint", None)


    def main(argv: Optional[List[str]] = None) -> int:
        parser = argparse.ArgumentParser(description="Lint CAR ticket frontmatter.")
        parser.add_argument(
            "--fix-ticket-ids",
            action="store_true",
            help="Backfill missing or invalid ticket_id values before linting.",
        )
        args = parser.parse_args(argv)

        run_ticket_lint = _load_shared_linter()
        if run_ticket_lint is None:
            return 2
        return run_ticket_lint(
            Path(__file__).resolve().parent.parent / "tickets",
            fix_ticket_ids=args.fix_ticket_ids,
        )


    if __name__ == "__main__":  # pragma: no cover
        sys.exit(main())
    """
)


def ensure_ticket_linter(repo_root: Path, *, force: bool = False) -> Path:
    """
    Ensure a portable ticket frontmatter linter exists under .codex-autorunner/bin.
    The file is always considered generated; it may be refreshed when the content changes.
    """

    ensure_ticket_lint_impl(repo_root, force=force)

    path = repo_root / LINTER_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None

    if force or existing != _SCRIPT:
        path.write_text(_SCRIPT, encoding="utf-8")
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)

    return path


def ensure_ticket_lint_impl(repo_root: Path, *, force: bool = False) -> Path:
    path = repo_root / LINT_IMPL_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _portable_ticket_lint_source()

    existing = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None

    if force or existing != content:
        path.write_text(content, encoding="utf-8")

    return path
