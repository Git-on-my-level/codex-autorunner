"""PMA context CLI commands (non-thread context management)."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from ...bootstrap import ensure_pma_docs, pma_doc_path
from ...core.config import load_hub_config
from .hub_path_option import hub_root_path_option
from .pma_control_plane import resolve_hub_path

logger = logging.getLogger(__name__)


def register_context_commands(app: typer.Typer) -> None:
    app.command("reset")(pma_context_reset)
    app.command("snapshot")(pma_context_snapshot)
    app.command("prune")(pma_context_prune)
    app.command("compact")(pma_context_compact)


def _extract_compact_summary_items(content: str, *, limit: int) -> list[str]:
    items: list[str] = []
    if limit <= 0:
        return items
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("# pma active context"):
            continue
        if lower.startswith("use this file for"):
            continue
        if lower.startswith("pruning guidance"):
            continue
        if lower.startswith("> auto-pruned on"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        elif line.startswith("* "):
            line = line[2:].strip()
        elif line[:2].isdigit() and line[2:4] == ". ":
            line = line[4:].strip()
        if not line:
            continue
        if line in items:
            continue
        items.append(line)
        if len(items) >= limit:
            break
    return items


def _render_compacted_active_context(
    *,
    timestamp: str,
    previous_line_count: int,
    max_lines: int,
    summary_items: list[str],
) -> str:
    base_lines = [
        "# PMA active context (short-lived)",
        "",
        "## Current priorities",
        "- Keep this section focused on in-flight priorities only.",
        "",
        "## Next steps",
        "- Capture immediate, executable follow-ups for the next PMA turn.",
        "",
        "## Open questions",
        "- Record unresolved blockers requiring explicit answers.",
        "",
        "## Compaction metadata",
        f"- Compacted at: {timestamp}",
        f"- Previous line count: {previous_line_count}",
        f"- Active context line budget: {max_lines}",
        "- Archived snapshot appended to context_log.md.",
        "",
        "## Archived context summary",
    ]
    remaining = max(max_lines - len(base_lines), 0)
    summary_lines = [f"- {item}" for item in summary_items[:remaining]]
    if not summary_lines and max_lines > len(base_lines):
        summary_lines = ["- No additional archival summary captured."]
    output_lines = base_lines + summary_lines
    if max_lines > 0:
        output_lines = output_lines[:max_lines]
    return "\n".join(output_lines).rstrip() + "\n"


def pma_context_reset(
    path: Optional[Path] = hub_root_path_option(),
):
    """Reset active_context.md to a minimal header."""
    hub_root = resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = pma_doc_path(hub_root, "active_context.md")

    minimal_content = """# PMA active context (short-lived)

Use this file for the current working set: active projects, open questions, links, and immediate next steps.

Pruning guidance:
- Keep this file compact (prefer bullet points).
- When it grows too large, summarize older items and move durable guidance to `AGENTS.md`.
- Before a major prune, append a timestamped snapshot to `context_log.md`.
"""

    try:
        active_context_path.write_text(minimal_content, encoding="utf-8")
        typer.echo(f"Reset active_context.md at {active_context_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


def pma_context_snapshot(
    path: Optional[Path] = hub_root_path_option(),
):
    """Snapshot active_context.md into context_log.md with ISO timestamp."""
    hub_root = resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = pma_doc_path(hub_root, "active_context.md")
    context_log_path = pma_doc_path(hub_root, "context_log.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
        typer.echo(f"Appended snapshot to {context_log_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


def pma_context_prune(
    path: Optional[Path] = hub_root_path_option(),
):
    """Prune active_context.md if over budget (snapshot first)."""
    hub_root = resolve_hub_path(path)

    max_lines = 200
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        if pma_cfg is not None:
            max_lines = int(getattr(pma_cfg, "active_context_max_lines", max_lines))
    except (OSError, ValueError):  # intentional: config fallback
        logger.debug(
            "Failed to read active_context_max_lines from config", exc_info=True
        )

    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = pma_doc_path(hub_root, "active_context.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
        line_count = len(active_content.splitlines())
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if line_count <= max_lines:
        typer.echo(
            f"active_context.md has {line_count} lines (budget: {max_lines}), no prune needed"
        )
        return

    typer.echo(
        f"active_context.md has {line_count} lines (budget: {max_lines}), snapshotting and pruning"
    )

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    context_log_path = pma_doc_path(hub_root, "context_log.md")
    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    minimal_content = f"""# PMA active context (short-lived)

Use this file for the current working set: active projects, open questions, links, and immediate next steps.

Pruning guidance:
- Keep this file compact (prefer bullet points).
- When it grows too large, summarize older items and move durable guidance to `AGENTS.md`.
- Before a major prune, append a timestamped snapshot to `context_log.md`.

> Note: This file was pruned on {timestamp} (had {line_count} lines, budget: {max_lines})
"""

    try:
        active_context_path.write_text(minimal_content, encoding="utf-8")
        typer.echo(f"Pruned active_context.md at {active_context_path}")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None


def pma_context_compact(
    max_lines: Optional[int] = typer.Option(
        None, "--max-lines", help="Target max lines for active_context.md"
    ),
    summary_lines: int = typer.Option(
        12, "--summary-lines", help="Max archived summary lines to keep"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Snapshot then compact active_context.md into a deterministic short form."""
    hub_root = resolve_hub_path(path)

    resolved_max_lines = 200
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        if pma_cfg is not None:
            resolved_max_lines = int(
                getattr(pma_cfg, "active_context_max_lines", resolved_max_lines)
            )
    except (OSError, ValueError):  # intentional: config fallback
        logger.debug(
            "Failed to read active_context_max_lines from config", exc_info=True
        )
    if isinstance(max_lines, int):
        resolved_max_lines = max(1, max_lines)
    else:
        resolved_max_lines = max(1, resolved_max_lines)

    resolved_summary_lines = max(0, int(summary_lines))

    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    active_context_path = pma_doc_path(hub_root, "active_context.md")
    context_log_path = pma_doc_path(hub_root, "context_log.md")

    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to read {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    previous_line_count = len(active_content.splitlines())
    timestamp = datetime.now(timezone.utc).isoformat()
    summary_items = _extract_compact_summary_items(
        active_content, limit=resolved_summary_lines
    )
    compacted = _render_compacted_active_context(
        timestamp=timestamp,
        previous_line_count=previous_line_count,
        max_lines=resolved_max_lines,
        summary_items=summary_items,
    )

    if dry_run:
        typer.echo(
            f"Dry run: compact active_context.md (current_lines={previous_line_count}, "
            f"target_max_lines={resolved_max_lines}, summary_lines={resolved_summary_lines})"
        )
        return

    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content
    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except OSError as exc:
        typer.echo(f"Failed to write {context_log_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    try:
        active_context_path.write_text(compacted, encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Failed to write {active_context_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(
        f"Compacted active_context.md at {active_context_path} "
        f"(lines: {previous_line_count} -> {len(compacted.splitlines())})"
    )
