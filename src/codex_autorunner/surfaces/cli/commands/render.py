from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....browser import (
    DEFAULT_VIEWPORT_TEXT,
    BrowserRuntime,
    parse_viewport,
    resolve_out_dir,
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
        if target.mode != "url" or not target.url:
            raise_exit("Serve mode is not implemented yet for `car render screenshot`.")
        target_url = target.url
        assert target_url is not None
        final_out_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root,
            out_dir=out_dir,
            output=output,
        )
        result = BrowserRuntime().capture_screenshot(
            base_url=target_url,
            path=target.path,
            out_dir=final_out_dir,
            viewport=parsed_viewport,
            output_name=output_name,
            output_format=normalized_format,
        )
        if not result.ok:
            raise_exit(
                f"Render screenshot failed ({result.error_type or 'Error'}): "
                f"{result.error_message or 'Unknown error.'}"
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
        output_dir, output_name = _resolve_out_dir_and_name(
            repo_root=repo_root, out_dir=out_dir, output=output
        )
        typer.echo(
            "render demo stub: "
            f"mode={target.mode} target_path={target.path} "
            f"script={script} "
            f"viewport={parsed_viewport.width}x{parsed_viewport.height} "
            f"trace={normalized_trace} out_dir={output_dir} output={output_name}"
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
            raise_exit(
                f"Render observe failed ({result.error_type or 'Error'}): "
                f"{result.error_message or 'Unknown error.'}"
            )
        snapshot = result.artifacts.get("snapshot")
        metadata = result.artifacts.get("metadata")
        if snapshot is None or metadata is None:
            raise_exit("Render observe did not produce required artifacts.")
        typer.echo(str(snapshot))
        typer.echo(str(metadata))
