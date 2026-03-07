from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .actions import execute_demo_manifest, load_demo_manifest
from .artifacts import (
    deterministic_artifact_name,
    reserve_artifact_path,
    write_json_artifact,
    write_text_artifact,
)
from .models import DEFAULT_VIEWPORT, Viewport

PlaywrightLoader = Callable[[], Any]


class BrowserNavigationError(RuntimeError):
    """Page navigation failed before capture could run."""


class BrowserArtifactError(RuntimeError):
    """Artifact capture/write failed after navigation."""


class ManifestValidationError(RuntimeError):
    """Demo script manifest failed validation/parsing."""


class DemoStepError(RuntimeError):
    """A demo step failed during execution."""


@dataclass(frozen=True)
class BrowserRunResult:
    ok: bool
    mode: str
    target_url: Optional[str]
    artifacts: Dict[str, Path] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    error_message: Optional[str] = None
    error_type: Optional[str] = None


def load_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - exercised through optional-deps gate.
        raise RuntimeError(
            "Playwright is unavailable. Install optional browser dependencies first."
        ) from exc
    return sync_playwright().start()


def build_navigation_url(base_url: str, path: str = "/") -> str:
    if not base_url or not base_url.strip():
        raise ValueError("A non-empty base URL is required.")
    normalized_base = base_url.strip()
    normalized_path = (path or "/").strip() or "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    base = urlsplit(normalized_base)
    override = urlsplit(normalized_path)
    return urlunsplit(
        (
            base.scheme,
            base.netloc,
            override.path or "/",
            override.query,
            override.fragment,
        )
    )


class BrowserRuntime:
    def __init__(
        self,
        *,
        playwright_loader: Optional[PlaywrightLoader] = None,
    ) -> None:
        self._playwright_loader = playwright_loader or load_playwright

    def capture_screenshot(
        self,
        *,
        base_url: str,
        path: str = "/",
        out_dir: Path,
        viewport: Viewport = DEFAULT_VIEWPORT,
        output_name: Optional[str] = None,
        output_format: str = "png",
        full_page: bool = True,
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
    ) -> BrowserRunResult:
        fmt = (output_format or "").strip().lower()
        if fmt not in {"png", "pdf"}:
            return BrowserRunResult(
                ok=False,
                mode="screenshot",
                target_url=None,
                error_message="Invalid output format. Expected one of: png, pdf.",
                error_type="ValueError",
            )

        nav_url = build_navigation_url(base_url, path)
        filename = deterministic_artifact_name(
            kind="screenshot",
            extension=fmt,
            url=nav_url,
            path_hint=path,
            output_name=output_name,
        )
        artifact_path, _collision = reserve_artifact_path(out_dir, filename)

        def _action(page: Any) -> tuple[dict[str, Path], dict[str, str]]:
            if fmt == "pdf":
                page.pdf(path=str(artifact_path))
            else:
                page.screenshot(path=str(artifact_path), full_page=full_page)
            return {"capture": artifact_path}, {}

        return self._run_page_action(
            mode="screenshot",
            nav_url=nav_url,
            viewport=viewport,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
            action=_action,
        )

    def capture_observe(
        self,
        *,
        base_url: str,
        path: str = "/",
        out_dir: Path,
        viewport: Viewport = DEFAULT_VIEWPORT,
        output_name: Optional[str] = None,
        include_html: bool = True,
        max_html_bytes: int = 250_000,
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
    ) -> BrowserRunResult:
        nav_url = build_navigation_url(base_url, path)
        snapshot_name = deterministic_artifact_name(
            kind="observe-a11y",
            extension="json",
            url=nav_url,
            path_hint=path,
            output_name=output_name,
        )
        metadata_name = deterministic_artifact_name(
            kind="observe-meta",
            extension="json",
            url=nav_url,
            path_hint=path,
        )
        html_name = deterministic_artifact_name(
            kind="observe-dom",
            extension="html",
            url=nav_url,
            path_hint=path,
        )

        def _action(page: Any) -> tuple[dict[str, Path], dict[str, str]]:
            artifacts: dict[str, Path] = {}
            skipped: dict[str, str] = {}

            snapshot_payload = None
            accessibility = getattr(page, "accessibility", None)
            if accessibility is not None and hasattr(accessibility, "snapshot"):
                snapshot_payload = accessibility.snapshot()
            snapshot_result = write_json_artifact(
                out_dir=out_dir,
                filename=snapshot_name,
                payload=snapshot_payload,
            )
            artifacts["snapshot"] = snapshot_result.path

            title_text = ""
            if hasattr(page, "title"):
                title_text = str(page.title() or "")
            current_url = str(getattr(page, "url", "") or nav_url)
            metadata_result = write_json_artifact(
                out_dir=out_dir,
                filename=metadata_name,
                payload={
                    "captured_url": current_url,
                    "title": title_text,
                    "snapshot_file": snapshot_result.path.name,
                },
            )
            artifacts["metadata"] = metadata_result.path

            if include_html and hasattr(page, "content"):
                html = str(page.content() or "")
                html_size = len(html.encode("utf-8"))
                if html_size <= max_html_bytes:
                    html_result = write_text_artifact(
                        out_dir=out_dir,
                        filename=html_name,
                        content=html,
                    )
                    artifacts["html"] = html_result.path
                else:
                    skipped["html"] = (
                        f"Skipped HTML snapshot ({html_size} bytes > {max_html_bytes} bytes)."
                    )
            return artifacts, skipped

        return self._run_page_action(
            mode="observe",
            nav_url=nav_url,
            viewport=viewport,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
            action=_action,
        )

    def capture_demo(
        self,
        *,
        base_url: str,
        path: str = "/",
        script_path: Path,
        out_dir: Path,
        viewport: Viewport = DEFAULT_VIEWPORT,
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
        record_video: bool = False,
        trace_mode: str = "off",
        output_name: Optional[str] = None,
    ) -> BrowserRunResult:
        normalized_trace = (trace_mode or "").strip().lower()
        if normalized_trace not in {"off", "on", "retain-on-failure"}:
            return BrowserRunResult(
                ok=False,
                mode="demo",
                target_url=None,
                error_message=(
                    "Invalid trace mode. Expected one of: off, on, retain-on-failure."
                ),
                error_type="ValueError",
            )

        try:
            manifest = load_demo_manifest(script_path)
        except Exception as exc:
            return BrowserRunResult(
                ok=False,
                mode="demo",
                target_url=None,
                error_message=str(exc) or "Demo manifest failed validation.",
                error_type=ManifestValidationError.__name__,
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        nav_url = build_navigation_url(base_url, path)

        playwright = None
        browser = None
        context = None
        page = None
        page_video = None
        run_ok = False
        error_type: Optional[str] = None
        error_message: Optional[str] = None
        artifacts: dict[str, Path] = {}
        skipped: dict[str, str] = {}
        step_reports: list[dict[str, Any]] = []
        tracing_started = False

        video_tmp_dir: Optional[Path] = None
        if record_video:
            video_tmp_dir = out_dir / f".demo-video-tmp-{uuid.uuid4().hex}"
            video_tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            playwright = self._playwright_loader()
            browser = playwright.chromium.launch(headless=True)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": viewport.width, "height": viewport.height}
            }
            if video_tmp_dir is not None:
                context_kwargs["record_video_dir"] = str(video_tmp_dir)
            context = browser.new_context(**context_kwargs)

            if normalized_trace in {"on", "retain-on-failure"}:
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                tracing_started = True

            page = context.new_page()
            page_video = getattr(page, "video", None)
            try:
                page.goto(nav_url, timeout=timeout_ms, wait_until=wait_until)
            except Exception as exc:
                raise BrowserNavigationError(str(exc) or "Navigation failed.") from exc

            execution = execute_demo_manifest(
                page=page,
                manifest=manifest,
                base_url=base_url,
                initial_path=path,
                out_dir=out_dir,
                timeout_ms=timeout_ms,
            )
            artifacts.update(execution.artifacts)
            step_reports = [
                {
                    "index": step.index,
                    "action": step.action,
                    "ok": step.ok,
                    "error": step.error,
                    "artifacts": step.artifacts,
                }
                for step in execution.steps
            ]
            if not execution.ok:
                raise DemoStepError(
                    execution.error_message or "Demo step execution failed."
                )
            run_ok = True
        except Exception as exc:
            run_ok = False
            error_type = type(exc).__name__
            error_message = str(exc).strip() or repr(exc)
        finally:
            if context is not None and tracing_started:
                try:
                    keep_trace = normalized_trace == "on" or (
                        normalized_trace == "retain-on-failure" and not run_ok
                    )
                    if keep_trace:
                        trace_tmp = out_dir / f".demo-trace-tmp-{uuid.uuid4().hex}.zip"
                        context.tracing.stop(path=str(trace_tmp))
                        trace_artifact = self._move_file_artifact(
                            source=trace_tmp,
                            out_dir=out_dir,
                            kind="demo-trace",
                            default_extension="zip",
                            url=nav_url,
                            path_hint=path,
                            output_name=None,
                        )
                        artifacts["trace"] = trace_artifact
                    else:
                        context.tracing.stop()
                        skipped["trace"] = (
                            "Trace capture disabled on success (retain-on-failure)."
                        )
                except Exception as exc:
                    skipped["trace"] = f"Trace finalization failed: {exc}"

            if not run_ok and page is not None:
                try:
                    failure_name = deterministic_artifact_name(
                        kind="demo-failure-screenshot",
                        extension="png",
                        url=nav_url,
                        path_hint=path,
                    )
                    failure_path, _collision = reserve_artifact_path(
                        out_dir, failure_name
                    )
                    page.screenshot(path=str(failure_path), full_page=True)
                    artifacts["failure_screenshot"] = failure_path
                except Exception as exc:
                    skipped["failure_screenshot"] = (
                        f"Failure screenshot capture failed: {exc}"
                    )

            if page is not None:
                self._safe_close(page)
            if context is not None:
                self._safe_close(context)

            if page_video is not None:
                try:
                    video_path_raw = page_video.path()
                    if isinstance(video_path_raw, str) and video_path_raw:
                        source_video = Path(video_path_raw)
                        if source_video.exists():
                            video_artifact = self._move_file_artifact(
                                source=source_video,
                                out_dir=out_dir,
                                kind="demo-video",
                                default_extension="webm",
                                url=nav_url,
                                path_hint=path,
                                output_name=None,
                            )
                            artifacts["video"] = video_artifact
                except Exception as exc:
                    skipped["video"] = f"Video finalization failed: {exc}"

            if browser is not None:
                self._safe_close(browser)
            self._safe_stop(playwright)

            if video_tmp_dir is not None and video_tmp_dir.exists():
                try:
                    shutil.rmtree(video_tmp_dir, ignore_errors=True)
                except Exception:
                    pass

            summary_payload = {
                "status": "ok" if run_ok else "failed",
                "manifest_version": manifest.version,
                "script_path": str(script_path),
                "target_url": nav_url,
                "steps": step_reports,
                "artifacts": {
                    name: str(path_value) for name, path_value in artifacts.items()
                },
                "skipped": skipped,
                "error": error_message,
            }
            try:
                summary_name = deterministic_artifact_name(
                    kind="demo-summary",
                    extension="json",
                    url=nav_url,
                    path_hint=path,
                    output_name=output_name,
                )
                summary_result = write_json_artifact(
                    out_dir=out_dir,
                    filename=summary_name,
                    payload=summary_payload,
                )
                artifacts["summary"] = summary_result.path
            except Exception as exc:
                if run_ok:
                    run_ok = False
                    error_type = BrowserArtifactError.__name__
                    error_message = f"Failed to write demo summary artifact: {exc}"
                skipped["summary"] = f"Summary write failed: {exc}"

        return BrowserRunResult(
            ok=run_ok,
            mode="demo",
            target_url=nav_url,
            artifacts=artifacts,
            skipped=skipped,
            error_message=error_message,
            error_type=error_type,
        )

    def _run_page_action(
        self,
        *,
        mode: str,
        nav_url: str,
        viewport: Viewport,
        timeout_ms: int,
        wait_until: str,
        action: Callable[[Any], tuple[dict[str, Path], dict[str, str]]],
    ) -> BrowserRunResult:
        playwright = None
        browser = None
        context = None
        page = None
        try:
            playwright = self._playwright_loader()
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": viewport.width, "height": viewport.height}
            )
            page = context.new_page()
            try:
                page.goto(nav_url, timeout=timeout_ms, wait_until=wait_until)
            except Exception as exc:
                raise BrowserNavigationError(str(exc) or "Navigation failed.") from exc
            try:
                artifacts, skipped = action(page)
            except Exception as exc:
                raise BrowserArtifactError(
                    str(exc) or "Artifact capture failed."
                ) from exc
            return BrowserRunResult(
                ok=True,
                mode=mode,
                target_url=nav_url,
                artifacts=artifacts,
                skipped=skipped,
            )
        except Exception as exc:
            message = str(exc).strip() or repr(exc)
            return BrowserRunResult(
                ok=False,
                mode=mode,
                target_url=nav_url,
                error_message=message,
                error_type=type(exc).__name__,
            )
        finally:
            for resource in (page, context, browser):
                self._safe_close(resource)
            self._safe_stop(playwright)

    @staticmethod
    def _safe_close(resource: Any) -> None:
        if resource is None or not hasattr(resource, "close"):
            return
        try:
            resource.close()
        except Exception:
            return

    @staticmethod
    def _safe_stop(playwright: Any) -> None:
        if playwright is None or not hasattr(playwright, "stop"):
            return
        try:
            playwright.stop()
        except Exception:
            return

    @staticmethod
    def _move_file_artifact(
        *,
        source: Path,
        out_dir: Path,
        kind: str,
        default_extension: str,
        url: str,
        path_hint: str,
        output_name: Optional[str],
    ) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        extension = source.suffix.lstrip(".") or default_extension
        filename = deterministic_artifact_name(
            kind=kind,
            extension=extension,
            url=url,
            path_hint=path_hint,
            output_name=output_name,
        )
        target, _collision = reserve_artifact_path(out_dir, filename)
        shutil.move(str(source), str(target))
        return target
