from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from ..force_attestation import enforce_force_attestation
from ..locks import process_is_active
from ..managed_processes.registry import (
    ProcessRecord,
    delete_process_record,
    write_process_record,
)
from ..process_termination import terminate_record
from .health import PreviewServiceHealthResult, check_service_health
from .logs import (
    DEFAULT_LOG_MAX_BYTES,
    append_bounded_log_bytes,
    prepare_log_file,
    service_log_relative_path,
    service_log_tail_url,
    tail_log_file,
)
from .models import (
    CommandDefinition,
    HealthCheck,
    HealthCheckType,
    PortPolicy,
    PortPolicyMode,
    PreviewServiceKind,
    PreviewServiceRecord,
    PreviewServiceStatus,
    ProcessMetadata,
    RestartPolicy,
    ScopeLink,
    ServiceLogs,
    ServiceTarget,
    utc_now_iso,
)
from .port_allocator import PreviewPortAllocator
from .registry import PreviewServiceRegistry

logger = logging.getLogger("codex_autorunner.preview_services.supervisor")

PROCESS_KIND = "preview_service"
DEFAULT_STOP_GRACE_SECONDS = 1.0
DEFAULT_KILL_SECONDS = 0.2
DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS = 2.0
DEFAULT_STARTUP_HEALTH_INTERVAL_SECONDS = 0.05


class PreviewServiceSupervisorError(ValueError):
    pass


class PreviewServiceSupervisor:
    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = False,
        host: str = "127.0.0.1",
        port_range: tuple[int, int] = (39000, 39999),
        log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._durable = durable
        self._host = host
        self._log_max_bytes = log_max_bytes
        self._registry = PreviewServiceRegistry(self._hub_root, durable=durable)
        self._allocator = PreviewPortAllocator(
            self._hub_root,
            port_range=port_range,
            host=host,
            durable=durable,
        )

    @property
    def registry(self) -> PreviewServiceRegistry:
        return self._registry

    def register_static(
        self,
        path: Path,
        *,
        name: str | None = None,
        kind: PreviewServiceKind | str | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
    ) -> PreviewServiceRecord:
        resolved = path.resolve()
        selected_kind = kind or (
            PreviewServiceKind.STATIC_DIR
            if resolved.is_dir()
            else PreviewServiceKind.STATIC_FILE
        )
        parsed_kind = PreviewServiceKind(selected_kind)
        if parsed_kind not in {
            PreviewServiceKind.STATIC_FILE,
            PreviewServiceKind.STATIC_DIR,
        }:
            raise PreviewServiceSupervisorError(
                "static registration requires static kind"
            )
        if parsed_kind == PreviewServiceKind.STATIC_FILE and resolved.is_dir():
            raise PreviewServiceSupervisorError("static_file path must be a file")
        if parsed_kind == PreviewServiceKind.STATIC_DIR and not resolved.is_dir():
            raise PreviewServiceSupervisorError("static_dir path must be a directory")
        return self._registry.create_from_parts(
            name=name or resolved.name or "Static preview",
            kind=parsed_kind,
            created_by=created_by,
            scope_links=_scope_links(scope_links),
            target=ServiceTarget(path=str(resolved)).model_dump(
                mode="json", exclude_none=True
            ),
            restart_policy=RestartPolicy().model_dump(mode="json"),
        )

    def register_loopback_url(
        self,
        url: str,
        *,
        name: str | None = None,
        health_path: str | None = "/",
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
    ) -> PreviewServiceRecord:
        target = _loopback_target(url)
        health_check = (
            HealthCheck(type=HealthCheckType.HTTP, path=health_path)
            if health_path
            else HealthCheck(type=HealthCheckType.TCP, path=None)
        )
        return self._registry.create_from_parts(
            name=name or url,
            kind=PreviewServiceKind.LOOPBACK_URL,
            created_by=created_by,
            scope_links=_scope_links(scope_links),
            target=target.model_dump(mode="json", exclude_none=True),
            health_check=health_check.model_dump(mode="json", exclude_none=True),
            restart_policy=RestartPolicy().model_dump(mode="json"),
        )

    def register_managed_command(
        self,
        *,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        port_policy: PortPolicy | dict[str, object] | None = None,
        health_check: HealthCheck | dict[str, object] | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        auto_start_on_hub_start: bool = False,
    ) -> PreviewServiceRecord:
        command = CommandDefinition(
            argv=[str(item) for item in argv],
            cwd=str(cwd.resolve()),
            env={str(key): str(value) for key, value in (env or {}).items()},
        )
        policy = _port_policy(port_policy)
        check = _health_check(health_check)
        return self._registry.create_from_parts(
            name=name,
            kind=PreviewServiceKind.MANAGED_COMMAND,
            status=PreviewServiceStatus.STOPPED,
            created_by=created_by,
            scope_links=_scope_links(scope_links),
            port_policy=policy.model_dump(mode="json", exclude_none=True),
            command=command.model_dump(mode="json", exclude_none=True),
            health_check=check.model_dump(mode="json", exclude_none=True),
            restart_policy=RestartPolicy(
                auto_start_on_hub_start=auto_start_on_hub_start
            ).model_dump(mode="json"),
        )

    def start_managed_command(
        self,
        *,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        port_policy: PortPolicy | dict[str, object] | None = None,
        health_check: HealthCheck | dict[str, object] | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        auto_start_on_hub_start: bool = False,
    ) -> PreviewServiceRecord:
        record = self.register_managed_command(
            name=name,
            argv=argv,
            cwd=cwd,
            env=env,
            port_policy=port_policy,
            health_check=health_check,
            scope_links=scope_links,
            created_by=created_by,
            auto_start_on_hub_start=auto_start_on_hub_start,
        )
        return self.start(record.service_id)

    def start(self, service_id: str) -> PreviewServiceRecord:
        record = self._registry.require(service_id)
        if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
            raise PreviewServiceSupervisorError("Only managed services can be started")
        if _record_process_is_active(record):
            return record
        if record.command is None:
            raise PreviewServiceSupervisorError("Managed service has no command")

        self._registry.update(service_id, {"status": PreviewServiceStatus.STARTING})
        process: subprocess.Popen[bytes] | None = None
        pgid: int | None = None
        try:
            allocation = self._allocator.reserve(service_id, record.port_policy)
            current = self._registry.require(service_id)
            command = current.command
            if command is None:
                raise PreviewServiceSupervisorError("Managed service has no command")
            log_path = prepare_log_file(
                self._hub_root,
                service_id,
                max_bytes=self._log_max_bytes,
            )
            env = _subprocess_env(
                command,
                port=allocation.port,
                host=allocation.host,
                service_id=service_id,
                car_url=current.exposure.car_url,
            )
            argv = _substitute_tokens(
                command.argv,
                port=allocation.port,
                host=allocation.host,
                service_id=service_id,
                car_url=current.exposure.car_url,
            )
            process = subprocess.Popen(
                argv,
                cwd=command.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
            _start_log_pump(
                process,
                log_path,
                max_bytes=self._log_max_bytes,
                service_id=service_id,
            )
            pgid = _process_group_id(process.pid)
            started_at = utc_now_iso()
            updated = self._registry.update(
                service_id,
                lambda latest: _record_with_process(
                    latest,
                    pid=process.pid,
                    pgid=pgid,
                    started_at=started_at,
                    status=PreviewServiceStatus.RUNNING,
                ),
            )
            write_process_record(
                self._hub_root,
                ProcessRecord(
                    kind=PROCESS_KIND,
                    handle_id=service_id,
                    workspace_id=None,
                    pid=process.pid,
                    pgid=pgid,
                    base_url=allocation.direct_url,
                    command=argv,
                    owner_pid=os.getpid(),
                    started_at=started_at,
                    metadata={"service_id": service_id},
                ),
                durable=self._durable,
            )
            return self._verify_startup_health(updated)
        except Exception as exc:
            if process is not None:
                terminate_record(
                    process.pid,
                    pgid,
                    grace_seconds=0,
                    kill_seconds=DEFAULT_KILL_SECONDS,
                    logger=logger,
                    event_prefix="preview_services.start_failed_cleanup",
                )
                delete_process_record(self._hub_root, PROCESS_KIND, service_id)
            self._registry.update(
                service_id,
                lambda latest: _record_failed_without_allocation(latest),
            )
            raise PreviewServiceSupervisorError(
                f"Failed to start preview service {service_id}: {exc}"
            ) from exc

    def stop(
        self,
        service_id: str,
        *,
        grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    ) -> PreviewServiceRecord:
        record = self._registry.require(service_id)
        if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
            raise PreviewServiceSupervisorError("Only managed services can be stopped")
        process = record.process
        if process is not None:
            terminate_record(
                process.pid,
                process.pgid,
                grace_seconds=grace_seconds,
                kill_seconds=DEFAULT_KILL_SECONDS,
                logger=logger,
                event_prefix="preview_services.stop",
            )
        delete_process_record(self._hub_root, PROCESS_KIND, service_id)
        return self._registry.update(
            service_id,
            lambda latest: _record_stopped(latest, PreviewServiceStatus.STOPPED),
        )

    def restart(self, service_id: str) -> PreviewServiceRecord:
        self.stop(service_id)
        return self.start(service_id)

    def kill(
        self,
        service_id: str,
        *,
        force: bool = False,
        force_attestation: Mapping[str, object] | None = None,
    ) -> PreviewServiceRecord:
        if not force:
            raise PreviewServiceSupervisorError("kill requires force=true")
        enforce_force_attestation(
            force=force,
            force_attestation=force_attestation,
            logger=logger,
            action="hub.preview_services.kill",
        )
        record = self._registry.require(service_id)
        process = record.process
        if process is not None:
            terminate_record(
                process.pid,
                process.pgid,
                grace_seconds=0,
                kill_seconds=DEFAULT_KILL_SECONDS,
                logger=logger,
                event_prefix="preview_services.kill",
            )
        delete_process_record(self._hub_root, PROCESS_KIND, service_id)
        return self._registry.update(
            service_id,
            lambda latest: _record_stopped(latest, PreviewServiceStatus.STOPPED),
        )

    def check_health(self, service_id: str) -> PreviewServiceHealthResult:
        record = self._registry.require(service_id)
        result = check_service_health(record)
        status = (
            PreviewServiceStatus.HEALTHY
            if result.ok
            else PreviewServiceStatus.UNHEALTHY
        )
        self._registry.update(service_id, {"status": status})
        return result

    def _verify_startup_health(
        self,
        record: PreviewServiceRecord,
        *,
        timeout_seconds: float = DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS,
        interval_seconds: float = DEFAULT_STARTUP_HEALTH_INTERVAL_SECONDS,
    ) -> PreviewServiceRecord:
        deadline = time.monotonic() + timeout_seconds
        latest = record
        while True:
            result = check_service_health(latest)
            if result.ok:
                return self._registry.update(
                    latest.service_id,
                    {"status": PreviewServiceStatus.HEALTHY},
                )
            process = latest.process
            if (
                process is not None
                and process.pid
                and not process_is_active(process.pid)
            ):
                delete_process_record(self._hub_root, PROCESS_KIND, latest.service_id)
                return self._registry.update(
                    latest.service_id,
                    lambda current: _record_stopped(
                        current,
                        PreviewServiceStatus.FAILED,
                    ),
                )
            if time.monotonic() >= deadline:
                return self._registry.update(
                    latest.service_id,
                    {"status": PreviewServiceStatus.UNHEALTHY},
                )
            time.sleep(interval_seconds)
            latest = self._registry.require(latest.service_id)

    def logs(self, service_id: str, *, tail: int = 200) -> str:
        self._registry.require(service_id)
        return tail_log_file(
            self._hub_root,
            service_id,
            lines=tail,
            max_bytes=self._log_max_bytes,
        )

    async def close_all(self) -> None:
        records = await asyncio.to_thread(self._registry.list)
        for record in records:
            if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
                continue
            if record.process is None:
                continue
            try:
                await asyncio.to_thread(self.stop, record.service_id)
            except Exception:  # intentional: shutdown cleanup must continue
                logger.debug(
                    "error stopping preview service during close_all",
                    extra={"service_id": record.service_id},
                    exc_info=True,
                )


def _scope_links(
    scope_links: Sequence[ScopeLink | dict[str, object]] | None,
) -> list[dict[str, object]]:
    if not scope_links:
        return []
    return [
        (
            link.model_dump(mode="json", exclude_none=True)
            if isinstance(link, ScopeLink)
            else dict(link)
        )
        for link in scope_links
    ]


def _port_policy(policy: PortPolicy | dict[str, object] | None) -> PortPolicy:
    if isinstance(policy, PortPolicy):
        return policy
    if policy is None:
        return PortPolicy(mode=PortPolicyMode.AUTO)
    return PortPolicy.model_validate(policy)


def _health_check(check: HealthCheck | dict[str, object] | None) -> HealthCheck:
    if isinstance(check, HealthCheck):
        return check
    if check is None:
        return HealthCheck(type=HealthCheckType.HTTP, path="/")
    return HealthCheck.model_validate(check)


def _loopback_target(url: str) -> ServiceTarget:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise PreviewServiceSupervisorError("loopback URL must be http or https")
    host = parsed.hostname
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise PreviewServiceSupervisorError("loopback URL host must be local")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    direct_url = parsed.geturl()
    scheme = cast(Literal["http", "https"], parsed.scheme)
    return ServiceTarget(
        host=host,
        port=port,
        scheme=scheme,
        direct_url=direct_url,
    )


def _subprocess_env(
    command: CommandDefinition,
    *,
    port: int,
    host: str,
    service_id: str,
    car_url: str,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        _substitute_env(
            command.env,
            port=port,
            host=host,
            service_id=service_id,
            car_url=car_url,
        )
    )
    env.update(
        {
            "PORT": str(port),
            "HOST": host,
            "CAR_PREVIEW_SERVICE_ID": service_id,
            "CAR_PREVIEW_URL": car_url,
        }
    )
    return env


def _substitute_env(
    env: Mapping[str, str],
    *,
    port: int,
    host: str,
    service_id: str,
    car_url: str,
) -> dict[str, str]:
    return {
        str(key): _substitute_token_text(
            str(value),
            port=port,
            host=host,
            service_id=service_id,
            car_url=car_url,
        )
        for key, value in env.items()
    }


def _substitute_tokens(
    argv: Sequence[str],
    *,
    port: int,
    host: str,
    service_id: str,
    car_url: str,
) -> list[str]:
    return [
        _substitute_token_text(
            str(item),
            port=port,
            host=host,
            service_id=service_id,
            car_url=car_url,
        )
        for item in argv
    ]


def _substitute_token_text(
    value: str,
    *,
    port: int,
    host: str,
    service_id: str,
    car_url: str,
) -> str:
    return (
        value.replace("$PORT", str(port))
        .replace("$HOST", host)
        .replace("$CAR_PREVIEW_SERVICE_ID", service_id)
        .replace("$CAR_PREVIEW_URL", car_url)
    )


def _process_group_id(pid: int) -> int | None:
    if os.name == "nt" or not hasattr(os, "getpgid"):
        return pid
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def _start_log_pump(
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    max_bytes: int,
    service_id: str,
) -> None:
    stream = process.stdout
    if stream is None:
        return

    def pump() -> None:
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    return
                append_bounded_log_bytes(log_path, chunk, max_bytes=max_bytes)
        except Exception:
            logger.debug(
                "preview service log pump failed",
                extra={"service_id": service_id},
                exc_info=True,
            )
        finally:
            try:
                stream.close()
            except OSError:
                pass

    thread = threading.Thread(
        target=pump,
        name=f"preview-service-log-{service_id}",
        daemon=True,
    )
    thread.start()


def _record_process_is_active(record: PreviewServiceRecord) -> bool:
    return bool(
        record.process and record.process.pid and process_is_active(record.process.pid)
    )


def _record_with_process(
    record: PreviewServiceRecord,
    *,
    pid: int,
    pgid: int | None,
    started_at: str,
    status: PreviewServiceStatus,
) -> PreviewServiceRecord:
    logs = ServiceLogs(
        path=service_log_relative_path(record.service_id),
        tail_url=service_log_tail_url(record.service_id),
    )
    return _record_from_payload(
        record,
        {
            "status": status,
            "process": ProcessMetadata(
                pid=pid,
                pgid=pgid,
                started_at=started_at,
                exit_code=None,
            ).model_dump(mode="json", exclude_none=True),
            "logs": logs.model_dump(mode="json", exclude_none=True),
        },
    )


def _record_stopped(
    record: PreviewServiceRecord,
    status: PreviewServiceStatus,
) -> PreviewServiceRecord:
    return _record_from_payload(
        record,
        {
            "status": status,
            "process": None,
        },
    )


def _record_failed_without_allocation(
    record: PreviewServiceRecord,
) -> PreviewServiceRecord:
    payload = record.to_dict()
    payload["status"] = PreviewServiceStatus.FAILED.value
    payload["process"] = None
    if payload.get("port_policy"):
        payload["port_policy"].pop("allocated_port", None)
    payload["target"] = None
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)


def _record_from_payload(
    record: PreviewServiceRecord,
    changes: dict[str, object],
) -> PreviewServiceRecord:
    payload = record.to_dict()
    payload.update(changes)
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)
