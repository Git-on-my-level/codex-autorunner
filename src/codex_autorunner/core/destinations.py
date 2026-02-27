from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

import yaml

from ..manifest import ManifestRepo, normalize_manifest_destination


class Destination(Protocol):
    @property
    def kind(self) -> str: ...

    def to_dict(self) -> Dict[str, Any]: ...


@dataclasses.dataclass(frozen=True)
class LocalDestination:
    kind: str = "local"

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind}


@dataclasses.dataclass(frozen=True)
class DockerDestination:
    image: str
    container_name: Optional[str] = None
    mounts: tuple[Dict[str, str], ...] = ()
    env_passthrough: tuple[str, ...] = ()
    workdir: Optional[str] = None
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    kind: str = "docker"

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"kind": self.kind, "image": self.image}
        if self.container_name:
            payload["container_name"] = self.container_name
        if self.mounts:
            payload["mounts"] = [dict(item) for item in self.mounts]
        if self.env_passthrough:
            payload["env_passthrough"] = list(self.env_passthrough)
        if self.workdir:
            payload["workdir"] = self.workdir
        payload.update(self.extra)
        return payload


@dataclasses.dataclass(frozen=True)
class DestinationValidationIssue:
    repo_id: str
    message: str


@dataclasses.dataclass(frozen=True)
class DestinationParseResult:
    destination: Destination
    valid: bool
    errors: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class DestinationResolution:
    destination: Destination
    source: str
    issues: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return self.destination.to_dict()


def default_local_destination() -> Dict[str, Any]:
    return LocalDestination().to_dict()


def parse_destination_config(
    value: Any,
    *,
    context: str = "destination",
) -> DestinationParseResult:
    normalized = normalize_manifest_destination(value)
    if normalized is None:
        return DestinationParseResult(
            destination=LocalDestination(),
            valid=False,
            errors=(f"{context}: expected a mapping with non-empty 'kind'",),
        )

    kind = str(normalized.get("kind", "")).strip().lower()
    if kind == "local":
        return DestinationParseResult(destination=LocalDestination(), valid=True)
    if kind != "docker":
        return DestinationParseResult(
            destination=LocalDestination(),
            valid=False,
            errors=(f"{context}: unsupported destination kind '{kind}'",),
        )

    errors: list[str] = []
    image = normalized.get("image")
    if not isinstance(image, str) or not image.strip():
        errors.append(f"{context}: docker destination requires non-empty 'image'")
    else:
        image = image.strip()

    container_name = normalized.get("container_name")
    if container_name is not None:
        if not isinstance(container_name, str) or not container_name.strip():
            errors.append(
                f"{context}: optional 'container_name' must be a non-empty string"
            )
            container_name = None
        else:
            container_name = container_name.strip()

    mounts: tuple[Dict[str, str], ...] = ()
    mounts_raw = normalized.get("mounts")
    if mounts_raw is not None:
        if not isinstance(mounts_raw, list):
            errors.append(f"{context}: optional 'mounts' must be a list")
        else:
            parsed_mounts: list[Dict[str, str]] = []
            for idx, mount in enumerate(mounts_raw):
                if not isinstance(mount, dict):
                    errors.append(
                        f"{context}: mounts[{idx}] must be an object with source/target"
                    )
                    continue
                source = mount.get("source")
                target = mount.get("target")
                if not isinstance(source, str) or not source.strip():
                    errors.append(
                        f"{context}: mounts[{idx}].source must be a non-empty string"
                    )
                    continue
                if not isinstance(target, str) or not target.strip():
                    errors.append(
                        f"{context}: mounts[{idx}].target must be a non-empty string"
                    )
                    continue
                parsed_mounts.append(
                    {"source": source.strip(), "target": target.strip()}
                )
            mounts = tuple(parsed_mounts)

    env_passthrough: tuple[str, ...] = ()
    env_raw = normalized.get("env_passthrough")
    if env_raw is not None:
        if not isinstance(env_raw, list):
            errors.append(f"{context}: optional 'env_passthrough' must be a list")
        else:
            parsed_env: list[str] = []
            for idx, item in enumerate(env_raw):
                if not isinstance(item, str) or not item.strip():
                    errors.append(
                        f"{context}: env_passthrough[{idx}] must be a non-empty string"
                    )
                    continue
                parsed_env.append(item.strip())
            env_passthrough = tuple(parsed_env)

    workdir = normalized.get("workdir")
    if workdir is not None:
        if not isinstance(workdir, str) or not workdir.strip():
            errors.append(f"{context}: optional 'workdir' must be a non-empty string")
            workdir = None
        else:
            workdir = workdir.strip()

    if errors:
        return DestinationParseResult(
            destination=LocalDestination(),
            valid=False,
            errors=tuple(errors),
        )

    extra = {
        key: val
        for key, val in normalized.items()
        if key
        not in {
            "kind",
            "image",
            "container_name",
            "mounts",
            "env_passthrough",
            "workdir",
        }
    }
    return DestinationParseResult(
        destination=DockerDestination(
            image=image,  # type: ignore[arg-type]
            container_name=container_name,
            mounts=mounts,
            env_passthrough=env_passthrough,
            workdir=workdir,
            extra=extra,
        ),
        valid=True,
    )


def resolve_effective_repo_destination(
    repo: ManifestRepo,
    repos_by_id: Mapping[str, ManifestRepo],
) -> DestinationResolution:
    issues: list[str] = []

    if repo.destination is not None:
        own = parse_destination_config(
            repo.destination, context=f"repo '{repo.id}' destination"
        )
        if own.valid:
            return DestinationResolution(destination=own.destination, source="repo")
        issues.extend(own.errors)

    if repo.kind == "worktree" and repo.worktree_of:
        parent = repos_by_id.get(repo.worktree_of)
        if parent and parent.destination is not None:
            inherited = parse_destination_config(
                parent.destination,
                context=f"base repo '{parent.id}' destination",
            )
            if inherited.valid:
                return DestinationResolution(
                    destination=inherited.destination,
                    source="base",
                    issues=tuple(issues),
                )
            issues.extend(inherited.errors)

    return DestinationResolution(
        destination=LocalDestination(),
        source="default",
        issues=tuple(issues),
    )


def validate_manifest_destinations(
    manifest_path: Path,
) -> list[DestinationValidationIssue]:
    if not manifest_path.exists():
        return []

    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return [
            DestinationValidationIssue(
                repo_id="manifest",
                message=f"failed to parse manifest YAML: {exc}",
            )
        ]

    if not isinstance(payload, dict):
        return [
            DestinationValidationIssue(
                repo_id="manifest",
                message="manifest root must be a mapping",
            )
        ]

    repos_raw = payload.get("repos")
    if repos_raw is None:
        return []
    if not isinstance(repos_raw, list):
        return [
            DestinationValidationIssue(
                repo_id="manifest",
                message="manifest 'repos' must be a list",
            )
        ]

    issues: list[DestinationValidationIssue] = []
    for idx, entry in enumerate(repos_raw):
        if not isinstance(entry, dict):
            issues.append(
                DestinationValidationIssue(
                    repo_id=f"<index:{idx}>",
                    message="repo entry must be an object",
                )
            )
            continue
        if "destination" not in entry:
            continue
        repo_id_raw = entry.get("id")
        repo_id = (
            repo_id_raw.strip()
            if isinstance(repo_id_raw, str) and repo_id_raw.strip()
            else f"<index:{idx}>"
        )
        parsed = parse_destination_config(
            entry.get("destination"),
            context=f"repo '{repo_id}' destination",
        )
        if parsed.valid:
            continue
        for err in parsed.errors:
            issues.append(DestinationValidationIssue(repo_id=repo_id, message=err))

    return issues


__all__ = [
    "Destination",
    "DestinationParseResult",
    "DestinationResolution",
    "DestinationValidationIssue",
    "DockerDestination",
    "LocalDestination",
    "default_local_destination",
    "parse_destination_config",
    "resolve_effective_repo_destination",
    "validate_manifest_destinations",
]
