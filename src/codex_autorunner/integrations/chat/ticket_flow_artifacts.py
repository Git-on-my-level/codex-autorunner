from __future__ import annotations

from pathlib import Path

from ...core.apps import AppArtifactCandidate, collect_before_chat_wrapup_artifacts


def collect_terminal_wrapup_artifacts(
    workspace_root: Path,
    *,
    max_file_size_bytes: int,
) -> tuple[AppArtifactCandidate, ...]:
    return collect_before_chat_wrapup_artifacts(
        workspace_root,
        max_file_size_bytes=max_file_size_bytes,
    )


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
    "render_terminal_notification_with_artifacts",
]
