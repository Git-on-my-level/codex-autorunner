import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, cast


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
    candidates = [
        "/opt/homebrew/bin",  # Apple Silicon Homebrew
        "/usr/local/bin",  # Intel Homebrew + common user installs
        "/opt/local/bin",  # MacPorts
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
