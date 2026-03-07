from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from ..core.filebox import sanitize_filename
from ..core.utils import atomic_write

_SAFE_CHARS_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class ArtifactWriteResult:
    path: Path
    filename: str
    bytes_written: int
    collision_index: int


def _normalize_extension(extension: str) -> str:
    normalized = (extension or "").strip().lower().lstrip(".")
    if not normalized:
        raise ValueError("Artifact extension cannot be empty.")
    return normalized


def _slugify(value: str, *, fallback: str) -> str:
    cleaned = (value or "").strip().lower()
    cleaned = _SAFE_CHARS_RE.sub("-", cleaned).strip("-.")
    if not cleaned:
        return fallback
    return cleaned


def _stem_from_url_or_path(*, url: Optional[str], path_hint: Optional[str]) -> str:
    if path_hint and path_hint.strip():
        candidate = path_hint.strip()
    elif url and url.strip():
        candidate = urlsplit(url.strip()).path
    else:
        candidate = ""

    if not candidate:
        return "index"
    parts = [part for part in candidate.split("/") if part and part not in {".", ".."}]
    if not parts:
        return "index"
    return parts[-1]


def deterministic_artifact_name(
    *,
    kind: str,
    extension: str,
    url: Optional[str] = None,
    path_hint: Optional[str] = None,
    output_name: Optional[str] = None,
) -> str:
    ext = _normalize_extension(extension)
    if output_name is not None:
        explicit = sanitize_filename(output_name)
        explicit_path = Path(explicit)
        if explicit_path.suffix:
            return explicit
        return sanitize_filename(f"{explicit}.{ext}")

    kind_slug = _slugify(kind, fallback="artifact")
    stem_raw = _stem_from_url_or_path(url=url, path_hint=path_hint)
    stem_slug = _slugify(stem_raw, fallback="index")
    return sanitize_filename(f"{kind_slug}-{stem_slug}.{ext}")


def _assert_within_out_dir(out_dir: Path, filename: str) -> Path:
    root = out_dir.resolve()
    safe_name = sanitize_filename(filename)
    candidate = (root / safe_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Resolved artifact path escapes output directory.") from exc
    if candidate.parent != root:
        raise ValueError("Artifact path must not include nested directories.")
    return candidate


def reserve_artifact_path(out_dir: Path, filename: str) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _assert_within_out_dir(out_dir, filename)
    if not base.exists():
        return base, 1

    stem = base.stem
    suffix = base.suffix
    for idx in range(2, 10_001):
        candidate = base.with_name(f"{stem}-{idx}{suffix}")
        if not candidate.exists():
            return candidate, idx
    raise ValueError("Unable to reserve artifact path after 10000 attempts.")


def write_text_artifact(
    *,
    out_dir: Path,
    filename: str,
    content: str,
) -> ArtifactWriteResult:
    path, collision_index = reserve_artifact_path(out_dir, filename)
    atomic_write(path, content)
    bytes_written = len(content.encode("utf-8"))
    return ArtifactWriteResult(
        path=path,
        filename=path.name,
        bytes_written=bytes_written,
        collision_index=collision_index,
    )


def write_json_artifact(
    *,
    out_dir: Path,
    filename: str,
    payload: Any,
) -> ArtifactWriteResult:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return write_text_artifact(out_dir=out_dir, filename=filename, content=rendered)
