from __future__ import annotations

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

_DEMO_SCRIPT_HELP = (
    "Path to YAML/JSON demo manifest. Format: version: 1 and steps: [..]. "
    "Supported actions: goto, click, fill, press, wait_for_url, wait_for_text, "
    "wait_ms, screenshot, snapshot_a11y. Locator priority: role+name, label, "
    "text, test_id, then selector fallback."
)


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
    @app.command("screenshot")
    def render_screenshot(
        url: Optional[str] = typer.Option(
            None, "--url", help="Capture an already-running URL."
        ),
        serve_cmd: Optional[str] = typer.Option(
            None,
            "--serve-cmd",
            help="Command used to start a local app before capture.",
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
        """Capture a screenshot artifact."""
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
            None, "--serve-cmd", help="Command used to start a local app before demo."
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
        """Run a scripted browser demo manifest and capture evidence artifacts."""
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
            help="Command used to start a local app before observe.",
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
        """Capture a structured page observation artifact."""
        _require_render_feature(require_optional_feature)
        ctx = require_repo_config(repo, hub)
        repo_root = _repo_root_from_context(ctx)
        try:
            target = select_render_target(url=url, serve_cmd=serve_cmd, path=path)
            parsed_viewport = parse_viewport(viewport)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        if target.mode != "url" or not target.url:
            raise_exit("Serve mode is not implemented yet for `car render observe`.")
        target_url = target.url
        assert target_url is not None
        final_out_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root,
            out_dir=out_dir,
            output=output,
        )
        result = BrowserRuntime().capture_observe(
            base_url=target_url,
            path=target.path,
            out_dir=final_out_dir,
            viewport=parsed_viewport,
            output_name=output_name,
        )
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
        typer.echo(str(snapshot))
        typer.echo(str(metadata))
