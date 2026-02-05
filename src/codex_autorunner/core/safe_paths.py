"""Safe path validation utilities for web endpoints.

This module provides utilities for validating user-controlled paths to prevent
directory traversal attacks and other path-based security issues.
"""

from pathlib import PurePosixPath
from typing import Optional


class SafePathError(Exception):
    """Raised when a path fails safety validation."""

    def __init__(self, message: str, path: Optional[str] = None) -> None:
        super().__init__(message)
        self.path = path


def validate_relative_posix_path(raw: str) -> PurePosixPath:
    """Validate a user-provided path string and return a PurePosixPath.

    This function validates that:
    1. The path is not absolute
    2. The path does not contain '..' segments (parent directory traversal)
    3. The path does not contain backslashes (Windows separators)
    4. The path is not empty, '.', or only slashes

    Args:
        raw: The user-provided path string (typically from a URL path parameter)

    Returns:
        A validated PurePosixPath object

    Raises:
        SafePathError: If the path fails validation

    Examples:
        >>> validate_relative_posix_path("file.txt")
        PurePosixPath('file.txt')

        >>> validate_relative_posix_path("a/b/c.txt")
        PurePosixPath('a/b/c.txt')

        >>> validate_relative_posix_path("../etc/passwd")
        SafePathError: Invalid path: '..' not allowed

        >>> validate_relative_posix_path("/etc/passwd")
        SafePathError: Absolute paths not allowed
    """
    if not raw or raw.strip() == "" or raw == ".":
        raise SafePathError("Invalid path: empty or '.'", path=raw)

    # Reject backslashes early (Windows separators)
    if "\\" in raw:
        raise SafePathError("Invalid path: backslashes not allowed", path=raw)

    # Reject '..' in the raw path before PurePosixPath normalizes it
    # We need to check the raw string because PurePosixPath("a/../b")
    # normalizes to "b", which would bypass the later parts check
    if ".." in raw:
        raise SafePathError("Invalid path: '..' not allowed", path=raw)

    # Parse with PurePosixPath to ensure POSIX semantics
    try:
        file_rel = PurePosixPath(raw)
    except Exception as exc:
        raise SafePathError(f"Invalid path: {exc}", path=raw) from exc

    # Reject absolute paths
    if file_rel.is_absolute():
        raise SafePathError("Absolute paths not allowed", path=raw)

    # Double-check '..' traversal segments after parsing (for edge cases)
    if ".." in file_rel.parts:
        raise SafePathError("Invalid path: '..' not allowed", path=raw)

    return file_rel


def validate_single_filename(raw: str) -> str:
    """Validate that a path string represents only a single filename (no subpaths).

    This is a stricter version of validate_relative_posix_path that only allows
    a single filename component, not subdirectories.

    Args:
        raw: The user-provided path string

    Returns:
        The validated filename

    Raises:
        SafePathError: If the path contains slashes or is otherwise invalid

    Examples:
        >>> validate_single_filename("file.txt")
        'file.txt'

        >>> validate_single_filename("a/b.txt")
        SafePathError: Subpaths not allowed: only single filenames permitted

        >>> validate_single_filename("../etc/passwd")
        SafePathError: Subpaths not allowed: only single filenames permitted
    """
    file_rel = validate_relative_posix_path(raw)

    # Ensure only a single component (no subpaths)
    if len(file_rel.parts) != 1:
        raise SafePathError(
            "Subpaths not allowed: only single filenames permitted", path=raw
        )

    # Return the string representation of the filename
    return str(file_rel)


__all__ = ["SafePathError", "validate_relative_posix_path", "validate_single_filename"]
