from __future__ import annotations

import json
import shlex
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple

import typer

from ....browser import (
    DEFAULT_VIEWPORT_TEXT,
    BrowserPreflightResult,
    BrowserRuntime,
    BrowserServeConfig,
    BrowserServerSupervisor,
    MissingRenderSessionError,
    RenderSessionError,
    ServeModeError,
    StaleRenderSessionError,
    load_render_session,
    parse_env_overrides,
    parse_viewport,
    persist_render_session,
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
MEDIA_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".pdf",
    ".webm",
    ".mp4",
    ".mov",
}

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


def _resolve_project_root(
    *,
    repo_root: Path,
    project_root: Optional[Path],
    project_context: bool,
) -> Optional[Path]:
    if not project_context:
        return None
    candidate = project_root or repo_root
    return candidate.expanduser().resolve()


def _format_serve_context_details(
    *,
    project_context: bool,
    project_root: Optional[Path],
    cwd: Optional[Path],
) -> str:
    context_value = "enabled" if project_context else "disabled"
    root_value = str(project_root) if project_root is not None else "<none>"
    cwd_value = str(cwd) if cwd is not None else "<inherit>"
    return (
        f"project_context={context_value}, "
        f"project_root={root_value}, "
        f"cwd={cwd_value}"
    )


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


def _preflight_error_category(error_type: Optional[str]) -> str:
    if error_type == "ManifestValidationError":
        return "manifest_validation"
    if error_type == "BrowserNavigationError":
        return "navigation_failure"
    if error_type == "DemoStepError":
        return "step_validation_failure"
    return "preflight_failure"


def _write_preflight_report(
    *,
    report_path: Path,
    result: BrowserPreflightResult,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "ok": result.ok,
        "mode": result.mode,
        "target_url": result.target_url,
        "error_type": result.error_type,
        "error_message": result.error_message,
        "steps": [
            {
                "index": step.index,
                "action": step.action,
                "ok": step.ok,
                "detail": step.detail,
            }
            for step in result.diagnostics.steps
        ],
    }
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit_preflight_output(
    *,
    result: BrowserPreflightResult,
    report_path: Optional[Path],
) -> None:
    status = "passed" if result.ok else "failed"
    typer.echo(f"Demo preflight {status}.")
    if report_path is not None:
        typer.echo(f"Preflight report: {report_path}")
    for step in result.diagnostics.steps:
        step_status = "ok" if step.ok else "fail"
        typer.echo(f"[{step_status}] step {step.index} {step.action}: {step.detail}")
    if result.diagnostics.error_message:
        typer.echo(result.diagnostics.error_message)


def _validate_demo_session_flags(
    *,
    target: Any,
    session_id: Optional[str],
    keep_session: bool,
    attach_session: bool,
    raise_exit: Callable[..., None],
) -> Optional[str]:
    normalized_session_id = (session_id or "").strip() or None
    if keep_session and attach_session:
        raise_exit("Use either --keep-session or --attach-session, not both.")
    if keep_session and target.mode != "serve":
        raise_exit("--keep-session is only supported in serve mode.")
    if attach_session and target.mode != "url":
        raise_exit(
            "--attach-session reuses a kept serve session and cannot be combined with --serve-cmd."
        )
    if (keep_session or attach_session) and not normalized_session_id:
        raise_exit(
            "Provide --session-id when using --keep-session or --attach-session."
        )
    if normalized_session_id and not (keep_session or attach_session):
        raise_exit(
            "Provide --keep-session or --attach-session when using --session-id."
        )
    return normalized_session_id


def _is_media_artifact(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def _prune_non_media_artifacts(
    *,
    artifacts: dict[str, Path],
    output_dir: Path,
) -> dict[str, Path]:
    filtered: dict[str, Path] = {}
    output_root = output_dir.resolve()
    for key, artifact_path in artifacts.items():
        if _is_media_artifact(artifact_path):
            filtered[key] = artifact_path
            continue

        try:
            resolved = artifact_path.resolve()
            is_within_output = resolved.is_relative_to(output_root)
        except Exception:
            is_within_output = False
        if is_within_output and artifact_path.exists():
            artifact_path.unlink(missing_ok=True)

    return filtered


@contextmanager
def _resolve_target_base_url(
    *,
    target: Any,
    repo_root: Path,
    ready_url: Optional[str],
    ready_log_pattern: Optional[str],
    cwd: Optional[Path],
    env: Optional[list[str]],
    project_root: Optional[Path],
    project_context: bool,
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

    resolved_project_root = _resolve_project_root(
        repo_root=repo_root,
        project_root=project_root,
        project_context=project_context,
    )
    resolved_cwd = cwd
    if resolved_cwd is None and resolved_project_root is not None:
        resolved_cwd = resolved_project_root
    context_details = _format_serve_context_details(
        project_context=project_context,
        project_root=resolved_project_root,
        cwd=resolved_cwd,
    )
    env_overrides = parse_env_overrides(env or [])
    config = BrowserServeConfig(
        serve_cmd=target.serve_cmd,
        ready_url=ready_url,
        ready_log_pattern=ready_log_pattern,
        cwd=cwd,
        env_overrides=env_overrides,
        project_root=resolved_project_root,
        project_context_enabled=project_context,
        timeout_seconds=ready_timeout_seconds,
    )
    try:
        with supervised_server(config) as session:
            if not session.target_url:
                raise ServeModeError(
                    "Serve readiness succeeded, but target URL could not be derived. "
                    "Provide --ready-url or use a --ready-log-pattern with a named "
                    "group (?P<url>http://...)."
                )
            yield session.target_url, session.ready_source
    except ServeModeError as exc:
        detail = str(exc) or "Unknown serve-mode error."
        raise type(exc)(f"{detail} (serve context: {context_details})") from exc


@contextmanager
def _resolve_demo_target_base_url(
    *,
    target: Any,
    repo_root: Path,
    ready_url: Optional[str],
    ready_log_pattern: Optional[str],
    cwd: Optional[Path],
    env: Optional[list[str]],
    project_root: Optional[Path],
    project_context: bool,
    ready_timeout_seconds: float,
    session_id: Optional[str],
    keep_session: bool,
    attach_session: bool,
) -> Iterator[Tuple[str, str]]:
    if attach_session:
        assert session_id is not None
        metadata = load_render_session(
            repo_root=repo_root,
            session_id=session_id,
            require_live=True,
        )
        with nullcontext((metadata.target_url, "attached_session")) as resolved:
            yield resolved
        return

    if not keep_session:
        with _resolve_target_base_url(
            target=target,
            repo_root=repo_root,
            ready_url=ready_url,
            ready_log_pattern=ready_log_pattern,
            cwd=cwd,
            env=env,
            project_root=project_root,
            project_context=project_context,
            ready_timeout_seconds=ready_timeout_seconds,
        ) as resolved:
            yield resolved
        return

    if target.mode != "serve" or not target.serve_cmd:
        raise ServeModeError("--keep-session requires serve mode with --serve-cmd.")
    assert session_id is not None
    resolved_project_root = _resolve_project_root(
        repo_root=repo_root,
        project_root=project_root,
        project_context=project_context,
    )
    resolved_cwd = cwd
    if resolved_cwd is None and resolved_project_root is not None:
        resolved_cwd = resolved_project_root
    context_details = _format_serve_context_details(
        project_context=project_context,
        project_root=resolved_project_root,
        cwd=resolved_cwd,
    )
    config = BrowserServeConfig(
        serve_cmd=target.serve_cmd,
        ready_url=ready_url,
        ready_log_pattern=ready_log_pattern,
        cwd=cwd,
        env_overrides=parse_env_overrides(env or []),
        project_root=resolved_project_root,
        project_context_enabled=project_context,
        timeout_seconds=ready_timeout_seconds,
    )
    supervisor = BrowserServerSupervisor(config)
    keep_alive = False
    try:
        supervisor.start()
        session = supervisor.wait_until_ready()
        if not session.target_url:
            raise ServeModeError(
                "Serve readiness succeeded, but target URL could not be derived. "
                "Provide --ready-url or use a --ready-log-pattern with a named "
                "group (?P<url>http://...)."
            )
        persist_render_session(
            repo_root=repo_root,
            session_id=session_id,
            config=config,
            session=session,
        )
        keep_alive = True
        with nullcontext((session.target_url, session.ready_source)) as resolved:
            yield resolved
    except ServeModeError:
        raise
    except Exception as exc:
        detail = str(exc) or "Unknown serve-mode error."
        raise ServeModeError(f"{detail} (serve context: {context_details})") from exc
    finally:
        if not keep_alive:
            supervisor.stop()


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
        project_root: Optional[Path] = typer.Option(
            None,
            "--project-root",
            help="Project root for serve-mode context (defaults to repo root).",
        ),
        project_context: bool = typer.Option(
            True,
            "--project-context/--no-project-context",
            help="Enable project-aware serve context (PATH bin injection + cwd fallback).",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: Optional[str] = typer.Option(
            None,
            "--path",
            help=(
                "Relative path to open. If omitted in --url mode, preserves "
                "the URL path/query."
            ),
        ),
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
                repo_root=repo_root,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                project_root=project_root,
                project_context=project_context,
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
        project_root: Optional[Path] = typer.Option(
            None,
            "--project-root",
            help="Project root for serve-mode context (defaults to repo root).",
        ),
        project_context: bool = typer.Option(
            True,
            "--project-context/--no-project-context",
            help="Enable project-aware serve context (PATH bin injection + cwd fallback).",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: Optional[str] = typer.Option(
            None,
            "--path",
            help=(
                "Relative path to open. If omitted in --url mode, preserves "
                "the URL path/query."
            ),
        ),
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
        preflight: bool = typer.Option(
            False,
            "--preflight",
            help="Run live preflight checks against the demo manifest before capture.",
        ),
        preflight_only: bool = typer.Option(
            False,
            "--preflight-only",
            help="Run live preflight checks only and skip demo capture.",
        ),
        preflight_report: Optional[Path] = typer.Option(
            None,
            "--preflight-report",
            help="Write preflight diagnostics JSON report to this path.",
        ),
        session_id: Optional[str] = typer.Option(
            None,
            "--session-id",
            help="Stable id used with --keep-session or --attach-session.",
        ),
        keep_session: bool = typer.Option(
            False,
            "--keep-session/--no-keep-session",
            help=(
                "Serve mode only: keep the spawned serve process alive and persist "
                "session metadata for later attachment."
            ),
        ),
        attach_session: bool = typer.Option(
            False,
            "--attach-session",
            help=(
                "URL mode only: attach to an existing kept session from "
                ".codex-autorunner/render_sessions."
            ),
        ),
        full_artifacts: bool = typer.Option(
            False,
            "--full-artifacts/--media-only",
            help=(
                "Emit full structured artifacts (JSON/HTML/trace) or keep only "
                "end-user media artifacts (screenshots/video)."
            ),
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
            normalized_session_id = _validate_demo_session_flags(
                target=target,
                session_id=session_id,
                keep_session=keep_session,
                attach_session=attach_session,
                raise_exit=raise_exit,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        if preflight_only:
            preflight = True
        if preflight_report is not None and not preflight:
            raise_exit(
                "Use --preflight or --preflight-only when providing --preflight-report."
            )
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
        resolved_preflight_report: Optional[Path] = None
        if preflight_report is not None:
            resolved_preflight_report = (
                preflight_report
                if preflight_report.is_absolute()
                else (repo_root / preflight_report)
            )
        runtime = BrowserRuntime()
        try:
            with _resolve_demo_target_base_url(
                target=target,
                repo_root=repo_root,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                project_root=project_root,
                project_context=project_context,
                ready_timeout_seconds=ready_timeout_seconds,
                session_id=normalized_session_id,
                keep_session=keep_session,
                attach_session=attach_session,
            ) as (base_url, _ready_source):
                if preflight:
                    preflight_result = runtime.preflight_demo(
                        base_url=base_url,
                        path=target.path,
                        script_path=script,
                        viewport=parsed_viewport,
                    )
                    if resolved_preflight_report is not None:
                        _write_preflight_report(
                            report_path=resolved_preflight_report,
                            result=preflight_result,
                        )
                    _emit_preflight_output(
                        result=preflight_result,
                        report_path=resolved_preflight_report,
                    )
                    if not preflight_result.ok:
                        category = _preflight_error_category(
                            preflight_result.error_type
                        )
                        raise_exit(
                            f"Render demo preflight failed ({category}): "
                            f"{preflight_result.error_message or 'Unknown preflight error.'}"
                        )
                    if preflight_only:
                        if keep_session and normalized_session_id is not None:
                            session_record = (
                                repo_root
                                / ".codex-autorunner"
                                / "render_sessions"
                                / f"{normalized_session_id}.json"
                            )
                            typer.echo(
                                f"Render session kept: {normalized_session_id} ({session_record})"
                            )
                        return

                result = runtime.capture_demo(
                    base_url=base_url,
                    path=target.path,
                    script_path=script,
                    out_dir=output_dir,
                    viewport=parsed_viewport,
                    record_video=record_video,
                    trace_mode=normalized_trace,
                    output_name=output_name,
                )
        except MissingRenderSessionError as exc:
            raise_exit(
                f"Render demo failed ({exc.category}): {exc} "
                "Start one with `car render demo --serve-cmd ... --keep-session --session-id ...`.",
                cause=exc,
            )
        except StaleRenderSessionError as exc:
            raise_exit(
                f"Render demo failed ({exc.category}): {exc} "
                "Restart it with `--keep-session` to refresh metadata.",
                cause=exc,
            )
        except RenderSessionError as exc:
            raise_exit(
                f"Render demo failed ({exc.category}): "
                f"{str(exc) or 'Render session metadata is invalid.'}",
                cause=exc,
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

        artifacts_to_emit = result.artifacts
        if not full_artifacts:
            artifacts_to_emit = _prune_non_media_artifacts(
                artifacts=result.artifacts,
                output_dir=output_dir,
            )
            if not artifacts_to_emit:
                raise_exit(
                    "Render demo completed but media-only mode found no media "
                    "artifacts. Add a screenshot step and/or --record-video, or "
                    "rerun with --full-artifacts."
                )

        for artifact_key in sorted(artifacts_to_emit):
            typer.echo(str(artifacts_to_emit[artifact_key]))
        if keep_session and normalized_session_id is not None:
            session_record = (
                repo_root
                / ".codex-autorunner"
                / "render_sessions"
                / f"{normalized_session_id}.json"
            )
            typer.echo(
                f"Render session kept: {normalized_session_id} ({session_record})"
            )

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
        project_root: Optional[Path] = typer.Option(
            None,
            "--project-root",
            help="Project root for serve-mode context (defaults to repo root).",
        ),
        project_context: bool = typer.Option(
            True,
            "--project-context/--no-project-context",
            help="Enable project-aware serve context (PATH bin injection + cwd fallback).",
        ),
        ready_timeout_seconds: float = typer.Option(
            30.0,
            "--ready-timeout-seconds",
            help="Serve readiness timeout in seconds.",
        ),
        path: Optional[str] = typer.Option(
            None,
            "--path",
            help=(
                "Relative path to open. If omitted in --url mode, preserves "
                "the URL path/query."
            ),
        ),
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
                repo_root=repo_root,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                project_root=project_root,
                project_context=project_context,
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
