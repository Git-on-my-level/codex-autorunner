from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List

from ..utils import atomic_write
from .ids import generate_service_id, validate_service_id
from .models import (
    PreviewServiceKind,
    PreviewServiceRecord,
    PreviewServiceStatus,
    ServiceExposure,
    service_record_from_dict,
    utc_now_iso,
)

_STATE_DIR = ".codex-autorunner"
_SERVICES_DIR = "services"
_RECORDS_DIR = "records"
NEEDS_ATTENTION_STATUSES = frozenset(
    {
        PreviewServiceStatus.UNHEALTHY.value,
        PreviewServiceStatus.FAILED.value,
        PreviewServiceStatus.EXITED.value,
        PreviewServiceStatus.ORPHANED.value,
        PreviewServiceStatus.CONFLICT.value,
    }
)


class PreviewServiceRegistryError(ValueError):
    pass


class PreviewServiceNotFoundError(PreviewServiceRegistryError):
    pass


@dataclass(frozen=True)
class PreviewServiceRegistryEntry:
    path: Path
    service_id: str
    record: PreviewServiceRecord | None = None
    error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.record is not None and self.error is None


class PreviewServiceRegistry:
    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = False,
        id_factory: Callable[[], str] = generate_service_id,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._durable = durable
        self._id_factory = id_factory

    @property
    def records_dir(self) -> Path:
        return self._hub_root / _STATE_DIR / _SERVICES_DIR / _RECORDS_DIR

    def list(self) -> List[PreviewServiceRecord]:
        entries = self.list_entries()
        invalid = next((entry for entry in entries if not entry.is_valid), None)
        if invalid is not None:
            raise PreviewServiceRegistryError(
                invalid.error or f"Invalid preview service record: {invalid.path}"
            )
        return [entry.record for entry in entries if entry.record is not None]

    def list_entries(self) -> List[PreviewServiceRegistryEntry]:
        if not self.records_dir.exists():
            return []
        entries: List[PreviewServiceRegistryEntry] = []
        for path in sorted(self.records_dir.glob("*.json")):
            entries.append(self._entry_from_path(path))
        return entries

    def get(self, service_id: str) -> PreviewServiceRecord | None:
        path = self._record_path(service_id)
        if not path.exists():
            return None
        entry = self._entry_from_path(path)
        if entry.record is None:
            raise PreviewServiceRegistryError(
                entry.error or f"Invalid preview service record: {path}"
            )
        return entry.record

    def require(self, service_id: str) -> PreviewServiceRecord:
        record = self.get(service_id)
        if record is None:
            raise PreviewServiceNotFoundError(
                f"Preview service not found: {service_id}"
            )
        return record

    def create(
        self,
        record: PreviewServiceRecord | dict[str, Any],
        *,
        service_id: str | None = None,
    ) -> PreviewServiceRecord:
        parsed = self._coerce_record(record)
        if service_id is not None:
            validate_service_id(service_id)
            if parsed.service_id != service_id:
                parsed = parsed.model_copy(
                    update={
                        "service_id": service_id,
                        "exposure": ServiceExposure(
                            car_url=f"/preview/services/{service_id}/",
                            proxy_enabled=parsed.exposure.proxy_enabled,
                        ),
                    }
                )
                parsed = self._coerce_record(parsed.to_dict())
        if self.get(parsed.service_id) is not None:
            raise PreviewServiceRegistryError(
                f"Preview service already exists: {parsed.service_id}"
            )
        self._write(parsed)
        return parsed

    def create_from_parts(
        self,
        *,
        name: str,
        kind: PreviewServiceKind | str,
        created_by: str | None = None,
        **fields: Any,
    ) -> PreviewServiceRecord:
        service_id = validate_service_id(
            str(fields.pop("service_id", "") or self._id_factory())
        )
        now = utc_now_iso()
        payload = {
            "service_id": service_id,
            "name": name,
            "kind": kind,
            "status": fields.pop("status", PreviewServiceStatus.REGISTERED),
            "created_by": created_by,
            "created_at": fields.pop("created_at", now),
            "updated_at": fields.pop("updated_at", now),
            "exposure": fields.pop(
                "exposure",
                {"car_url": f"/preview/services/{service_id}/", "proxy_enabled": True},
            ),
            **fields,
        }
        return self.create(payload)

    def update(
        self,
        service_id: str,
        changes: (
            dict[str, Any] | Callable[[PreviewServiceRecord], PreviewServiceRecord]
        ),
    ) -> PreviewServiceRecord:
        current = self.require(service_id)
        if callable(changes):
            updated = changes(current)
        else:
            updated = current.model_copy(
                update={**changes, "updated_at": utc_now_iso()}
            )
            updated = self._coerce_record(updated.to_dict())
        if updated.service_id != current.service_id:
            raise PreviewServiceRegistryError("service_id cannot be changed")
        self._write(updated)
        return updated

    def delete(self, service_id: str) -> bool:
        path = self._record_path(service_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def read_model(self) -> dict[str, Any]:
        records = [service_read_model(record) for record in self.list()]
        counts = _service_counts(records)
        return {"services": records, "counts": counts}

    def _write(self, record: PreviewServiceRecord) -> Path:
        payload = record.to_dict()
        path = self._record_path(record.service_id)
        atomic_write(
            path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            durable=self._durable,
        )
        return path

    def _record_path(self, service_id: str) -> Path:
        clean_id = validate_service_id(service_id)
        return self.records_dir / f"{clean_id}.json"

    def _coerce_record(
        self, record: PreviewServiceRecord | dict[str, Any]
    ) -> PreviewServiceRecord:
        if isinstance(record, PreviewServiceRecord):
            return service_record_from_dict(record.to_dict())
        if not isinstance(record, dict):
            raise PreviewServiceRegistryError("preview service record must be a dict")
        return service_record_from_dict(record)

    def _entry_from_path(self, path: Path) -> PreviewServiceRegistryEntry:
        fallback_id = path.stem
        try:
            validate_service_id(fallback_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            record = service_record_from_dict(data)
        except json.JSONDecodeError:
            return PreviewServiceRegistryEntry(
                path=path,
                service_id=fallback_id,
                error=f"Invalid preview service JSON: {path}",
            )
        except (OSError, ValueError) as exc:
            return PreviewServiceRegistryEntry(
                path=path,
                service_id=fallback_id,
                error=str(exc),
            )
        if record.service_id != fallback_id:
            return PreviewServiceRegistryEntry(
                path=path,
                service_id=fallback_id,
                record=record,
                error="record service_id does not match registry path",
            )
        return PreviewServiceRegistryEntry(
            path=path, service_id=fallback_id, record=record
        )


def service_read_model(record: PreviewServiceRecord) -> dict[str, Any]:
    data = record.to_dict()
    scope = _scope_label(data.get("scope_links") or [])
    target = data.get("target") or {}
    port_policy = data.get("port_policy") or {}
    process = data.get("process") or {}
    capabilities = service_capabilities(record)
    desired_state = record.desired_state.model_dump(mode="json", exclude_none=True)
    desired_state = _redact_desired_state(desired_state)
    return {
        "service_id": data["service_id"],
        "name": data["name"],
        "kind": data["kind"],
        "service_class": data["service_class"],
        "trust_level": data["trust_level"],
        "ownership": data["ownership"],
        "network_policy": data["network_policy"],
        "status": data["status"],
        "created_by": data.get("created_by"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "scope_links": data.get("scope_links") or [],
        "scope": scope,
        "car_url": data["exposure"]["car_url"],
        "proxy_enabled": data["exposure"].get("proxy_enabled", True),
        "direct_url": target.get("direct_url"),
        "host": target.get("host"),
        "port": target.get("port") or port_policy.get("allocated_port"),
        "owner_pid": process.get("pid"),
        "health_check": data.get("health_check"),
        "restart_policy": data.get("restart_policy") or {},
        "logs": data.get("logs"),
        "metadata": data.get("metadata") or {},
        "capabilities": capabilities,
        "desired_state": desired_state,
        "observed_state": record.observed_state.model_dump(
            mode="json", exclude_none=True
        ),
    }


def _redact_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    command = desired_state.get("command")
    if isinstance(command, dict):
        env = command.get("env")
        if isinstance(env, dict):
            command = dict(command)
            command["env"] = {str(key): "<redacted>" for key in env}
            desired_state = {**desired_state, "command": command}
    return desired_state


def service_capabilities(record: PreviewServiceRecord) -> dict[str, bool]:
    kind = _enum_value(record.kind)
    status = _enum_value(record.status)
    managed = kind == PreviewServiceKind.MANAGED_COMMAND.value
    running = status in _running_statuses()
    startable = status in {
        PreviewServiceStatus.REGISTERED.value,
        PreviewServiceStatus.STOPPED.value,
        PreviewServiceStatus.EXITED.value,
        PreviewServiceStatus.FAILED.value,
        PreviewServiceStatus.CONFLICT.value,
    }
    return {
        "can_open": bool(record.exposure.proxy_enabled),
        "can_start": bool(managed and startable),
        "can_stop": bool(managed and running),
        "can_restart": bool(
            managed and record.command is not None and (running or startable)
        ),
        "can_kill": bool(managed and running),
        "can_unlink": not (managed and running),
        "can_teardown": True,
        "can_edit": True,
        "can_view_logs": bool(managed and record.logs is not None),
        "requires_force_for_unlink": bool(managed and running),
    }


def services_read_model(records: list[PreviewServiceRecord]) -> dict[str, Any]:
    service_models = [
        service_read_model(record)
        for record in sorted(records, key=lambda item: item.service_id)
    ]
    return {"services": service_models, "counts": _service_counts(service_models)}


def _service_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(records),
        "running": sum(
            1 for record in records if record["status"] in _running_statuses()
        ),
        "attention": sum(
            1 for record in records if record["status"] in NEEDS_ATTENTION_STATUSES
        ),
        "managed": sum(
            1
            for record in records
            if record["kind"] == PreviewServiceKind.MANAGED_COMMAND.value
        ),
        "static": sum(
            1
            for record in records
            if record["kind"]
            in {
                PreviewServiceKind.STATIC_FILE.value,
                PreviewServiceKind.STATIC_DIR.value,
            }
        ),
        "loopback": sum(
            1
            for record in records
            if record["kind"] == PreviewServiceKind.LOOPBACK_URL.value
        ),
        "preview": sum(1 for record in records if record["service_class"] == "preview"),
        "application": sum(
            1 for record in records if record["service_class"] == "application"
        ),
        "infrastructure": sum(
            1 for record in records if record["service_class"] == "infrastructure"
        ),
    }


def _running_statuses() -> set[str]:
    return {
        PreviewServiceStatus.STARTING.value,
        PreviewServiceStatus.RUNNING.value,
        PreviewServiceStatus.HEALTHY.value,
        PreviewServiceStatus.UNHEALTHY.value,
    }


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _scope_label(scope_links: list[dict[str, Any]]) -> str | None:
    if not scope_links:
        return None
    first = scope_links[0]
    kind = first.get("kind")
    if first.get("id"):
        return f"{kind}:{first['id']}"
    if first.get("path"):
        return f"{kind}:{first['path']}"
    return str(kind) if kind else None


def read_preview_services_model(
    hub_root: Path, *, durable: bool = False
) -> dict[str, Any]:
    return PreviewServiceRegistry(hub_root, durable=durable).read_model()
