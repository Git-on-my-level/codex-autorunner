from __future__ import annotations

import re
import shutil
from pathlib import Path

from ...core.apps import AppArtifactCandidate, collect_before_chat_wrapup_artifacts
from ...core.filebox import ensure_structure, outbox_dir, sanitize_filename

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def collect_terminal_wrapup_artifacts(
    workspace_root: Path,
    *,
    max_file_size_bytes: int,
) -> tuple[AppArtifactCandidate, ...]:
    return collect_before_chat_wrapup_artifacts(
        workspace_root,
        max_file_size_bytes=max_file_size_bytes,
    )


def _safe_filename_part(value: str) -> str:
    normalized = _UNSAFE_FILENAME_CHARS.sub("-", value.strip()).strip(".-")
    return normalized or "artifact"


def _artifact_outbox_filename(artifact: AppArtifactCandidate) -> str:
    source_name = sanitize_filename(Path(artifact.relative_path).name)
    app_prefix = _safe_filename_part(artifact.app_id)
    return sanitize_filename(f"{app_prefix}-{source_name}")


def _collision_safe_path(folder: Path, filename: str) -> Path:
    safe_name = sanitize_filename(filename)
    candidate = folder / safe_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        next_candidate = folder / f"{stem}-{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def publish_terminal_wrapup_artifacts_to_outbox(
    workspace_root: Path,
    artifacts: tuple[AppArtifactCandidate, ...],
) -> tuple[Path, ...]:
    if not artifacts:
        return ()

    ensure_structure(workspace_root)
    target_dir = outbox_dir(workspace_root)
    published: list[Path] = []
    for artifact in artifacts:
        try:
            if not artifact.absolute_path.is_file():
                continue
            target_path = _collision_safe_path(
                target_dir,
                _artifact_outbox_filename(artifact),
            )
            shutil.copyfile(artifact.absolute_path, target_path)
            published.append(target_path)
        except (OSError, ValueError):
            continue
    return tuple(published)


def render_terminal_notification_with_artifacts(
    message: str,
    artifacts: tuple[AppArtifactCandidate, ...],
    *,
    attachment_delivery_supported: bool,
) -> str:
    if not artifacts or attachment_delivery_supported:
        return message
    lines = [
        message,
        "",
        "App artifacts available (file attachment delivery is unavailable on this surface):",
    ]
    for artifact in artifacts:
        lines.append(
            f"- {artifact.app_id}: {artifact.relative_path} ({artifact.absolute_path})"
        )
    return "\n".join(lines)


__all__ = [
    "collect_terminal_wrapup_artifacts",
    "publish_terminal_wrapup_artifacts_to_outbox",
    "render_terminal_notification_with_artifacts",
]
