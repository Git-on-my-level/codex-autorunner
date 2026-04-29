from __future__ import annotations

import dataclasses
import re
from typing import Optional

_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")


@dataclasses.dataclass(frozen=True)
class AppRef:
    repo_id: str
    app_path: str
    ref: Optional[str]


def validate_app_id(raw: str) -> str:
    if not _APP_ID_RE.match(raw):
        raise ValueError(
            f"invalid app id: {raw!r} " "(must match ^[a-z0-9][a-z0-9._-]{1,127}$)"
        )
    return raw


def parse_app_ref(raw: str) -> AppRef:
    if ":" not in raw:
        raise ValueError("app ref must be formatted as REPO_ID:APP_PATH[@REF]")
    repo_id, remainder = raw.split(":", 1)
    if not repo_id:
        raise ValueError("app ref missing repo_id")
    if not remainder:
        raise ValueError("app ref missing app_path")

    app_path: str
    ref: Optional[str]
    if "@" in remainder:
        app_path, ref = remainder.rsplit("@", 1)
        if not ref:
            raise ValueError("app ref missing ref after '@'")
    else:
        app_path, ref = remainder, None

    if not app_path:
        raise ValueError("app ref missing app_path")

    return AppRef(repo_id=repo_id, app_path=app_path, ref=ref)
