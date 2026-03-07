import shlex
import subprocess
from pathlib import Path

import typer

from ....core.markdown_export import (
    artifact_base_name,
    extract_mermaid_fences,
    rewrite_markdown_with_mermaid_images,
)
from ....core.utils import resolve_executable

SUPPORTED_DIAGRAM_FORMATS = {"png", "pdf", "svg"}
SUPPORTED_DOC_FORMATS = {"html", "pdf", "docx"}


def _resolve_command(raw: str, *, label: str) -> list[str]:
    try:
        parts = [part for part in shlex.split(raw) if part]
    except ValueError as exc:
        raise ValueError(f"Invalid {label} command: {exc}") from exc
    if not parts:
        raise ValueError(f"Invalid {label} command: empty value")
    resolved = resolve_executable(parts[0])
    if not resolved:
        raise ValueError(
            f"Unable to find '{parts[0]}' in PATH for {label}; use --{label}-cmd to override."
        )
    return [resolved, *parts[1:]]


def _run_checked(cmd: list[str], *, label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or "").strip()
    if detail:
        raise RuntimeError(f"{label} failed: {detail}")
    raise RuntimeError(f"{label} failed with exit code {proc.returncode}")


def register_render_commands(app: typer.Typer, raise_exit) -> None:
    @app.command("markdown")
    def render_markdown(
        source: Path = typer.Argument(
            ...,
            help="Markdown file to render.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
        out_dir: Path = typer.Option(
            Path(".codex-autorunner/filebox/outbox"),
            "--out-dir",
            help="Output directory for generated export artifacts.",
            resolve_path=True,
        ),
        mermaid_cmd: str = typer.Option(
            "mmdc",
            "--mermaid-cmd",
            help="Mermaid CLI command. Must support '-i <input>' and '-o <output>'.",
        ),
        pandoc_cmd: str = typer.Option(
            "pandoc",
            "--pandoc-cmd",
            help="Pandoc command used for document exports.",
        ),
        diagram_format: list[str] = typer.Option(
            ["png"],
            "--diagram-format",
            help=(
                "Diagram output format(s): png, pdf, svg. "
                "Repeat to set multiple (default: png)."
            ),
        ),
        doc_format: list[str] = typer.Option(
            ["html"],
            "--doc-format",
            help=(
                "Document output format(s): html, pdf, docx. "
                "Repeat to set multiple (default: html)."
            ),
        ),
        keep_intermediate: bool = typer.Option(
            False,
            "--keep-intermediate",
            help="Keep intermediate rendered markdown used by pandoc.",
        ),
    ) -> None:
        """Render Markdown with Mermaid fences into static export artifacts."""
        try:
            source_markdown = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise_exit(f"Failed to read source markdown: {exc}", cause=exc)

        diagram_formats = list(
            dict.fromkeys(
                value.lower().strip()
                for value in diagram_format
                if value and value.strip()
            )
        )
        if not diagram_formats:
            raise_exit("At least one --diagram-format value is required.")
        invalid_diagram = sorted(set(diagram_formats) - SUPPORTED_DIAGRAM_FORMATS)
        if invalid_diagram:
            raise_exit(
                "Unsupported diagram format(s): "
                + ", ".join(invalid_diagram)
                + ". Supported: png, pdf, svg."
            )

        document_formats = list(
            dict.fromkeys(
                value.lower().strip() for value in doc_format if value and value.strip()
            )
        )
        if not document_formats:
            raise_exit("At least one --doc-format value is required.")
        invalid_doc = sorted(set(document_formats) - SUPPORTED_DOC_FORMATS)
        if invalid_doc:
            raise_exit(
                "Unsupported doc format(s): "
                + ", ".join(invalid_doc)
                + ". Supported: html, pdf, docx."
            )

        try:
            mermaid_bin = _resolve_command(mermaid_cmd, label="mermaid")
            pandoc_bin = _resolve_command(pandoc_cmd, label="pandoc")
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        out_dir.mkdir(parents=True, exist_ok=True)
        base = artifact_base_name(source)
        fences = extract_mermaid_fences(source_markdown)

        image_names: list[str] = []
        for fence in fences:
            mermaid_src = out_dir / f"{base}.diagram-{fence.index:02d}.mmd"
            mermaid_src.write_text(fence.body + "\n", encoding="utf-8")
            png_name = f"{base}.diagram-{fence.index:02d}.png"
            for fmt in diagram_formats:
                artifact_path = out_dir / f"{base}.diagram-{fence.index:02d}.{fmt}"
                cmd = [*mermaid_bin, "-i", str(mermaid_src), "-o", str(artifact_path)]
                try:
                    _run_checked(cmd, label=f"mermaid diagram #{fence.index} ({fmt})")
                except RuntimeError as exc:
                    raise_exit(str(exc), cause=exc)
                if fmt == "png":
                    image_names.append(png_name)
            mermaid_src.unlink(missing_ok=True)

        if fences and not image_names:
            raise_exit(
                "No PNG diagrams were generated; include --diagram-format png so markdown can embed rendered diagrams."
            )

        rendered_markdown = rewrite_markdown_with_mermaid_images(
            source_markdown, fences, image_names
        )
        rendered_markdown_path = out_dir / f"{base}.rendered.md"
        try:
            rendered_markdown_path.write_text(rendered_markdown, encoding="utf-8")
        except OSError as exc:
            raise_exit(f"Failed to write rendered markdown: {exc}", cause=exc)

        document_paths: list[Path] = []
        for fmt in document_formats:
            destination = out_dir / f"{base}.{fmt}"
            cmd = [*pandoc_bin, str(rendered_markdown_path), "-o", str(destination)]
            try:
                _run_checked(cmd, label=f"pandoc ({fmt})")
            except RuntimeError as exc:
                raise_exit(str(exc), cause=exc)
            document_paths.append(destination)

        if not keep_intermediate:
            rendered_markdown_path.unlink(missing_ok=True)

        typer.echo(f"Source: {source}")
        typer.echo(f"Mermaid blocks: {len(fences)}")
        typer.echo(f"Output directory: {out_dir}")
        for path in sorted(out_dir.glob(f"{base}.diagram-*.*")):
            typer.echo(f"- {path}")
        for path in document_paths:
            typer.echo(f"- {path}")
