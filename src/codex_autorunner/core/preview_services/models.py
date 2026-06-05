from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ids import validate_service_id


class PreviewServiceValidationError(ValueError):
    pass


class PreviewServiceKind(str, Enum):
    STATIC_FILE = "static_file"
    STATIC_DIR = "static_dir"
    LOOPBACK_URL = "loopback_url"
    MANAGED_COMMAND = "managed_command"


class PreviewServiceStatus(str, Enum):
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    EXITED = "exited"
    FAILED = "failed"
    ORPHANED = "orphaned"
    CONFLICT = "conflict"


class PreviewScopeKind(str, Enum):
    HUB = "hub"
    REPO = "repo"
    WORKTREE = "worktree"
    WORKSPACE = "workspace"
    CHAT = "chat"
    TICKET = "ticket"


class PortPolicyMode(str, Enum):
    EXACT = "exact"
    PREFERRED = "preferred"
    AUTO = "auto"


class HealthCheckType(str, Enum):
    HTTP = "http"
    TCP = "tcp"
    NONE = "none"


class RestartOnExit(str, Enum):
    NEVER = "never"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ScopeLink(StrictModel):
    kind: PreviewScopeKind
    id: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _validate_scope_payload(self) -> "ScopeLink":
        if self.kind == PreviewScopeKind.HUB:
            if self.id is not None or self.path is not None:
                raise ValueError("hub scope must not include id or path")
            return self
        if self.kind == PreviewScopeKind.WORKSPACE:
            if not self.path:
                raise ValueError("workspace scope requires path")
            return self
        if not self.id:
            raise ValueError(f"{self.kind} scope requires id")
        return self


class ServiceTarget(StrictModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    scheme: Literal["http", "https"] | None = None
    direct_url: str | None = None
    path: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("target.path must be non-empty when set")
        return value


class ServiceExposure(StrictModel):
    car_url: str
    proxy_enabled: bool = True

    @field_validator("car_url")
    @classmethod
    def _validate_car_url(cls, value: str) -> str:
        if not value.startswith("/preview/services/"):
            raise ValueError(
                "exposure.car_url must use /preview/services/{service_id}/"
            )
        return value


class PortPolicy(StrictModel):
    mode: PortPolicyMode
    port: int | None = Field(default=None, ge=1, le=65535)
    allocated_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def _validate_port_policy(self) -> "PortPolicy":
        if (
            self.mode in {PortPolicyMode.EXACT, PortPolicyMode.PREFERRED}
            and self.port is None
        ):
            raise ValueError(f"port_policy.mode={self.mode} requires port")
        if self.mode == PortPolicyMode.AUTO and self.port is not None:
            raise ValueError("port_policy.mode=auto must not include port")
        return self


class CommandDefinition(StrictModel):
    argv: list[str]
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("command.argv must be a non-empty list")
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("command.argv must contain only non-empty strings")
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        if not value:
            raise ValueError("command.cwd must be non-empty")
        return value


class ProcessMetadata(StrictModel):
    pid: int | None = Field(default=None, ge=1)
    pgid: int | None = Field(default=None, ge=1)
    started_at: str | None = None
    exit_code: int | None = None

    @field_validator("started_at")
    @classmethod
    def _validate_started_at(cls, value: str | None) -> str | None:
        if value is not None:
            parse_iso_timestamp(value, "process.started_at")
        return value


class HealthCheck(StrictModel):
    type: HealthCheckType
    path: str | None = "/"
    expected_status: list[int] = Field(
        default_factory=lambda: [200, 204, 301, 302, 304]
    )

    @model_validator(mode="after")
    def _validate_health_check(self) -> "HealthCheck":
        if self.type in {HealthCheckType.HTTP, HealthCheckType.HTTP.value}:
            if not self.path or not self.path.startswith("/"):
                raise ValueError("http health_check requires an absolute path")
            if not self.expected_status:
                raise ValueError("http health_check requires expected_status")
        if self.type in {HealthCheckType.TCP, HealthCheckType.TCP.value}:
            if self.path not in {None, "", "/"}:
                raise ValueError("tcp health_check must not include path")
            self.path = None
        return self


class RestartPolicy(StrictModel):
    auto_start_on_hub_start: bool = False
    restart_on_exit: RestartOnExit = RestartOnExit.NEVER


class ServiceLogs(StrictModel):
    path: str
    tail_url: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_log_path(cls, value: str) -> str:
        if not value:
            raise ValueError("logs.path must be non-empty")
        pure = PurePosixPath(value.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(
                "logs.path must be a relative path without parent traversal"
            )
        return value


class PreviewServiceRecord(StrictModel):
    service_id: str
    name: str
    kind: PreviewServiceKind
    status: PreviewServiceStatus = PreviewServiceStatus.REGISTERED
    created_by: str | None = None
    created_at: str
    updated_at: str
    scope_links: list[ScopeLink] = Field(default_factory=list)
    target: ServiceTarget | None = None
    exposure: ServiceExposure
    port_policy: PortPolicy | None = None
    command: CommandDefinition | None = None
    process: ProcessMetadata | None = None
    health_check: HealthCheck | None = None
    restart_policy: RestartPolicy = Field(default_factory=RestartPolicy)
    logs: ServiceLogs | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service_id")
    @classmethod
    def _validate_service_id(cls, value: str) -> str:
        return validate_service_id(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamp(cls, value: str, info: Any) -> str:
        parse_iso_timestamp(value, str(info.field_name))
        return value

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewServiceRecord":
        expected_url = f"/preview/services/{self.service_id}/"
        if self.exposure.car_url != expected_url:
            raise ValueError(f"exposure.car_url must be {expected_url}")
        if self.kind == PreviewServiceKind.MANAGED_COMMAND and self.command is None:
            raise ValueError("managed_command service requires command")
        if self.kind != PreviewServiceKind.MANAGED_COMMAND and self.process is not None:
            raise ValueError(
                "process metadata is only valid for managed_command services"
            )
        if self.kind != PreviewServiceKind.MANAGED_COMMAND and self.command is not None:
            raise ValueError("command is only valid for managed_command services")
        if self.kind in {PreviewServiceKind.STATIC_FILE, PreviewServiceKind.STATIC_DIR}:
            if self.target is None or self.target.path is None:
                raise ValueError(f"{self.kind} service requires target.path")
        if self.kind == PreviewServiceKind.LOOPBACK_URL:
            if self.target is None or not self.target.direct_url:
                raise ValueError("loopback_url service requires target.direct_url")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO timestamp string")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def service_record_from_dict(data: dict[str, Any]) -> PreviewServiceRecord:
    try:
        return PreviewServiceRecord.model_validate(data)
    except ValueError as exc:
        raise PreviewServiceValidationError(str(exc)) from exc
