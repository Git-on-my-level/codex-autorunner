from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from ..core.filebox import outbox_dir

TargetMode = Literal["url", "serve"]

_VIEWPORT_RE = re.compile(r"^\s*(\d+)[xX](\d+)\s*$")


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int


@dataclass(frozen=True)
class RenderTarget:
    mode: TargetMode
    path: Optional[str]
    url: Optional[str] = None
    serve_cmd: Optional[str] = None


DEFAULT_VIEWPORT = Viewport(width=1280, height=720)
DEFAULT_VIEWPORT_TEXT = f"{DEFAULT_VIEWPORT.width}x{DEFAULT_VIEWPORT.height}"


def normalize_target_path(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    raw = path.strip()
    if not raw:
        return "/"
    if not raw.startswith("/"):
        return f"/{raw}"
    return raw


def parse_viewport(raw: Optional[str]) -> Viewport:
    text = (raw or DEFAULT_VIEWPORT_TEXT).strip()
    match = _VIEWPORT_RE.match(text)
    if not match:
        raise ValueError(
            f"Invalid viewport '{raw}'. Expected format WIDTHxHEIGHT (for example: 1280x720)."
        )
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("Viewport width and height must be positive integers.")
    return Viewport(width=width, height=height)


def select_render_target(
    *,
    url: Optional[str],
    serve_cmd: Optional[str],
    path: Optional[str] = None,
) -> RenderTarget:
    normalized_path = normalize_target_path(path)
    has_url = bool((url or "").strip())
    has_serve_cmd = bool((serve_cmd or "").strip())
    if has_url == has_serve_cmd:
        raise ValueError("Provide exactly one of --url or --serve-cmd.")
    if has_url:
        return RenderTarget(
            mode="url",
            url=(url or "").strip(),
            path=normalized_path,
        )
    return RenderTarget(
        mode="serve",
        serve_cmd=(serve_cmd or "").strip(),
        path=normalized_path,
    )


def resolve_out_dir(repo_root: Path, out_dir: Optional[Path]) -> Path:
    if out_dir is None:
        return outbox_dir(repo_root)
    if out_dir.is_absolute():
        return out_dir
    return repo_root / out_dir
