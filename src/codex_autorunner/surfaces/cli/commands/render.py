from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....browser import (
    DEFAULT_VIEWPORT_TEXT,
    parse_viewport,
    resolve_output_path,
    select_render_target,
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
        """Capture a screenshot artifact (stub)."""
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
        output_path = resolve_output_path(
            repo_root=repo_root,
            output=output,
            out_dir=out_dir,
            default_name=f"screenshot.{normalized_format}",
        )
        typer.echo(
            "render screenshot stub: "
            f"mode={target.mode} target_path={target.path} "
            f"viewport={parsed_viewport.width}x{parsed_viewport.height} "
            f"output={output_path}"
        )

    @app.command("demo")
    def render_demo(
        script: Path = typer.Option(
            ...,
            "--script",
            help="Path to a YAML/JSON action manifest for the scripted demo.",
        ),
        url: Optional[str] = typer.Option(
            None, "--url", help="Run demo against an already-running URL."
        ),
        serve_cmd: Optional[str] = typer.Option(
            None, "--serve-cmd", help="Command used to start a local app before demo."
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
        """Run a scripted browser demo capture (stub)."""
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
        output_path = resolve_output_path(
            repo_root=repo_root,
            output=output,
            out_dir=out_dir,
            default_name="demo.mp4" if record_video else "demo.json",
        )
        typer.echo(
            "render demo stub: "
            f"mode={target.mode} target_path={target.path} "
            f"script={script} "
            f"viewport={parsed_viewport.width}x{parsed_viewport.height} "
            f"trace={normalized_trace} output={output_path}"
        )

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
        """Capture a structured page observation artifact (stub)."""
        _require_render_feature(require_optional_feature)
        ctx = require_repo_config(repo, hub)
        repo_root = _repo_root_from_context(ctx)
        try:
            target = select_render_target(url=url, serve_cmd=serve_cmd, path=path)
            parsed_viewport = parse_viewport(viewport)
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)
        output_path = resolve_output_path(
            repo_root=repo_root,
            output=output,
            out_dir=out_dir,
            default_name="observe-a11y.json",
        )
        typer.echo(
            "render observe stub: "
            f"mode={target.mode} target_path={target.path} "
            f"viewport={parsed_viewport.width}x{parsed_viewport.height} "
            f"output={output_path}"
        )
