from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ..utils import canonicalize_path


def normalize_ticket_flow_input_data(
    repo_root: Path,
    input_data: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized = dict(input_data or {})
    resolved_repo_root = canonicalize_path(repo_root)
    raw_workspace = normalized.get("workspace_root")
    if isinstance(raw_workspace, str) and raw_workspace.strip():
        workspace_root = Path(raw_workspace)
        if not workspace_root.is_absolute():
            workspace_root = resolved_repo_root / workspace_root
        normalized["workspace_root"] = str(canonicalize_path(workspace_root))
    else:
        normalized["workspace_root"] = str(resolved_repo_root)
    return normalized


def resolve_ticket_flow_workspace_root(
    record_input: Mapping[str, Any],
    repo_root: Path,
    *,
    enforce_repo_boundary: bool = False,
) -> Path:
    resolved_repo_root = canonicalize_path(repo_root)
    raw_workspace = record_input.get("workspace_root")
    if isinstance(raw_workspace, str) and raw_workspace.strip():
        workspace_root = Path(raw_workspace)
        if not workspace_root.is_absolute():
            raise ValueError(
                f"ticket_flow run has non-absolute workspace_root: {raw_workspace}"
            )
        resolved_workspace_root = canonicalize_path(workspace_root)
    else:
        resolved_workspace_root = resolved_repo_root
    if enforce_repo_boundary:
        try:
            resolved_workspace_root.relative_to(resolved_repo_root)
        except ValueError as exc:
            raise ValueError(
                f"workspace_root escapes repo boundary: {resolved_workspace_root}"
            ) from exc
    return resolved_workspace_root
