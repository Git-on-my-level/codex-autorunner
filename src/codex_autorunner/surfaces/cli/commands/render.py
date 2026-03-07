from __future__ import annotations

import shlex
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple

import typer

from ....browser import (
    DEFAULT_VIEWPORT_TEXT,
    BrowserRuntime,
    BrowserServeConfig,
    ServeModeError,
    parse_env_overrides,
    parse_viewport,
    resolve_out_dir,
    select_render_target,
    supervised_server,
)
from ....core.markdown_export import (
    artifact_base_name,
    extract_mermaid_fences,
    rewrite_markdown_with_mermaid_images,
)
from ....core.utils import resolve_executable

SUPPORTED_DIAGRAM_FORMATS = {"png", "pdf", "svg"}
SUPPORTED_DOC_FORMATS = {"html", "pdf", "docx"}

_DEMO_SCRIPT_HELP = (
    "Path to YAML/JSON demo manifest. Format: version: 1 and steps: [..]. "
    "Supported actions: goto, click, fill, press, wait_for_url, wait_for_text, "
    "wait_ms, screenshot, snapshot_a11y. Locator priority: role+name, label, "
    "text, test_id, then selector fallback."
)


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


def _require_render_feature(require_optional_feature: Callable[..., None]) -> None:
    require_optional_feature(
        feature="render",
        deps=[("playwright", "playwright")],
        extra="browser",
    )


def _repo_root_from_context(context: Any) -> Path:
    repo_root = getattr(context, "repo_root", None)
    if isinstance(repo_root, Path):
        return repo_root
    return Path.cwd()


def _resolve_out_dir_and_name(
    *,
    repo_root: Path,
    out_dir: Optional[Path],
    output: Optional[Path],
) -> tuple[Path, Optional[str]]:
    base_dir = resolve_out_dir(repo_root, out_dir)
    if output is None:
        return base_dir, None
    if output.is_absolute():
        return output.parent, output.name
    if output.parent != Path("."):
        return base_dir / output.parent, output.name
    return base_dir, output.name


def _runtime_error_category(error_type: Optional[str]) -> str:
    if error_type == "BrowserNavigationError":
        return "navigation_failure"
    if error_type == "BrowserArtifactError":
        return "artifact_write_failure"
    if error_type == "ManifestValidationError":
        return "manifest_validation"
    if error_type == "DemoStepError":
        return "step_failure"
    return "capture_failure"


@contextmanager
def _resolve_target_base_url(
    *,
    target: Any,
    ready_url: Optional[str],
    ready_log_pattern: Optional[str],
    cwd: Optional[Path],
    env: Optional[list[str]],
    ready_timeout_seconds: float,
) -> Iterator[Tuple[str, str]]:
    if target.mode == "url":
        if not target.url:
            raise ValueError("URL target is missing URL value.")
        with nullcontext((target.url, "url")) as resolved:
            yield resolved
        return

    if target.mode != "serve" or not target.serve_cmd:
        raise ValueError("Serve mode target is missing command.")

    env_overrides = parse_env_overrides(env or [])
    config = BrowserServeConfig(
        serve_cmd=target.serve_cmd,
        ready_url=ready_url,
        ready_log_pattern=ready_log_pattern,
        cwd=cwd,
        env_overrides=env_overrides,
        timeout_seconds=ready_timeout_seconds,
    )
    with supervised_server(config) as session:
        if not session.target_url:
            raise ServeModeError(
                "Serve readiness succeeded, but target URL could not be derived. "
                "Provide --ready-url or use a --ready-log-pattern with a named "
                "group (?P<url>http://...)."
            )
        yield session.target_url, session.ready_source


def register_render_commands(
    app: typer.Typer,
    *,
    require_optional_feature: Callable[..., None],
    require_repo_config: Callable[[Optional[Path], Optional[Path]], Any],
    raise_exit: Callable[..., None],
) -> None:
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

    @app.command("screenshot")
    def render_screenshot(
        url: Optional[str] = typer.Option(
            None, "--url", help="Capture an already-running URL."
        ),
        serve_cmd: Optional[str] = typer.Option(
            None,
            "--serve-cmd",
            help="Command used to start a local app before capture. CAR tears it down on every exit path.",
        ),
        ready_url: Optional[str] = typer.Option(
            None,
            "--ready-url",
            help="Readiness URL polled until healthy (preferred in serve mode).",
        ),
        ready_log_pattern: Optional[str] = typer.Option(
            None,
            "--ready-log-pattern",
            help="Regex matched against serve stdout/stderr when --ready-url is absent.",
        ),
        cwd: Optional[Path] = typer.Option(
            None,
            "--cwd",
            help="Working directory for the serve command.",
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Repeat KEY=VALUE overrides passed to the serve command environment.",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: str = typer.Option("/", "--path", help="Relative path to open."),
        viewport: str = typer.Option(
            DEFAULT_VIEWPORT_TEXT,
            "--viewport",
            help="Viewport in WIDTHxHEIGHT format.",
        ),
        format: str = typer.Option(
            "png",
            "--format",
            help="Screenshot output format: png or pdf.",
        ),
        output: Optional[Path] = typer.Option(
            None, "--output", help="Output filename or absolute path."
        ),
        out_dir: Optional[Path] = typer.Option(
            None,
            "--out-dir",
            help="Output directory (defaults to .codex-autorunner/filebox/outbox).",
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo root path."),
        hub: Optional[Path] = typer.Option(
            None, "--hub", "--hub-path", help="Hub root or config path."
        ),
    ) -> None:
        """Capture a screenshot from URL mode or serve mode with guaranteed serve cleanup."""
        _require_render_feature(require_optional_feature)
        ctx = require_repo_config(repo, hub)
        repo_root = _repo_root_from_context(ctx)
        try:
            target = select_render_target(url=url, serve_cmd=serve_cmd, path=path)
            parsed_viewport = parse_viewport(viewport)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        normalized_format = (format or "").strip().lower()
        if normalized_format not in {"png", "pdf"}:
            raise_exit("Invalid --format value. Expected one of: png, pdf.")

        final_out_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root,
            out_dir=out_dir,
            output=output,
        )
        try:
            with _resolve_target_base_url(
                target=target,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                ready_timeout_seconds=ready_timeout_seconds,
            ) as (base_url, _ready_source):
                result = BrowserRuntime().capture_screenshot(
                    base_url=base_url,
                    path=target.path,
                    out_dir=final_out_dir,
                    viewport=parsed_viewport,
                    output_name=output_name,
                    output_format=normalized_format,
                )
        except ServeModeError as exc:
            raise_exit(
                f"Render screenshot failed ({exc.category}): {str(exc) or 'Unknown serve-mode error.'}",
                cause=exc,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        except KeyboardInterrupt:
            raise_exit("Render screenshot interrupted; serve process was terminated.")

        if not result.ok:
            category = _runtime_error_category(result.error_type)
            raise_exit(
                f"Render screenshot failed ({category}): "
                f"{result.error_message or 'Unknown capture error.'}"
            )
        capture = result.artifacts.get("capture")
        if capture is None:
            raise_exit("Render screenshot did not produce an artifact.")
        typer.echo(str(capture))

    @app.command("demo")
    def render_demo(
        script: Path = typer.Option(
            ...,
            "--script",
            help=_DEMO_SCRIPT_HELP,
        ),
        url: Optional[str] = typer.Option(
            None, "--url", help="Run demo against an already-running URL."
        ),
        serve_cmd: Optional[str] = typer.Option(
            None,
            "--serve-cmd",
            help="Command used to start a local app before demo. CAR tears it down on every exit path.",
        ),
        ready_url: Optional[str] = typer.Option(
            None,
            "--ready-url",
            help="Readiness URL polled until healthy (preferred in serve mode).",
        ),
        ready_log_pattern: Optional[str] = typer.Option(
            None,
            "--ready-log-pattern",
            help="Regex matched against serve stdout/stderr when --ready-url is absent.",
        ),
        cwd: Optional[Path] = typer.Option(
            None,
            "--cwd",
            help="Working directory for the serve command.",
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Repeat KEY=VALUE overrides passed to the serve command environment.",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: str = typer.Option("/", "--path", help="Relative path to open."),
        viewport: str = typer.Option(
            DEFAULT_VIEWPORT_TEXT,
            "--viewport",
            help="Viewport in WIDTHxHEIGHT format.",
        ),
        record_video: bool = typer.Option(
            False,
            "--record-video/--no-record-video",
            help="Record a demo video artifact.",
        ),
        trace: str = typer.Option(
            "off",
            "--trace",
            help="Trace mode: off, on, or retain-on-failure.",
        ),
        output: Optional[Path] = typer.Option(
            None, "--output", help="Output filename or absolute path."
        ),
        out_dir: Optional[Path] = typer.Option(
            None,
            "--out-dir",
            help="Output directory (defaults to .codex-autorunner/filebox/outbox).",
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo root path."),
        hub: Optional[Path] = typer.Option(
            None, "--hub", "--hub-path", help="Hub root or config path."
        ),
    ) -> None:
        """Run a deterministic demo manifest from URL or serve mode with guaranteed serve cleanup."""
        _require_render_feature(require_optional_feature)
        ctx = require_repo_config(repo, hub)
        repo_root = _repo_root_from_context(ctx)
        try:
            target = select_render_target(url=url, serve_cmd=serve_cmd, path=path)
            parsed_viewport = parse_viewport(viewport)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        normalized_trace = (trace or "").strip().lower()
        if normalized_trace not in {"off", "on", "retain-on-failure"}:
            raise_exit(
                "Invalid --trace value. Expected one of: off, on, retain-on-failure."
            )
        if not script.exists():
            raise_exit(f"Demo script not found: {script}")
        if script.is_dir():
            raise_exit(f"Demo script must be a file, got directory: {script}")

        output_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root,
            out_dir=out_dir,
            output=output,
        )
        try:
            with _resolve_target_base_url(
                target=target,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                ready_timeout_seconds=ready_timeout_seconds,
            ) as (base_url, _ready_source):
                result = BrowserRuntime().capture_demo(
                    base_url=base_url,
                    path=target.path,
                    script_path=script,
                    out_dir=output_dir,
                    viewport=parsed_viewport,
                    record_video=record_video,
                    trace_mode=normalized_trace,
                    output_name=output_name,
                )
        except ServeModeError as exc:
            raise_exit(
                f"Render demo failed ({exc.category}): {str(exc) or 'Unknown serve-mode error.'}",
                cause=exc,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        except KeyboardInterrupt:
            raise_exit("Render demo interrupted; serve process was terminated.")

        if not result.ok:
            category = _runtime_error_category(result.error_type)
            raise_exit(
                f"Render demo failed ({category}): "
                f"{result.error_message or 'Unknown demo capture error.'}"
            )

        summary = result.artifacts.get("summary")
        if summary is None:
            raise_exit("Render demo did not produce a summary artifact.")

        typer.echo(str(summary))
        for artifact_key in sorted(result.artifacts):
            if artifact_key == "summary":
                continue
            typer.echo(str(result.artifacts[artifact_key]))

    @app.command("observe")
    def render_observe(
        url: Optional[str] = typer.Option(
            None, "--url", help="Observe an already-running URL."
        ),
        serve_cmd: Optional[str] = typer.Option(
            None,
            "--serve-cmd",
            help="Command used to start a local app before observe. CAR tears it down on every exit path.",
        ),
        ready_url: Optional[str] = typer.Option(
            None,
            "--ready-url",
            help="Readiness URL polled until healthy (preferred in serve mode).",
        ),
        ready_log_pattern: Optional[str] = typer.Option(
            None,
            "--ready-log-pattern",
            help="Regex matched against serve stdout/stderr when --ready-url is absent.",
        ),
        cwd: Optional[Path] = typer.Option(
            None,
            "--cwd",
            help="Working directory for the serve command.",
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Repeat KEY=VALUE overrides passed to the serve command environment.",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: str = typer.Option("/", "--path", help="Relative path to open."),
        viewport: str = typer.Option(
            DEFAULT_VIEWPORT_TEXT,
            "--viewport",
            help="Viewport in WIDTHxHEIGHT format.",
        ),
        output: Optional[Path] = typer.Option(
            None, "--output", help="Output filename or absolute path."
        ),
        out_dir: Optional[Path] = typer.Option(
            None,
            "--out-dir",
            help="Output directory (defaults to .codex-autorunner/filebox/outbox).",
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo root path."),
        hub: Optional[Path] = typer.Option(
            None, "--hub", "--hub-path", help="Hub root or config path."
        ),
    ) -> None:
        """Capture deterministic, accessibility-first observations from URL or serve mode with guaranteed cleanup."""
        _require_render_feature(require_optional_feature)
        ctx = require_repo_config(repo, hub)
        repo_root = _repo_root_from_context(ctx)
        try:
            target = select_render_target(url=url, serve_cmd=serve_cmd, path=path)
            parsed_viewport = parse_viewport(viewport)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        final_out_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root,
            out_dir=out_dir,
            output=output,
        )
        try:
            with _resolve_target_base_url(
                target=target,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                ready_timeout_seconds=ready_timeout_seconds,
            ) as (base_url, _ready_source):
                result = BrowserRuntime().capture_observe(
                    base_url=base_url,
                    path=target.path,
                    out_dir=final_out_dir,
                    viewport=parsed_viewport,
                    output_name=output_name,
                )
        except ServeModeError as exc:
            raise_exit(
                f"Render observe failed ({exc.category}): {str(exc) or 'Unknown serve-mode error.'}",
                cause=exc,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        except KeyboardInterrupt:
            raise_exit("Render observe interrupted; serve process was terminated.")

        if not result.ok:
            category = _runtime_error_category(result.error_type)
            raise_exit(
                f"Render observe failed ({category}): "
                f"{result.error_message or 'Unknown capture error.'}"
            )
        snapshot = result.artifacts.get("snapshot")
        metadata = result.artifacts.get("metadata")
        if snapshot is None or metadata is None:
            raise_exit("Render observe did not produce required artifacts.")
        for artifact_key in sorted(result.artifacts):
            typer.echo(str(result.artifacts[artifact_key]))
