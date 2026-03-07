from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .actions import (
    DemoPreflightResult,
    DemoPreflightStepReport,
    execute_demo_manifest,
    load_demo_manifest,
    step_requires_locator_preflight,
)
from .artifacts import (
    deterministic_artifact_name,
    reserve_artifact_path,
    write_json_artifact,
)
from .models import DEFAULT_VIEWPORT, Viewport
from .primitives import (
    capture_artifact,
    describe_step_locator,
    observe_page,
    resolve_step_locator,
)

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


@dataclass(frozen=True)
class BrowserPreflightResult:
    ok: bool
    mode: str
    target_url: Optional[str]
    diagnostics: DemoPreflightResult
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


def build_navigation_url(base_url: str, path: Optional[str] = None) -> str:
    if not base_url or not base_url.strip():
        raise ValueError("A non-empty base URL is required.")
    normalized_base = base_url.strip()
    base = urlsplit(normalized_base)
    if path is None:
        return urlunsplit(
            (
                base.scheme,
                base.netloc,
                base.path or "/",
                base.query,
                base.fragment,
            )
        )
    normalized_path = path.strip() or "/"
    if not normalized_path:
        return urlunsplit(
            (
                base.scheme,
                base.netloc,
                base.path or "/",
                base.query,
                base.fragment,
            )
        )
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

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
        path: Optional[str] = None,
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

        def _action(page: Any) -> tuple[dict[str, Path], dict[str, str]]:
            artifact_path = capture_artifact(
                out_dir=out_dir,
                kind="screenshot",
                extension=fmt,
                url=nav_url,
                path_hint=path,
                output_name=output_name,
                writer=(
                    (lambda p: page.pdf(path=str(p)))
                    if fmt == "pdf"
                    else (lambda p: page.screenshot(path=str(p), full_page=full_page))
                ),
            )
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
        path: Optional[str] = None,
        out_dir: Path,
        viewport: Viewport = DEFAULT_VIEWPORT,
        output_name: Optional[str] = None,
        include_html: bool = True,
        max_html_bytes: int = 250_000,
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
    ) -> BrowserRunResult:
        nav_url = build_navigation_url(base_url, path)

        def _action(page: Any) -> tuple[dict[str, Path], dict[str, str]]:
            observation = observe_page(
                page=page,
                out_dir=out_dir,
                target_url=nav_url,
                path_hint=(path or urlsplit(nav_url).path or "/"),
                viewport=viewport,
                output_name=output_name,
                include_html=include_html,
                max_html_bytes=max_html_bytes,
            )
            return observation.artifacts, observation.skipped

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
        path: Optional[str] = None,
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
                initial_path=(path or urlsplit(nav_url).path or "/"),
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
                            path_hint=(path or urlsplit(nav_url).path or "/"),
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
                    failure_path = capture_artifact(
                        out_dir=out_dir,
                        kind="demo-failure-screenshot",
                        extension="png",
                        url=nav_url,
                        path_hint=path,
                        writer=lambda p: page.screenshot(path=str(p), full_page=True),
                    )
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
                                path_hint=(path or urlsplit(nav_url).path or "/"),
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

    def preflight_demo(
        self,
        *,
        base_url: str,
        path: Optional[str] = None,
        script_path: Path,
        viewport: Viewport = DEFAULT_VIEWPORT,
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
    ) -> BrowserPreflightResult:
        try:
            manifest = load_demo_manifest(script_path)
        except Exception as exc:
            message = str(exc) or "Demo manifest failed validation."
            return BrowserPreflightResult(
                ok=False,
                mode="demo_preflight",
                target_url=None,
                diagnostics=DemoPreflightResult(
                    ok=False,
                    steps=[],
                    error_message=message,
                ),
                error_message=message,
                error_type=ManifestValidationError.__name__,
            )

        nav_url = build_navigation_url(base_url, path)
        initial_path = path or urlsplit(nav_url).path or "/"

        playwright = None
        browser = None
        context = None
        page = None
        reports: list[DemoPreflightStepReport] = []
        fatal_error: Optional[str] = None
        try:
            playwright = self._playwright_loader()
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": viewport.width, "height": viewport.height}
            )
            page = context.new_page()
            page.goto(nav_url, timeout=timeout_ms, wait_until=wait_until)
        except Exception as exc:
            fatal_error = str(exc).strip() or repr(exc)
        finally:
            if fatal_error is not None:
                for resource in (page, context, browser):
                    self._safe_close(resource)
                self._safe_stop(playwright)
        if fatal_error is not None:
            diagnostics = DemoPreflightResult(
                ok=False,
                steps=[],
                error_message=f"Failed to open preflight target: {fatal_error}",
            )
            return BrowserPreflightResult(
                ok=False,
                mode="demo_preflight",
                target_url=nav_url,
                diagnostics=diagnostics,
                error_message=diagnostics.error_message,
                error_type=BrowserNavigationError.__name__,
            )

        assert page is not None
        run_ok = True
        for idx, step in enumerate(manifest.steps, start=1):
            step_timeout = _step_timeout_value(step.data, timeout_ms)
            try:
                if step.action == "goto":
                    target = _resolve_step_url_for_demo(
                        base_url=base_url,
                        raw_url=str(step.data.get("url") or ""),
                        initial_path=initial_path,
                    )
                    step_wait = step.data.get("wait_until")
                    wait_value = (
                        step_wait.strip()
                        if isinstance(step_wait, str) and step_wait.strip()
                        else wait_until
                    )
                    page.goto(target, timeout=step_timeout, wait_until=wait_value)
                    reports.append(
                        DemoPreflightStepReport(
                            index=idx,
                            action=step.action,
                            ok=True,
                            detail=f"goto reachable: {target}",
                        )
                    )
                    continue

                if step_requires_locator_preflight(step):
                    locator = resolve_step_locator(page, step.data)
                    _assert_locator_actionable(
                        locator,
                        action=step.action,
                        timeout_ms=step_timeout,
                    )
                    reports.append(
                        DemoPreflightStepReport(
                            index=idx,
                            action=step.action,
                            ok=True,
                            detail=(
                                "locator is actionable: "
                                f"{describe_step_locator(step.data)}"
                            ),
                        )
                    )
                    continue

                if step.action == "press":
                    reports.append(
                        DemoPreflightStepReport(
                            index=idx,
                            action=step.action,
                            ok=True,
                            detail="keyboard press uses no locator; skipped.",
                        )
                    )
                    continue

                reports.append(
                    DemoPreflightStepReport(
                        index=idx,
                        action=step.action,
                        ok=True,
                        detail="no locator assumptions to validate.",
                    )
                )
            except Exception as exc:
                run_ok = False
                detail = str(exc).strip() or repr(exc)
                reports.append(
                    DemoPreflightStepReport(
                        index=idx,
                        action=step.action,
                        ok=False,
                        detail=detail,
                    )
                )

        failed = [report for report in reports if not report.ok]
        diagnostics_message: Optional[str] = None
        if failed:
            parts = [
                f"step {report.index} ({report.action}): {report.detail}"
                for report in failed
            ]
            diagnostics_message = (
                f"Preflight failed for {len(failed)} step(s): " + "; ".join(parts)
            )
        diagnostics = DemoPreflightResult(
            ok=run_ok,
            steps=reports,
            error_message=diagnostics_message,
        )
        result = BrowserPreflightResult(
            ok=run_ok,
            mode="demo_preflight",
            target_url=nav_url,
            diagnostics=diagnostics,
            error_message=diagnostics_message,
            error_type=None if run_ok else DemoStepError.__name__,
        )

        for resource in (page, context, browser):
            self._safe_close(resource)
        self._safe_stop(playwright)
        return result

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


def _step_timeout_value(step: Dict[str, Any], default_timeout_ms: int) -> int:
    raw = step.get("timeout_ms")
    if raw is None:
        return default_timeout_ms
    if not isinstance(raw, int) or raw <= 0:
        raise ValueError("timeout_ms must be a positive integer.")
    return raw


def _assert_locator_actionable(locator: Any, *, action: str, timeout_ms: int) -> None:
    probe = locator
    first = getattr(locator, "first", None)
    if first is not None:
        probe = first
    wait_state = (
        "visible"
        if action in {"click", "fill", "wait_for_text", "press"}
        else "attached"
    )
    if hasattr(probe, "wait_for"):
        probe.wait_for(state=wait_state, timeout=timeout_ms)
        return
    if hasattr(locator, "count"):
        count = int(locator.count())
        if count <= 0:
            raise ValueError("Locator resolved to zero elements.")
        return
    raise ValueError(
        "Unable to verify locator; locator API does not support wait_for/count."
    )


def _resolve_step_url_for_demo(
    *, base_url: str, raw_url: str, initial_path: str
) -> str:
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    if raw_url.startswith("/"):
        return build_navigation_url(base_url, raw_url)
    if raw_url.strip() == "":
        return build_navigation_url(base_url, initial_path)
    return build_navigation_url(base_url, f"/{raw_url}")
