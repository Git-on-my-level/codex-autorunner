import json
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Dict,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Union,
    cast,
)

if TYPE_CHECKING:
    from ..agents.opencode.supervisor import OpenCodeSupervisor


class RepoNotFoundError(Exception):
    pass


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for parent in [current] + list(current.parents):
        if (parent / ".git").exists():
            return parent
    raise RepoNotFoundError("Could not find .git directory in current or parent paths")


def canonicalize_path(path: Path) -> Path:
    return path.expanduser().resolve()


def is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(content)
    tmp_path.replace(path)


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return cast(Optional[dict], json.load(f))


def _default_path_prefixes() -> list[str]:
    """
    launchd and other non-interactive runners often have a minimal PATH that
    excludes Homebrew/MacPorts locations.
    """
    home = Path.home()
    candidates = [
        "/opt/homebrew/bin",  # Apple Silicon Homebrew
        "/usr/local/bin",  # Intel Homebrew + common user installs
        "/opt/local/bin",  # MacPorts
        str(home / ".opencode" / "bin"),  # OpenCode default install
        str(home / ".local" / "bin"),  # Common user-local installs
    ]
    return [p for p in candidates if os.path.isdir(p)]


def augmented_path(path: Optional[str] = None) -> str:
    prefixes = _default_path_prefixes()
    existing = [p for p in (path or "").split(os.pathsep) if p]
    merged: list[str] = []
    for p in prefixes + existing:
        if p and p not in merged:
            merged.append(p)
    return os.pathsep.join(merged)


def subprocess_env(
    extra_paths: Optional[Sequence[str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    env = dict(base_env) if base_env is not None else dict(os.environ)
    path = env.get("PATH")
    merged = augmented_path(path)
    if extra_paths:
        extra = [p for p in extra_paths if p]
        if extra:
            merged = augmented_path(os.pathsep.join(extra + [merged]))
    env["PATH"] = merged
    return env


def resolve_executable(
    binary: str, *, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """
    Resolve an executable path in a way that's resilient to minimal PATHs.
    Returns an absolute path if found, else None.
    """
    if not binary:
        return None
    # If explicitly provided a path, respect it.
    if os.path.sep in binary or (os.path.altsep and os.path.altsep in binary):
        candidate = Path(binary).expanduser()
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
        return None

    resolved = shutil.which(binary)
    if resolved:
        return resolved
    path = env.get("PATH") if env is not None else os.environ.get("PATH")
    resolved = shutil.which(binary, path=augmented_path(path))
    return resolved


def ensure_executable(binary: str) -> bool:
    return resolve_executable(binary) is not None


def default_editor() -> str:
    return os.environ.get("EDITOR") or "vi"


def resolve_opencode_binary(raw_command: Optional[str] = None) -> Optional[str]:
    """
    Resolve the OpenCode binary for minimal PATH environments.
    """
    if not raw_command:
        return None
    try:
        parts = [part for part in shlex.split(raw_command) if part]
    except ValueError:
        return None
    if not parts:
        return None
    return resolve_executable(parts[0])


def infer_home_from_workspace(workspace_root: Union[Path, str]) -> Optional[Path]:
    """
    Infer the user's home directory from a workspace path.

    Handles:
    - macOS with /Users/username
    - Linux with /home/username
    - macOS with /System/Volumes/Data/Users/username (Docker/WSL/Parallels)

    Returns None if the path doesn't match expected patterns.
    """
    resolved = Path(workspace_root).resolve()
    parts = resolved.parts
    if (
        len(parts) >= 6
        and parts[0] == os.path.sep
        and parts[1] == "System"
        and parts[2] == "Volumes"
        and parts[3] == "Data"
        and parts[4] == "Users"
    ):
        return Path(parts[0]) / parts[1] / parts[2] / parts[3] / parts[4] / parts[5]
    if (
        len(parts) >= 3
        and parts[0] == os.path.sep
        and parts[1]
        in (
            "Users",
            "home",
        )
    ):
        return Path(parts[0]) / parts[1] / parts[2]
    return None


def build_opencode_supervisor(
    *,
    opencode_command: Optional[Sequence[str]] = None,
    opencode_binary: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    logger: Optional["logging.Logger"] = None,
    request_timeout: Optional[float] = None,
    max_handles: Optional[int] = None,
    idle_ttl_seconds: Optional[float] = None,
    session_stall_timeout_seconds: Optional[float] = None,
    base_env: Optional[MutableMapping[str, str]] = None,
    subagent_models: Optional[Mapping[str, str]] = None,
) -> Optional["OpenCodeSupervisor"]:
    """
    Unified factory for building OpenCodeSupervisor instances.

    Centralizes:
    - Binary/serve-command resolution
    - Auth (username/password) sourcing from env
    - Request timeout / max handles / idle TTL behavior
    - Subagent model configuration
    """
    command = list(opencode_command or [])
    if not command and opencode_binary:
        command = [
            opencode_binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
        ]

    resolved_source = None
    if opencode_command:
        resolved_source = opencode_command[0]
    elif opencode_binary:
        resolved_source = opencode_binary
    resolved_binary = resolve_opencode_binary(resolved_source)

    if not command:
        return None

    if resolved_binary:
        command[0] = resolved_binary

    if not _command_available(command, workspace_root=workspace_root, env=base_env):
        return None

    if base_env is None:
        base_env = os.environ
    username = base_env.get("OPENCODE_SERVER_USERNAME")
    password = base_env.get("OPENCODE_SERVER_PASSWORD")
    if password and not username:
        username = "opencode"

    from ..agents.opencode.supervisor import OpenCodeSupervisor

    return OpenCodeSupervisor(
        command,
        logger=logger,
        request_timeout=request_timeout,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
        session_stall_timeout_seconds=session_stall_timeout_seconds,
        username=username if password else None,
        password=password if password else None,
        base_env=base_env,
        subagent_models=subagent_models,
    )


def _command_available(
    command: Sequence[str],
    *,
    workspace_root: Optional[Path],
    env: Optional[MutableMapping[str, str]] = None,
) -> bool:
    if not command or workspace_root is None:
        return False
    entry = str(command[0]).strip()
    if not entry:
        return False
    if os.path.sep in entry or (os.path.altsep and os.path.altsep in entry):
        path = Path(entry)
        if not path.is_absolute():
            path = workspace_root / path
        return path.is_file() and os.access(path, os.X_OK)
    return resolve_executable(entry, env=env) is not None
