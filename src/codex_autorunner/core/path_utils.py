from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


class ConfigPathError(Exception):
    """Raised when a config path is invalid."""

    def __init__(
        self,
        message: str,
        *,
        path: Optional[str] = None,
        resolved: Optional[Path] = None,
        scope: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.resolved = resolved
        self.scope = scope

    def __str__(self) -> str:
        msg = super().__str__()
        if self.scope:
            msg = f"{self.scope}: {msg}"
        if self.path:
            msg = f"{msg} (path: {self.path})"
        if self.resolved:
            msg = f"{msg} (resolved: {self.resolved})"
        return msg


def resolve_config_path(
    value: PathLike,
    repo_root: Path,
    *,
    allow_absolute: bool = False,
    allow_home: bool = False,
    allow_dotdot: bool = False,
    scope: Optional[str] = None,
) -> Path:
    """
    Resolve a config path according to standard rules.

    Rules:
    1. If value starts with '/' and allow_absolute=True, use as-is
    2. If value starts with '~', expand to home directory
    3. Otherwise, resolve relative to repo_root
    4. Reject '..' segments unless allow_dotdot=True
    5. Reject paths escaping repo_root (except home expansion)

    Args:
        value: Path string or Path object
        repo_root: Repository root directory
        allow_absolute: Allow absolute paths (default False)
        allow_home: Allow home directory expansion with ~ (default False)
        allow_dotdot: Allow '..' segments (default False, for security)
        scope: Config section name for error messages (e.g., 'docs.todo')

    Returns:
        Resolved Path object

    Raises:
        ConfigPathError: If path is invalid
    """
    value_str = str(value)

    if not value_str:
        raise ConfigPathError("Path cannot be empty", path=value_str, scope=scope)

    if value_str.strip() == "":
        raise ConfigPathError(
            "Path cannot be whitespace only", path=value_str, scope=scope
        )

    value_str = value_str.strip()

    path = Path(value_str)

    if path.is_absolute():
        if allow_absolute:
            return path.resolve()
        raise ConfigPathError(
            "Absolute paths are not allowed",
            path=value_str,
            scope=scope,
        )

    if str(path).startswith("~"):
        if not allow_home:
            raise ConfigPathError(
                "Home directory expansion (~) is not allowed",
                path=value_str,
                scope=scope,
            )
        if not allow_dotdot and ".." in path.parts:
            raise ConfigPathError(
                "Path contains '..' segments",
                path=value_str,
                scope=scope,
            )
        resolved = path.expanduser().resolve()
        return resolved

    if not allow_dotdot and ".." in path.parts:
        raise ConfigPathError(
            "Path contains '..' segments",
            path=value_str,
            scope=scope,
        )

    resolved = (repo_root / path).resolve()

    if not allow_home and not allow_dotdot and not resolved.is_relative_to(repo_root):
        raise ConfigPathError(
            "Path resolves outside repo root",
            path=value_str,
            resolved=resolved,
            scope=scope,
        )

    return resolved
