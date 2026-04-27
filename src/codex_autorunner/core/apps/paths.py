from __future__ import annotations

from pathlib import PurePosixPath


class AppPathError(Exception):
    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


def validate_app_path(raw: str, *, allow_glob: bool = False) -> PurePosixPath:
    if not raw or raw.strip() == "" or raw == ".":
        raise AppPathError("path must not be empty or '.'", path=raw)

    if "\\" in raw:
        raise AppPathError("backslashes not allowed in app paths", path=raw)

    if ".." in raw:
        raise AppPathError("'..' not allowed in app paths", path=raw)

    parsed = PurePosixPath(raw)

    if parsed.is_absolute():
        raise AppPathError("absolute paths not allowed", path=raw)

    if ".." in parsed.parts:
        raise AppPathError("'..' not allowed in app paths", path=raw)

    if not allow_glob and any("*" in part or "?" in part for part in parsed.parts):
        raise AppPathError(
            "glob characters not allowed (pass allow_glob=True)", path=raw
        )

    return parsed


def validate_app_glob(raw: str) -> PurePosixPath:
    return validate_app_path(raw, allow_glob=True)
