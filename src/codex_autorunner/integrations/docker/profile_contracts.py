from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional, Sequence

DOCKER_PROFILE_FULL_DEV = "full-dev"


@dataclasses.dataclass(frozen=True)
class DockerProfileMount:
    source: str
    target: str
    read_only: bool = False


@dataclasses.dataclass(frozen=True)
class DockerProfileContract:
    name: str
    required_binaries: tuple[str, ...]
    default_mounts: tuple[DockerProfileMount, ...]
    default_env_passthrough: tuple[str, ...]
    required_auth_files: tuple[str, ...]


FULL_DEV_PROFILE_CONTRACT = DockerProfileContract(
    name=DOCKER_PROFILE_FULL_DEV,
    required_binaries=(
        "codex",
        "opencode",
        "python3",
        "git",
        "rg",
        "bash",
        "node",
        "pnpm",
    ),
    default_mounts=(
        DockerProfileMount(source="${HOME}/.codex", target="${HOME}/.codex"),
        DockerProfileMount(
            source="${HOME}/.local/share/opencode",
            target="${HOME}/.local/share/opencode",
        ),
    ),
    default_env_passthrough=(
        "CAR_*",
        "OPENAI_API_KEY",
        "CODEX_HOME",
        "OPENCODE_SERVER_USERNAME",
        "OPENCODE_SERVER_PASSWORD",
    ),
    required_auth_files=(
        "${HOME}/.codex/auth.json",
        "${HOME}/.local/share/opencode/auth.json",
    ),
)

SUPPORTED_DOCKER_PROFILES: tuple[str, ...] = (DOCKER_PROFILE_FULL_DEV,)


def resolve_docker_profile_contract(
    profile: Optional[str],
) -> Optional[DockerProfileContract]:
    profile_name = str(profile or "").strip().lower()
    if profile_name == DOCKER_PROFILE_FULL_DEV:
        return FULL_DEV_PROFILE_CONTRACT
    return None


def expand_profile_template(value: str, *, repo_root: Path, home_dir: Path) -> str:
    out = str(value)
    out = out.replace("${REPO_ROOT}", str(repo_root.resolve()))
    out = out.replace("${HOME}", str(home_dir))
    return out


def expand_profile_paths(
    values: Sequence[str],
    *,
    repo_root: Path,
    home_dir: Optional[Path] = None,
) -> tuple[str, ...]:
    resolved_home = home_dir or Path.home()
    expanded: list[str] = []
    for item in values:
        path = str(item).strip()
        if not path:
            continue
        expanded.append(
            expand_profile_template(path, repo_root=repo_root, home_dir=resolved_home)
        )
    return tuple(expanded)


__all__ = [
    "DOCKER_PROFILE_FULL_DEV",
    "DockerProfileContract",
    "DockerProfileMount",
    "FULL_DEV_PROFILE_CONTRACT",
    "SUPPORTED_DOCKER_PROFILES",
    "expand_profile_paths",
    "expand_profile_template",
    "resolve_docker_profile_contract",
]
