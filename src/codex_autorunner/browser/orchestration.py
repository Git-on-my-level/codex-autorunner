from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, Sequence

import yaml

from ..core.utils import atomic_write
from .artifacts import reserve_artifact_path
from .models import DEFAULT_VIEWPORT_TEXT, Viewport, parse_viewport
from .runtime import BrowserRunResult, BrowserRuntime
from .server import BrowserServeConfig, BrowserServerSupervisor, BrowserServeSession

_TRACE_VALUES = {"off", "on", "retain-on-failure"}


class DemoWorkflowError(RuntimeError):
    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class DemoWorkflowConfigError(DemoWorkflowError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="workflow_config")


class DemoWorkflowExecutionError(DemoWorkflowError):
    pass


@dataclass(frozen=True)
class WorkflowServiceConfig:
    name: str
    serve_cmd: str
    ready_url: Optional[str]
    ready_log_pattern: Optional[str]
    cwd: Optional[Path]
    env: dict[str, str]
    ready_timeout_seconds: float


@dataclass(frozen=True)
class WorkflowDemoCaptureConfig:
    script_path: Path
    path: Optional[str]
    viewport: Viewport
    record_video: bool
    trace_mode: str
    out_dir: Path
    output_name: Optional[str]
    service: Optional[str] = None
    base_url: Optional[str] = None


@dataclass(frozen=True)
class WorkflowExportConfig:
    manifest_name: str
    primary_artifact: Optional[str]


@dataclass(frozen=True)
class WorkflowPublishConfig:
    enabled: bool
    outbox_dir: Path
    include: tuple[str, ...]


@dataclass(frozen=True)
class DemoWorkflowConfig:
    workflow_path: Path
    services: tuple[WorkflowServiceConfig, ...]
    demo: WorkflowDemoCaptureConfig
    export: WorkflowExportConfig
    publish: WorkflowPublishConfig


@dataclass(frozen=True)
class WorkflowServiceSession:
    name: str
    pid: int
    pgid: Optional[int]
    ready_source: str
    target_url: Optional[str]
    ready_url: Optional[str]


@dataclass(frozen=True)
class DemoWorkflowRunResult:
    workflow_path: Path
    target_base_url: str
    output_dir: Path
    service_sessions: tuple[WorkflowServiceSession, ...]
    capture_result: BrowserRunResult
    primary_artifact_key: str
    primary_artifact_path: Path
    export_manifest_path: Path
    published_artifacts: tuple[Path, ...]


class _Supervisor(Protocol):
    def start(self) -> None: ...

    def wait_until_ready(self) -> BrowserServeSession: ...

    def stop(self) -> None: ...


SupervisorFactory = Callable[[BrowserServeConfig], _Supervisor]


@contextmanager
def orchestrate_workflow_services(
    services: Sequence[WorkflowServiceConfig],
    *,
    supervisor_factory: SupervisorFactory = BrowserServerSupervisor,
) -> Iterator[tuple[WorkflowServiceSession, ...]]:
    supervisors: list[tuple[str, _Supervisor]] = []
    sessions: list[WorkflowServiceSession] = []
    try:
        for service in services:
            serve_config = BrowserServeConfig(
                serve_cmd=service.serve_cmd,
                ready_url=service.ready_url,
                ready_log_pattern=service.ready_log_pattern,
                cwd=service.cwd,
                env_overrides=service.env,
                timeout_seconds=service.ready_timeout_seconds,
            )
            supervisor = supervisor_factory(serve_config)
            supervisors.append((service.name, supervisor))
            try:
                supervisor.start()
                session = supervisor.wait_until_ready()
            except DemoWorkflowError:
                raise
            except (
                Exception
            ) as exc:  # intentional: wrap unpredictable supervisor startup errors
                raise DemoWorkflowExecutionError(
                    (
                        f"Service '{service.name}' failed readiness/startup: "
                        f"{str(exc) or 'unknown service error'}"
                    ),
                    category="service_readiness",
                ) from exc

            sessions.append(
                WorkflowServiceSession(
                    name=service.name,
                    pid=session.pid,
                    pgid=session.pgid,
                    ready_source=session.ready_source,
                    target_url=session.target_url,
                    ready_url=session.ready_url,
                )
            )
        yield tuple(sessions)
    finally:
        _stop_supervisors(supervisors)


def load_demo_workflow_config(
    *,
    workflow_path: Path,
    repo_root: Path,
    out_dir_override: Optional[Path] = None,
    outbox_dir_override: Optional[Path] = None,
    publish_enabled_override: Optional[bool] = None,
    # Backward-compatibility for old CLI naming.
    outbox_path_override: Optional[Path] = None,
    publish_outbox_override: Optional[bool] = None,
) -> DemoWorkflowConfig:
    resolved_workflow_path = workflow_path.expanduser().resolve()
    if not resolved_workflow_path.exists() or not resolved_workflow_path.is_file():
        raise DemoWorkflowConfigError(
            f"Workflow config file was not found: {resolved_workflow_path}"
        )

    try:
        raw_text = resolved_workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DemoWorkflowConfigError(f"Unable to read workflow config: {exc}") from exc

    try:
        if resolved_workflow_path.suffix.lower() == ".json":
            payload = json.loads(raw_text)
        else:
            payload = yaml.safe_load(raw_text)
    except (ValueError, yaml.YAMLError) as exc:
        raise DemoWorkflowConfigError(
            f"Unable to parse workflow config: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise DemoWorkflowConfigError("Workflow config root must be a mapping.")

    workflow_dir = resolved_workflow_path.parent
    resolved_repo_root = repo_root.expanduser().resolve()
    effective_outbox_override = outbox_dir_override or outbox_path_override
    if publish_enabled_override is None:
        effective_publish_override = publish_outbox_override
    else:
        effective_publish_override = publish_enabled_override

    services = _parse_services(payload=payload, workflow_dir=workflow_dir)
    service_names = {service.name for service in services}
    demo = _parse_demo_capture(
        payload=payload,
        workflow_dir=workflow_dir,
        repo_root=resolved_repo_root,
        service_names=service_names,
        out_dir_override=out_dir_override,
    )
    export = _parse_export(payload=payload)
    publish = _parse_publish(
        payload=payload,
        repo_root=resolved_repo_root,
        outbox_dir_override=effective_outbox_override,
        publish_enabled_override=effective_publish_override,
    )

    return DemoWorkflowConfig(
        workflow_path=resolved_workflow_path,
        services=tuple(services),
        demo=demo,
        export=export,
        publish=publish,
    )


def run_demo_workflow(
    config: DemoWorkflowConfig,
    *,
    runtime: Optional[BrowserRuntime] = None,
    supervisor_factory: SupervisorFactory = BrowserServerSupervisor,
) -> DemoWorkflowRunResult:
    runtime_impl = runtime or BrowserRuntime()
    with orchestrate_workflow_services(
        config.services,
        supervisor_factory=supervisor_factory,
    ) as service_sessions:
        sessions_by_name = {session.name: session for session in service_sessions}
        base_url = _resolve_target_base_url(
            demo_config=config.demo,
            services=config.services,
            sessions=sessions_by_name,
        )
        capture_result = runtime_impl.capture_demo(
            base_url=base_url,
            path=config.demo.path,
            script_path=config.demo.script_path,
            out_dir=config.demo.out_dir,
            viewport=config.demo.viewport,
            record_video=config.demo.record_video,
            trace_mode=config.demo.trace_mode,
            output_name=config.demo.output_name,
        )
        if not capture_result.ok:
            raise DemoWorkflowExecutionError(
                (
                    f"Demo capture failed ({_runtime_error_category(capture_result.error_type)}): "
                    f"{capture_result.error_message or 'Unknown capture error.'}"
                ),
                category="capture_failure",
            )

        primary_key, primary_path = _select_primary_artifact(
            artifacts=capture_result.artifacts,
            preferred_key=config.export.primary_artifact,
        )
        manifest_path = _write_export_manifest(
            config=config,
            target_base_url=base_url,
            sessions=service_sessions,
            capture_result=capture_result,
            primary_artifact_key=primary_key,
            primary_artifact_path=primary_path,
            published=(),
        )

        artifact_catalog: dict[str, Path] = {
            **capture_result.artifacts,
            "manifest": manifest_path,
        }
        published_artifacts: tuple[Path, ...] = ()
        if config.publish.enabled:
            publish_keys = _resolve_publish_keys(
                include=config.publish.include,
                primary_key=primary_key,
            )
            published_artifacts = tuple(
                _publish_artifacts(
                    artifacts=artifact_catalog,
                    outbox_dir=config.publish.outbox_dir,
                    selected=publish_keys,
                )
            )
            manifest_path = _write_export_manifest(
                config=config,
                target_base_url=base_url,
                sessions=service_sessions,
                capture_result=capture_result,
                primary_artifact_key=primary_key,
                primary_artifact_path=primary_path,
                published=published_artifacts,
            )
            for manifest_publish_index, key in enumerate(publish_keys):
                if key != "manifest":
                    continue
                published_manifest = published_artifacts[manifest_publish_index]
                if _same_path(manifest_path, published_manifest):
                    continue
                try:
                    shutil.copy2(manifest_path, published_manifest)
                except OSError as exc:
                    raise DemoWorkflowExecutionError(
                        f"Failed publishing artifact 'manifest' to outbox: {exc}",
                        category="publish_failure",
                    ) from exc

        return DemoWorkflowRunResult(
            workflow_path=config.workflow_path,
            target_base_url=base_url,
            output_dir=config.demo.out_dir,
            service_sessions=service_sessions,
            capture_result=capture_result,
            primary_artifact_key=primary_key,
            primary_artifact_path=primary_path,
            export_manifest_path=manifest_path,
            published_artifacts=published_artifacts,
        )


def _parse_services(
    *,
    payload: Mapping[str, Any],
    workflow_dir: Path,
) -> list[WorkflowServiceConfig]:
    raw_services = payload.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise DemoWorkflowConfigError(
            "Workflow config must include a non-empty services list."
        )

    services: list[WorkflowServiceConfig] = []
    for index, raw_service in enumerate(raw_services, start=1):
        if not isinstance(raw_service, dict):
            raise DemoWorkflowConfigError(f"services[{index}] must be a mapping.")

        raw_name = raw_service.get("name")
        if raw_name is None:
            name = f"service-{index}"
        else:
            name = _require_non_empty_str(raw_name, field=f"services[{index}].name")

        raw_serve_cmd = raw_service.get(
            "serve_cmd",
            raw_service.get("cmd", raw_service.get("command")),
        )
        serve_cmd = _require_non_empty_str(
            raw_serve_cmd,
            field=f"services[{index}].serve_cmd",
        )

        ready_url = _optional_non_empty_str(
            raw_service.get("ready_url"),
            field=f"services[{index}].ready_url",
        )
        ready_log_pattern = _optional_non_empty_str(
            raw_service.get("ready_log_pattern"),
            field=f"services[{index}].ready_log_pattern",
        )
        if ready_url is None and ready_log_pattern is None:
            raise DemoWorkflowConfigError(
                (
                    f"services[{index}] requires readiness config: "
                    "set ready_url or ready_log_pattern."
                )
            )

        cwd_raw = raw_service.get("cwd")
        cwd: Optional[Path] = None
        if cwd_raw is not None:
            cwd_value = _require_non_empty_str(cwd_raw, field=f"services[{index}].cwd")
            cwd = _resolve_path(cwd_value, base_dir=workflow_dir)

        env = _parse_env_block(raw_service.get("env"), field=f"services[{index}].env")
        timeout = _parse_positive_float(
            raw_service.get("ready_timeout_seconds"),
            field=f"services[{index}].ready_timeout_seconds",
            default=30.0,
        )

        services.append(
            WorkflowServiceConfig(
                name=name,
                serve_cmd=serve_cmd,
                ready_url=ready_url,
                ready_log_pattern=ready_log_pattern,
                cwd=cwd,
                env=env,
                ready_timeout_seconds=timeout,
            )
        )

    names = [service.name for service in services]
    if len(names) != len(set(names)):
        raise DemoWorkflowConfigError("Service names must be unique.")
    return services


def _parse_demo_capture(
    *,
    payload: Mapping[str, Any],
    workflow_dir: Path,
    repo_root: Path,
    service_names: set[str],
    out_dir_override: Optional[Path],
) -> WorkflowDemoCaptureConfig:
    raw_demo = payload.get("demo")
    if not isinstance(raw_demo, dict):
        raise DemoWorkflowConfigError("Workflow config must include a demo mapping.")

    script_value = raw_demo.get("script", raw_demo.get("script_path"))
    script_raw = _require_non_empty_str(script_value, field="demo.script")
    script_path = _resolve_path(script_raw, base_dir=workflow_dir)
    if not script_path.exists() or not script_path.is_file():
        raise DemoWorkflowConfigError(f"Demo script was not found: {script_path}")

    path = _optional_non_empty_str(raw_demo.get("path"), field="demo.path")
    viewport_raw = _optional_non_empty_str(
        raw_demo.get("viewport"), field="demo.viewport"
    )
    viewport_value = viewport_raw or DEFAULT_VIEWPORT_TEXT
    try:
        viewport = parse_viewport(viewport_value)
    except ValueError as exc:
        raise DemoWorkflowConfigError(str(exc)) from exc

    record_video = _parse_bool(
        raw_demo.get("record_video"),
        field="demo.record_video",
        default=False,
    )
    trace_mode = (
        _optional_non_empty_str(raw_demo.get("trace"), field="demo.trace") or "off"
    ).lower()
    if trace_mode not in _TRACE_VALUES:
        raise DemoWorkflowConfigError(
            "demo.trace must be one of: off, on, retain-on-failure."
        )

    out_dir = (
        _resolve_repo_path(out_dir_override, repo_root=repo_root)
        if out_dir_override is not None
        else _resolve_repo_path(
            _optional_path(raw_demo.get("out_dir"), field="demo.out_dir"),
            repo_root=repo_root,
            default=repo_root / ".codex-autorunner" / "render" / "workflow_runs",
        )
    )
    out_dir, output_name = _resolve_output_override(
        base_out_dir=out_dir,
        output_value=_optional_non_empty_str(
            raw_demo.get("output"), field="demo.output"
        ),
    )

    service = _optional_non_empty_str(raw_demo.get("service"), field="demo.service")
    if service is not None and service not in service_names:
        raise DemoWorkflowConfigError(
            f"demo.service references unknown service '{service}'."
        )
    base_url = _optional_non_empty_str(raw_demo.get("base_url"), field="demo.base_url")

    return WorkflowDemoCaptureConfig(
        script_path=script_path,
        path=path,
        viewport=viewport,
        record_video=record_video,
        trace_mode=trace_mode,
        out_dir=out_dir,
        output_name=output_name,
        service=service,
        base_url=base_url,
    )


def _parse_export(*, payload: Mapping[str, Any]) -> WorkflowExportConfig:
    raw_export = payload.get("export")
    if raw_export is None:
        export_map: Mapping[str, Any] = {}
    elif isinstance(raw_export, dict):
        export_map = raw_export
    else:
        raise DemoWorkflowConfigError("export must be a mapping when provided.")

    manifest_name = _optional_non_empty_str(
        export_map.get("manifest_name"),
        field="export.manifest_name",
    )
    if manifest_name is None:
        manifest_name = "demo-workflow-export-manifest.json"
    if Path(manifest_name).name != manifest_name:
        raise DemoWorkflowConfigError(
            "export.manifest_name must be a filename, not a path."
        )

    primary_artifact = _optional_non_empty_str(
        export_map.get("primary_artifact"),
        field="export.primary_artifact",
    )

    return WorkflowExportConfig(
        manifest_name=manifest_name,
        primary_artifact=primary_artifact,
    )


def _parse_publish(
    *,
    payload: Mapping[str, Any],
    repo_root: Path,
    outbox_dir_override: Optional[Path],
    publish_enabled_override: Optional[bool],
) -> WorkflowPublishConfig:
    raw_publish = payload.get("publish")
    legacy_outbox = payload.get("outbox")

    publish_field = "enabled"
    outbox_field = "outbox_dir"
    include_field = "include"
    if raw_publish is None and legacy_outbox is not None:
        raw_publish = legacy_outbox
        publish_field = "publish"
        outbox_field = "path"
        include_field = "artifacts"

    if raw_publish is None:
        publish_map: Mapping[str, Any] = {}
    elif isinstance(raw_publish, dict):
        publish_map = raw_publish
    else:
        raise DemoWorkflowConfigError("publish must be a mapping when provided.")

    publish_default = _parse_bool(
        publish_map.get(publish_field),
        field=f"publish.{publish_field}",
        default=False,
    )
    enabled = (
        publish_enabled_override
        if publish_enabled_override is not None
        else publish_default
    )

    outbox_dir = (
        _resolve_repo_path(outbox_dir_override, repo_root=repo_root)
        if outbox_dir_override is not None
        else _resolve_repo_path(
            _optional_path(
                publish_map.get(outbox_field),
                field=f"publish.{outbox_field}",
            ),
            repo_root=repo_root,
            default=repo_root / ".codex-autorunner" / "filebox" / "outbox",
        )
    )

    raw_include = publish_map.get(include_field)
    if raw_include is None:
        include: tuple[str, ...] = ()
    elif isinstance(raw_include, list) and raw_include:
        normalized: list[str] = []
        for idx, item in enumerate(raw_include, start=1):
            value = _require_non_empty_str(
                item, field=f"publish.{include_field}[{idx}]"
            )
            if value not in normalized:
                normalized.append(value)
        include = tuple(normalized)
    else:
        raise DemoWorkflowConfigError(
            f"publish.{include_field} must be a non-empty list when provided."
        )

    return WorkflowPublishConfig(
        enabled=enabled,
        outbox_dir=outbox_dir,
        include=include,
    )


def _resolve_target_base_url(
    *,
    demo_config: WorkflowDemoCaptureConfig,
    services: Sequence[WorkflowServiceConfig],
    sessions: Mapping[str, WorkflowServiceSession],
) -> str:
    if demo_config.base_url is not None:
        return demo_config.base_url

    if demo_config.service is not None:
        session = sessions.get(demo_config.service)
        if session is None or not session.target_url:
            raise DemoWorkflowExecutionError(
                (
                    "demo.service could not resolve a target URL. "
                    "Set demo.base_url or ensure the selected service has ready_url "
                    "or ready_log_pattern with a URL capture group."
                ),
                category="target_resolution",
            )
        return session.target_url

    for service in reversed(services):
        session = sessions.get(service.name)
        if session and session.target_url:
            return session.target_url

    raise DemoWorkflowExecutionError(
        (
            "Could not resolve target base URL from workflow services. "
            "Set demo.base_url or demo.service explicitly."
        ),
        category="target_resolution",
    )


def _select_primary_artifact(
    *,
    artifacts: Mapping[str, Path],
    preferred_key: Optional[str],
) -> tuple[str, Path]:
    if not artifacts:
        raise DemoWorkflowExecutionError(
            "Demo workflow captured no artifacts.",
            category="export_failure",
        )

    if preferred_key is not None:
        preferred = artifacts.get(preferred_key)
        if preferred is None:
            raise DemoWorkflowExecutionError(
                (
                    "export.primary_artifact references unknown artifact key "
                    f"'{preferred_key}'."
                ),
                category="export_failure",
            )
        return preferred_key, preferred

    if "video" in artifacts:
        return "video", artifacts["video"]

    first_key = sorted(artifacts)[0]
    return first_key, artifacts[first_key]


def _write_export_manifest(
    *,
    config: DemoWorkflowConfig,
    target_base_url: str,
    sessions: Sequence[WorkflowServiceSession],
    capture_result: BrowserRunResult,
    primary_artifact_key: str,
    primary_artifact_path: Path,
    published: Sequence[Path],
) -> Path:
    manifest_path = (config.demo.out_dir / config.export.manifest_name).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "workflow_path": str(config.workflow_path),
        "target_base_url": target_base_url,
        "services": [
            {
                "name": session.name,
                "pid": session.pid,
                "pgid": session.pgid,
                "ready_source": session.ready_source,
                "target_url": session.target_url,
                "ready_url": session.ready_url,
            }
            for session in sessions
        ],
        "demo": {
            "script": str(config.demo.script_path),
            "path": config.demo.path,
            "viewport": {
                "width": config.demo.viewport.width,
                "height": config.demo.viewport.height,
            },
            "record_video": config.demo.record_video,
            "trace": config.demo.trace_mode,
            "out_dir": str(config.demo.out_dir),
            "output_name": config.demo.output_name,
            "service": config.demo.service,
            "base_url": config.demo.base_url,
        },
        "capture": {
            "ok": capture_result.ok,
            "mode": capture_result.mode,
            "target_url": capture_result.target_url,
            "artifacts": {
                key: str(value)
                for key, value in sorted(capture_result.artifacts.items())
            },
            "skipped": dict(sorted(capture_result.skipped.items())),
            "error_type": capture_result.error_type,
            "error_message": capture_result.error_message,
        },
        "export": {
            "manifest_name": config.export.manifest_name,
            "manifest_path": str(manifest_path),
            "primary_artifact_key": primary_artifact_key,
            "primary_artifact_path": str(primary_artifact_path),
        },
        "publish": {
            "enabled": config.publish.enabled,
            "outbox_dir": str(config.publish.outbox_dir),
            "include": list(config.publish.include),
            "published": [str(path) for path in published],
        },
    }
    try:
        atomic_write(
            manifest_path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
    except OSError as exc:
        raise DemoWorkflowExecutionError(
            f"Failed to write workflow export manifest: {exc}",
            category="export_failure",
        ) from exc
    return manifest_path


def _resolve_publish_keys(
    *, include: Sequence[str], primary_key: str
) -> tuple[str, ...]:
    if include:
        return tuple(include)

    ordered: list[str] = []
    for key in (primary_key, "manifest"):
        if key not in ordered:
            ordered.append(key)
    return tuple(ordered)


def _publish_artifacts(
    *,
    artifacts: Mapping[str, Path],
    outbox_dir: Path,
    selected: Sequence[str],
) -> list[Path]:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []

    for key in selected:
        source = artifacts.get(key)
        if source is None:
            raise DemoWorkflowExecutionError(
                f"publish.include contains unknown artifact key '{key}'.",
                category="publish_failure",
            )
        if not source.exists():
            raise DemoWorkflowExecutionError(
                f"Artifact '{key}' does not exist on disk: {source}",
                category="publish_failure",
            )

        candidate = (outbox_dir / source.name).resolve()
        if _same_path(source, candidate):
            published.append(candidate)
            continue

        destination = candidate
        if destination.exists() and not _same_path(destination, source):
            destination, _collision_index = reserve_artifact_path(
                outbox_dir, source.name
            )

        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            raise DemoWorkflowExecutionError(
                f"Failed publishing artifact '{key}' to outbox: {exc}",
                category="publish_failure",
            ) from exc
        published.append(destination)

    return published


def _stop_supervisors(supervisors: Sequence[tuple[str, _Supervisor]]) -> None:
    for _name, supervisor in reversed(supervisors):
        try:
            supervisor.stop()
        except Exception:  # intentional: cleanup must not raise
            continue


def _resolve_output_override(
    *,
    base_out_dir: Path,
    output_value: Optional[str],
) -> tuple[Path, Optional[str]]:
    if output_value is None:
        return base_out_dir, None

    output = Path(output_value)
    if output.is_absolute():
        resolved = output.expanduser().resolve()
        return resolved.parent, resolved.name

    if output.parent != Path("."):
        return (base_out_dir / output.parent).resolve(), output.name
    return base_out_dir, output.name


def _resolve_repo_path(
    path: Optional[Path],
    *,
    repo_root: Path,
    default: Optional[Path] = None,
) -> Path:
    candidate = path or default
    if candidate is None:
        raise DemoWorkflowConfigError("Internal error: missing path value.")
    try:
        candidate = candidate.expanduser()
    except RuntimeError as exc:
        raise DemoWorkflowConfigError(
            f"Unable to expand path '{candidate}': {exc}"
        ) from exc
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _resolve_path(value: str, *, base_dir: Path) -> Path:
    candidate = Path(value)
    try:
        candidate = candidate.expanduser()
    except RuntimeError as exc:
        raise DemoWorkflowConfigError(
            f"Unable to expand path '{value}': {exc}"
        ) from exc
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _parse_env_block(raw: Any, *, field: str) -> dict[str, str]:
    if raw is None:
        return {}

    if isinstance(raw, list):
        parsed: dict[str, str] = {}
        for idx, item in enumerate(raw, start=1):
            entry = _require_non_empty_str(item, field=f"{field}[{idx}]")
            key, sep, value = entry.partition("=")
            if sep != "=" or not key.strip():
                raise DemoWorkflowConfigError(
                    f"{field}[{idx}] must use KEY=VALUE format."
                )
            parsed[key.strip()] = value
        return parsed

    if isinstance(raw, dict):
        parsed_map: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise DemoWorkflowConfigError(
                    f"{field} keys must be non-empty strings."
                )
            parsed_map[key.strip()] = str(value)
        return parsed_map

    raise DemoWorkflowConfigError(
        f"{field} must be a list of KEY=VALUE entries or a mapping."
    )


def _optional_non_empty_str(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DemoWorkflowConfigError(f"{field} must be a string when provided.")
    normalized = value.strip()
    return normalized if normalized else None


def _require_non_empty_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DemoWorkflowConfigError(f"{field} must be a non-empty string.")
    return value.strip()


def _parse_positive_float(value: Any, *, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DemoWorkflowConfigError(f"{field} must be a positive number.")
    parsed = float(value)
    if parsed <= 0:
        raise DemoWorkflowConfigError(f"{field} must be greater than zero.")
    return parsed


def _parse_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DemoWorkflowConfigError(f"{field} must be a boolean value.")
    return value


def _optional_path(value: Any, *, field: str) -> Optional[Path]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DemoWorkflowConfigError(f"{field} must be a non-empty path string.")
    return Path(value.strip())


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except (OSError, RuntimeError):
        return False


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


__all__ = [
    "DemoWorkflowConfig",
    "DemoWorkflowConfigError",
    "DemoWorkflowError",
    "DemoWorkflowExecutionError",
    "DemoWorkflowRunResult",
    "WorkflowDemoCaptureConfig",
    "WorkflowExportConfig",
    "WorkflowPublishConfig",
    "WorkflowServiceConfig",
    "WorkflowServiceSession",
    "load_demo_workflow_config",
    "orchestrate_workflow_services",
    "run_demo_workflow",
]
