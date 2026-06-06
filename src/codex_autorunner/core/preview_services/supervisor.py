from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal, cast
from urllib.parse import urlparse

from ..force_attestation import enforce_force_attestation
from ..locks import file_lock, process_command_matches, process_is_active
from ..managed_processes.registry import (
    ProcessRecord,
    delete_process_record,
    read_process_record,
    write_process_record,
)
from ..process_termination import terminate_record
from ..utils import atomic_write
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
    EnvPolicy,
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
DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS = 30.0
DEFAULT_STARTUP_HEALTH_INTERVAL_SECONDS = 0.05
DEFAULT_EVENT_HISTORY_LIMIT = 200
_STATE_DIR = ".codex-autorunner"
_SERVICES_DIR = "services"
_LOCKS_DIR = "locks"
_EVENTS_DIR = "events"


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
        startup_health_timeout_seconds: float = DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS,
        event_history_limit: int = DEFAULT_EVENT_HISTORY_LIMIT,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._durable = durable
        self._host = host
        self._log_max_bytes = log_max_bytes
        self._startup_health_timeout_seconds = startup_health_timeout_seconds
        self._event_history_limit = event_history_limit
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
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

    def service_lock_path(self, service_id: str) -> Path:
        return (
            self._hub_root
            / _STATE_DIR
            / _SERVICES_DIR
            / _LOCKS_DIR
            / f"{service_id}.lock"
        )

    def events_path(self, service_id: str) -> Path:
        return (
            self._hub_root
            / _STATE_DIR
            / _SERVICES_DIR
            / _EVENTS_DIR
            / f"{service_id}.jsonl"
        )

    @contextmanager
    def service_lock(self, service_id: str) -> Iterator[None]:
        with file_lock(self.service_lock_path(service_id)):
            yield

    def events(self, service_id: str) -> list[dict[str, Any]]:
        path = self.events_path(service_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def register_static(
        self,
        path: Path,
        *,
        name: str | None = None,
        kind: PreviewServiceKind | str | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        service_class: str | None = None,
        trust_level: str | None = None,
        ownership: str | None = None,
        network_policy: str | None = None,
        metadata: Mapping[str, Any] | None = None,
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
        record = self._registry.create_from_parts(
            name=name or resolved.name or "Static preview",
            kind=parsed_kind,
            created_by=created_by,
            **_taxonomy_fields(
                service_class=service_class,
                trust_level=trust_level,
                ownership=ownership,
                network_policy=network_policy,
            ),
            scope_links=_scope_links(scope_links),
            target=ServiceTarget(path=str(resolved)).model_dump(
                mode="json", exclude_none=True
            ),
            restart_policy=RestartPolicy().model_dump(mode="json"),
            metadata=dict(metadata or {}),
        )
        self._append_event(record.service_id, "created", status=record.status)
        return record

    def register_loopback_url(
        self,
        url: str,
        *,
        name: str | None = None,
        health_path: str | None = "/",
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        service_class: str | None = None,
        trust_level: str | None = None,
        ownership: str | None = None,
        network_policy: str | None = None,
    ) -> PreviewServiceRecord:
        target = _loopback_target(url)
        health_check = (
            HealthCheck(type=HealthCheckType.HTTP, path=health_path)
            if health_path
            else HealthCheck(type=HealthCheckType.TCP, path=None)
        )
        record = self._registry.create_from_parts(
            name=name or url,
            kind=PreviewServiceKind.LOOPBACK_URL,
            created_by=created_by,
            **_taxonomy_fields(
                service_class=service_class,
                trust_level=trust_level,
                ownership=ownership,
                network_policy=network_policy,
            ),
            scope_links=_scope_links(scope_links),
            target=target.model_dump(mode="json", exclude_none=True),
            health_check=health_check.model_dump(mode="json", exclude_none=True),
            restart_policy=RestartPolicy().model_dump(mode="json"),
        )
        self._append_event(record.service_id, "created", status=record.status)
        return record

    def register_managed_command(
        self,
        *,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        env_policy: EnvPolicy | str = EnvPolicy.MINIMAL,
        port_policy: PortPolicy | dict[str, object] | None = None,
        health_check: HealthCheck | dict[str, object] | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        auto_start_on_hub_start: bool = False,
        service_class: str | None = None,
        trust_level: str | None = None,
        ownership: str | None = None,
        network_policy: str | None = None,
    ) -> PreviewServiceRecord:
        command = CommandDefinition(
            argv=[str(item) for item in argv],
            cwd=str(cwd.resolve()),
            env={str(key): str(value) for key, value in (env or {}).items()},
            env_policy=EnvPolicy(env_policy),
        )
        policy = _port_policy(port_policy)
        check = _health_check(health_check)
        record = self._registry.create_from_parts(
            name=name,
            kind=PreviewServiceKind.MANAGED_COMMAND,
            status=PreviewServiceStatus.STOPPED,
            created_by=created_by,
            **_taxonomy_fields(
                service_class=service_class,
                trust_level=trust_level,
                ownership=ownership,
                network_policy=network_policy,
            ),
            scope_links=_scope_links(scope_links),
            port_policy=policy.model_dump(mode="json", exclude_none=True),
            command=command.model_dump(mode="json", exclude_none=True),
            health_check=check.model_dump(mode="json", exclude_none=True),
            restart_policy=RestartPolicy(
                auto_start_on_hub_start=auto_start_on_hub_start
            ).model_dump(mode="json"),
        )
        self._append_event(record.service_id, "created", status=record.status)
        return record

    def start_managed_command(
        self,
        *,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        env_policy: EnvPolicy | str = EnvPolicy.MINIMAL,
        port_policy: PortPolicy | dict[str, object] | None = None,
        health_check: HealthCheck | dict[str, object] | None = None,
        scope_links: Sequence[ScopeLink | dict[str, object]] | None = None,
        created_by: str | None = None,
        auto_start_on_hub_start: bool = False,
        service_class: str | None = None,
        trust_level: str | None = None,
        ownership: str | None = None,
        network_policy: str | None = None,
    ) -> PreviewServiceRecord:
        record = self.register_managed_command(
            name=name,
            argv=argv,
            cwd=cwd,
            env=env,
            env_policy=env_policy,
            port_policy=port_policy,
            health_check=health_check,
            scope_links=scope_links,
            created_by=created_by,
            auto_start_on_hub_start=auto_start_on_hub_start,
            service_class=service_class,
            trust_level=trust_level,
            ownership=ownership,
            network_policy=network_policy,
        )
        return self.start(record.service_id)

    def start(self, service_id: str) -> PreviewServiceRecord:
        with self.service_lock(service_id):
            return self._start_locked(service_id)

    def _start_locked(self, service_id: str) -> PreviewServiceRecord:
        self.reconcile_service(service_id, lock=False)
        record = self._registry.require(service_id)
        if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
            raise PreviewServiceSupervisorError("Only managed services can be started")
        if _record_process_is_active(record):
            return record
        if record.command is None:
            raise PreviewServiceSupervisorError("Managed service has no command")

        self._registry.update(service_id, {"status": PreviewServiceStatus.STARTING})
        self._append_event(service_id, "starting", status=PreviewServiceStatus.STARTING)
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
            self._processes[service_id] = process
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
                    argv=argv,
                    cwd=command.cwd,
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
                    metadata={
                        "service_id": service_id,
                        "command_fingerprint": _command_fingerprint(argv, command.cwd),
                    },
                ),
                durable=self._durable,
            )
            self._append_event(
                service_id,
                "started",
                status=PreviewServiceStatus.RUNNING,
                pid=process.pid,
                pgid=pgid,
                port=allocation.port,
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
                self._processes.pop(service_id, None)
            failed = self._registry.update(
                service_id,
                lambda latest: _record_failed_without_allocation(latest),
            )
            self._append_event(
                service_id,
                "start_failed",
                status=failed.status,
                reason=str(exc),
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
        with self.service_lock(service_id):
            return self._stop_locked(service_id, grace_seconds=grace_seconds)

    def _stop_locked(
        self,
        service_id: str,
        *,
        grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    ) -> PreviewServiceRecord:
        self.reconcile_service(service_id, lock=False)
        record = self._registry.require(service_id)
        if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
            raise PreviewServiceSupervisorError("Only managed services can be stopped")
        process = record.process
        if process is not None:
            identity = _process_identity_status(self._hub_root, record)
            if identity == "match":
                terminate_record(
                    process.pid,
                    process.pgid,
                    grace_seconds=grace_seconds,
                    kill_seconds=DEFAULT_KILL_SECONDS,
                    logger=logger,
                    event_prefix="preview_services.stop",
                )
            elif identity != "exited":
                self._verify_process_identity_or_orphan(record, action="stop")
        delete_process_record(self._hub_root, PROCESS_KIND, service_id)
        self._processes.pop(service_id, None)
        stopped = self._registry.update(
            service_id,
            lambda latest: _record_stopped(latest, PreviewServiceStatus.STOPPED),
        )
        self._append_event(service_id, "stopped", status=stopped.status)
        return stopped

    def restart(self, service_id: str) -> PreviewServiceRecord:
        with self.service_lock(service_id):
            record = self.reconcile_service(service_id, lock=False)
            if record.status not in {
                PreviewServiceStatus.EXITED.value,
                PreviewServiceStatus.FAILED.value,
                PreviewServiceStatus.STOPPED.value,
                PreviewServiceStatus.REGISTERED.value,
                PreviewServiceStatus.CONFLICT.value,
            }:
                self._stop_locked(service_id)
            return self._start_locked(service_id)

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
        with self.service_lock(service_id):
            return self._kill_locked(
                service_id,
            )

    def _kill_locked(
        self,
        service_id: str,
    ) -> PreviewServiceRecord:
        self.reconcile_service(service_id, lock=False)
        record = self._registry.require(service_id)
        process = record.process
        if process is not None:
            self._verify_process_identity_or_orphan(record, action="kill")
            terminate_record(
                process.pid,
                process.pgid,
                grace_seconds=0,
                kill_seconds=DEFAULT_KILL_SECONDS,
                logger=logger,
                event_prefix="preview_services.kill",
            )
        delete_process_record(self._hub_root, PROCESS_KIND, service_id)
        self._processes.pop(service_id, None)
        killed = self._registry.update(
            service_id,
            lambda latest: _record_stopped(latest, PreviewServiceStatus.STOPPED),
        )
        self._append_event(service_id, "killed", status=killed.status)
        return killed

    def check_health(self, service_id: str) -> PreviewServiceHealthResult:
        with self.service_lock(service_id):
            self.reconcile_service(service_id, lock=False)
            record = self._registry.require(service_id)
            result = check_service_health(record)
            status = (
                PreviewServiceStatus.HEALTHY
                if result.ok
                else PreviewServiceStatus.UNHEALTHY
            )
            updated = self._registry.update(service_id, {"status": status})
            self._append_event(
                service_id,
                "healthy" if result.ok else "health_failed",
                status=updated.status,
                detail=_health_result_detail(result),
            )
            return result

    def update_service(
        self,
        service_id: str,
        changes: dict[str, Any],
    ) -> PreviewServiceRecord:
        with self.service_lock(service_id):
            current = self._registry.require(service_id)
            unsafe_running_keys = {
                "command",
                "port_policy",
                "health_check",
                "network_policy",
            }
            if _is_running_managed(current) and unsafe_running_keys.intersection(
                changes
            ):
                raise PreviewServiceSupervisorError(
                    "Cannot edit managed service runtime config while it is running; "
                    "stop it before changing command, port, health, or network policy."
                )
            updated = self._registry.update(service_id, changes)
            self._append_event(service_id, "updated", status=updated.status)
            return updated

    def teardown(
        self,
        service_id: str,
        *,
        force: bool = False,
        force_attestation: Mapping[str, object] | None = None,
    ) -> PreviewServiceRecord:
        with self.service_lock(service_id):
            record = self._registry.require(service_id)
            if _is_running_managed(record):
                if force:
                    enforce_force_attestation(
                        force=True,
                        force_attestation=force_attestation,
                        logger=logger,
                        action="hub.preview_services.teardown",
                    )
                    record = self._kill_locked(
                        service_id,
                    )
                else:
                    record = self._stop_locked(service_id)
            deleted = self._registry.delete(service_id)
            if not deleted:
                raise PreviewServiceSupervisorError(
                    f"Preview service not found: {service_id}"
                )
            self._append_event(service_id, "teardown", status=record.status)
            return record

    def unlink(
        self,
        service_id: str,
        *,
        force: bool = False,
        force_attestation: Mapping[str, object] | None = None,
    ) -> PreviewServiceRecord:
        with self.service_lock(service_id):
            self.reconcile_service(service_id, lock=False)
            record = self._registry.require(service_id)
            if _is_running_managed(record) and not force:
                raise PreviewServiceSupervisorError(
                    "Cannot unlink a running managed preview service without force; "
                    "use teardown to stop it first."
                )
            if _is_running_managed(record):
                enforce_force_attestation(
                    force=True,
                    force_attestation=force_attestation,
                    logger=logger,
                    action="hub.preview_services.unlink_running",
                )
                process = record.process
                logger.warning(
                    "preview_services.unlink_running_orphans_process",
                    extra={
                        "service_id": service_id,
                        "pid": process.pid if process is not None else None,
                        "pgid": process.pgid if process is not None else None,
                    },
                )
                self._append_event(
                    service_id,
                    "orphaned",
                    status=PreviewServiceStatus.ORPHANED,
                    reason="forced unlink left process unmanaged",
                )
            deleted = self._registry.delete(service_id)
            if not deleted:
                raise PreviewServiceSupervisorError(
                    f"Preview service not found: {service_id}"
                )
            self._append_event(service_id, "unlinked", status=record.status)
            return record

    def reconcile(self) -> list[PreviewServiceRecord]:
        records = self._registry.list()
        reconciled: list[PreviewServiceRecord] = []
        for record in records:
            reconciled.append(self.reconcile_service(record.service_id))
        return reconciled

    def reconcile_service(
        self,
        service_id: str,
        *,
        lock: bool = True,
    ) -> PreviewServiceRecord:
        if lock:
            with self.service_lock(service_id):
                return self.reconcile_service(service_id, lock=False)
        record = self._registry.require(service_id)
        if record.kind != PreviewServiceKind.MANAGED_COMMAND.value:
            return record
        if record.process is None:
            return record
        identity = _process_identity_status(self._hub_root, record)
        if identity == "mismatch":
            orphaned = self._registry.update(
                service_id,
                lambda latest: _record_orphaned(
                    latest,
                    reason="process identity could not be verified",
                ),
            )
            self._append_event(
                service_id,
                "orphaned",
                status=orphaned.status,
                reason="process identity could not be verified",
            )
            return orphaned
        if identity == "exited":
            process_handle = self._processes.pop(service_id, None)
            exit_code = _collect_exit_code(record.process.pid, process_handle)
            delete_process_record(self._hub_root, PROCESS_KIND, service_id)
            exited = self._registry.update(
                service_id,
                lambda latest: _record_exited(
                    latest,
                    exit_code=exit_code,
                    reason="process exited",
                ),
            )
            self._append_event(
                service_id,
                "exited",
                status=exited.status,
                exit_code=exit_code,
            )
            return exited
        return record

    def _verify_startup_health(
        self,
        record: PreviewServiceRecord,
        *,
        timeout_seconds: float = DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS,
        interval_seconds: float = DEFAULT_STARTUP_HEALTH_INTERVAL_SECONDS,
    ) -> PreviewServiceRecord:
        effective_timeout = (
            self._startup_health_timeout_seconds
            if timeout_seconds == DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS
            else timeout_seconds
        )
        deadline = time.monotonic() + effective_timeout
        latest = record
        while True:
            result = check_service_health(latest)
            if result.ok:
                healthy = self._registry.update(
                    latest.service_id,
                    {"status": PreviewServiceStatus.HEALTHY},
                )
                self._append_event(
                    latest.service_id,
                    "healthy",
                    status=healthy.status,
                    detail=_health_result_detail(result),
                )
                return healthy
            process = latest.process
            if (
                process is not None
                and process.pid
                and not process_is_active(process.pid)
            ):
                delete_process_record(self._hub_root, PROCESS_KIND, latest.service_id)
                self._processes.pop(latest.service_id, None)
                failed = self._registry.update(
                    latest.service_id,
                    lambda current: _record_stopped(
                        current,
                        PreviewServiceStatus.FAILED,
                    ),
                )
                self._append_event(
                    latest.service_id,
                    "health_failed",
                    status=failed.status,
                    detail="process exited during startup health check",
                )
                return failed
            if time.monotonic() >= deadline:
                unhealthy = self._registry.update(
                    latest.service_id,
                    {"status": PreviewServiceStatus.UNHEALTHY},
                )
                self._append_event(
                    latest.service_id,
                    "health_failed",
                    status=unhealthy.status,
                    detail=_health_result_detail(result),
                )
                return unhealthy
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

    def _append_event(
        self,
        service_id: str,
        event_type: str,
        **fields: Any,
    ) -> None:
        path = self.events_path(service_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "type": event_type,
            "service_id": service_id,
            "at": utc_now_iso(),
            **{
                key: (value.value if hasattr(value, "value") else value)
                for key, value in fields.items()
                if value is not None
            },
        }
        existing: list[str] = []
        if path.exists():
            existing = path.read_text(encoding="utf-8").splitlines()
        existing.append(json.dumps(event, sort_keys=True))
        if self._event_history_limit > 0:
            existing = existing[-self._event_history_limit :]
        atomic_write(
            path,
            "\n".join(existing) + "\n",
            durable=self._durable,
        )

    def _verify_process_identity_or_orphan(
        self,
        record: PreviewServiceRecord,
        *,
        action: str,
    ) -> None:
        status = _process_identity_status(self._hub_root, record)
        if status == "match":
            return
        if status == "exited":
            self.reconcile_service(record.service_id, lock=False)
            raise PreviewServiceSupervisorError(
                f"Preview service {record.service_id} already exited; cannot {action}"
            )
        orphaned = self._registry.update(
            record.service_id,
            lambda latest: _record_orphaned(
                latest,
                reason=f"process identity mismatch before {action}",
            ),
        )
        self._append_event(
            record.service_id,
            "orphaned",
            status=orphaned.status,
            reason=f"process identity mismatch before {action}",
        )
        raise PreviewServiceSupervisorError(
            f"Preview service {record.service_id} process identity could not be "
            f"verified; refusing to {action} by stale PID"
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


def _taxonomy_fields(
    *,
    service_class: str | None,
    trust_level: str | None,
    ownership: str | None,
    network_policy: str | None,
) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "service_class": service_class,
            "trust_level": trust_level,
            "ownership": ownership,
            "network_policy": network_policy,
        }.items()
        if value is not None
    }


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
    public_url = car_url
    base_path = car_url.rstrip("/") or "/"
    env = _base_subprocess_env(command.env_policy)
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
            "CAR_PREVIEW_BASE_PATH": base_path,
            "CAR_PREVIEW_PUBLIC_URL": public_url,
            "CAR_PREVIEW_URL": car_url,
        }
    )
    return env


def _base_subprocess_env(env_policy: str) -> dict[str, str]:
    if env_policy == EnvPolicy.INHERIT_ALL.value:
        return dict(os.environ)
    allowlist = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowlist}


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
    argv: Sequence[str],
    cwd: str,
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
                command_fingerprint=_command_fingerprint(argv, cwd),
                cwd=cwd,
                owner_pid=os.getpid(),
            ).model_dump(mode="json", exclude_none=True),
            "logs": logs.model_dump(mode="json", exclude_none=True),
        },
    )


def _record_stopped(
    record: PreviewServiceRecord,
    status: PreviewServiceStatus,
) -> PreviewServiceRecord:
    payload = record.to_dict()
    payload["status"] = status.value if hasattr(status, "value") else status
    payload["process"] = None
    if payload.get("port_policy"):
        payload["port_policy"].pop("allocated_port", None)
    payload["target"] = None
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)


def _record_exited(
    record: PreviewServiceRecord,
    *,
    exit_code: int | None,
    reason: str,
) -> PreviewServiceRecord:
    payload = record.to_dict()
    process = dict(payload.get("process") or {})
    process["exit_code"] = exit_code
    process["exited_at"] = utc_now_iso()
    process["last_exit_reason"] = reason
    payload["status"] = PreviewServiceStatus.EXITED.value
    payload["process"] = process
    if payload.get("port_policy"):
        payload["port_policy"].pop("allocated_port", None)
    payload["target"] = None
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)


def _record_orphaned(
    record: PreviewServiceRecord,
    *,
    reason: str,
) -> PreviewServiceRecord:
    payload = record.to_dict()
    process = dict(payload.get("process") or {})
    process["last_exit_reason"] = reason
    payload["status"] = PreviewServiceStatus.ORPHANED.value
    payload["process"] = process or None
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)


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


def _is_running_managed(record: PreviewServiceRecord) -> bool:
    return bool(
        record.kind == PreviewServiceKind.MANAGED_COMMAND.value
        and record.status
        in {
            PreviewServiceStatus.STARTING.value,
            PreviewServiceStatus.RUNNING.value,
            PreviewServiceStatus.HEALTHY.value,
            PreviewServiceStatus.UNHEALTHY.value,
        }
    )


def _health_result_detail(result: PreviewServiceHealthResult) -> str | None:
    if result.error:
        return result.error
    if result.status_code is not None:
        return f"status={result.status_code}"
    return result.check_type


def _process_identity_status(
    hub_root: Path,
    record: PreviewServiceRecord,
) -> Literal["match", "exited", "mismatch"]:
    process = record.process
    if process is None or process.pid is None:
        return "exited"
    if not process_is_active(process.pid):
        return "exited"
    if process.pgid is not None and os.name != "nt" and hasattr(os, "getpgid"):
        try:
            if os.getpgid(process.pid) != process.pgid:
                return "mismatch"
        except OSError:
            return "exited"
    process_record = read_process_record(hub_root, PROCESS_KIND, record.service_id)
    if process_record is None:
        return "mismatch"
    if process_record.pid != process.pid:
        return "mismatch"
    if process.pgid is not None and process_record.pgid != process.pgid:
        return "mismatch"
    if process.started_at and process_record.started_at != process.started_at:
        return "mismatch"
    expected_fingerprint = process.command_fingerprint
    actual_fingerprint = process_record.metadata.get("command_fingerprint")
    if expected_fingerprint and actual_fingerprint != expected_fingerprint:
        return "mismatch"
    if process.cwd and record.command is not None:
        current_command = process_record.command
        if _command_fingerprint(current_command, process.cwd) != expected_fingerprint:
            return "mismatch"
    if record.command is not None:
        command_match = process_command_matches(
            process.pid,
            _identity_command_fragments(process_record.command),
        )
        if command_match is False:
            return "mismatch"
    return "match"


def _identity_command_fragments(argv: Sequence[str]) -> list[str]:
    return [str(item) for item in argv if str(item)]


def _command_fingerprint(argv: Sequence[str], cwd: str) -> str:
    payload = json.dumps(
        {"argv": [str(item) for item in argv], "cwd": str(cwd)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_exit_code(
    pid: int | None,
    process_handle: subprocess.Popen[bytes] | None,
) -> int | None:
    if process_handle is not None:
        return process_handle.poll()
    if pid is None or os.name == "nt" or not hasattr(os, "waitpid"):
        return None
    try:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    except OSError:
        return None
    if waited_pid == 0:
        return None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return None


def _record_from_payload(
    record: PreviewServiceRecord,
    changes: dict[str, object],
) -> PreviewServiceRecord:
    payload = record.to_dict()
    payload.update(changes)
    payload["updated_at"] = utc_now_iso()
    return PreviewServiceRecord.model_validate(payload)
