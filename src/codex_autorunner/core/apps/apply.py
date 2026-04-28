from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Optional

import yaml

from ...tickets.frontmatter import (
    render_markdown_frontmatter,
    split_markdown_frontmatter,
)
from ...tickets.lint import parse_ticket_index
from ..config import HubConfig
from ..utils import atomic_write
from .indexer import is_probably_installed_app_id
from .install import (
    AppInstallConflictError,
    AppInstallError,
    InstalledAppInfo,
    get_installed_app,
    install_app,
)


class AppApplyError(Exception):
    """Raised when applying an app entrypoint ticket fails."""


@dataclasses.dataclass(frozen=True)
class AppliedAppTicket:
    app: InstalledAppInfo
    ticket_path: Path
    ticket_index: int
    source_ref: str
    app_inputs: dict[str, Any]
    apply_inputs_path: Path
    install_changed: bool


def apply_app_entrypoint(
    repo_root: Path,
    app_ref_or_id: str,
    *,
    hub_config: Optional[HubConfig] = None,
    hub_root: Optional[Path] = None,
    at: Optional[int] = None,
    next_index: bool = True,
    suffix: Optional[str] = None,
    app_inputs: Optional[dict[str, Any]] = None,
) -> AppliedAppTicket:
    installed_app, install_changed = _resolve_app_install(
        repo_root,
        app_ref_or_id,
        hub_config=hub_config,
        hub_root=hub_root,
    )
    manifest = installed_app.manifest
    if manifest.entrypoint is None:
        raise AppApplyError(
            f"Installed app {installed_app.app_id} does not declare entrypoint.template"
        )

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    if ticket_dir.exists() and not ticket_dir.is_dir():
        raise AppApplyError(f"Ticket dir is not a directory: {ticket_dir}")
    ticket_dir.mkdir(parents=True, exist_ok=True)

    if at is None and not next_index:
        raise AppApplyError("Specify --at or leave --next enabled to pick an index.")
    if at is not None and at < 1:
        raise AppApplyError("Ticket index must be >= 1.")

    existing_indices = _collect_ticket_indices(ticket_dir)
    if at is None:
        ticket_index = _next_available_ticket_index(existing_indices)
    else:
        ticket_index = at
        if ticket_index in existing_indices:
            raise AppApplyError(
                f"Ticket index {ticket_index} already exists. "
                "Choose another index or open a gap."
            )

    normalized_suffix = _normalize_ticket_suffix(suffix)
    width = max(3, max([len(str(i)) for i in existing_indices + [ticket_index]]))
    ticket_path = ticket_dir / _ticket_filename(
        ticket_index,
        suffix=normalized_suffix,
        width=width,
    )
    if ticket_path.exists():
        raise AppApplyError(f"Ticket already exists: {ticket_path}")

    parsed_inputs = dict(app_inputs or {})
    _validate_declared_inputs(installed_app, parsed_inputs)

    content = _render_ticket_content(installed_app, parsed_inputs)
    atomic_write(ticket_path, content)

    apply_inputs_path = installed_app.paths.state_root / "apply-inputs.json"
    apply_state = {
        "app_id": installed_app.app_id,
        "app_version": installed_app.app_version,
        "app_source": _source_ref_from_lock(installed_app),
        "app_commit": installed_app.lock.commit_sha,
        "app_manifest_sha": installed_app.lock.manifest_sha,
        "app_bundle_sha": installed_app.lock.bundle_sha,
        "ticket_path": str(ticket_path.relative_to(repo_root)),
        "ticket_index": ticket_index,
        "inputs": parsed_inputs,
    }
    atomic_write(
        apply_inputs_path,
        json.dumps(apply_state, indent=2, sort_keys=True) + "\n",
    )

    return AppliedAppTicket(
        app=installed_app,
        ticket_path=ticket_path,
        ticket_index=ticket_index,
        source_ref=_source_ref_from_lock(installed_app),
        app_inputs=parsed_inputs,
        apply_inputs_path=apply_inputs_path,
        install_changed=install_changed,
    )


def _resolve_app_install(
    repo_root: Path,
    app_ref_or_id: str,
    *,
    hub_config: Optional[HubConfig],
    hub_root: Optional[Path],
) -> tuple[InstalledAppInfo, bool]:
    if is_probably_installed_app_id(app_ref_or_id):
        installed_app = get_installed_app(repo_root, app_ref_or_id)
        if installed_app is None:
            raise AppApplyError(f"Installed app not found: {app_ref_or_id}")
        return installed_app, False

    if hub_config is None or hub_root is None:
        raise AppApplyError(
            "Applying an app source ref requires hub configuration context."
        )

    try:
        result = install_app(hub_config, hub_root, repo_root, app_ref_or_id)
    except (AppInstallConflictError, AppInstallError) as exc:
        raise AppApplyError(str(exc)) from exc
    return result.app, result.changed


def _render_ticket_content(
    installed_app: InstalledAppInfo,
    app_inputs: dict[str, Any],
) -> str:
    entrypoint = installed_app.manifest.entrypoint
    assert entrypoint is not None
    template_path = installed_app.paths.bundle_root / entrypoint.path
    if not template_path.exists() or not template_path.is_file():
        raise AppApplyError(f"Entrypoint template not found: {template_path}")

    raw = template_path.read_text(encoding="utf-8")
    fm_yaml, body = split_markdown_frontmatter(raw)
    if fm_yaml is None:
        raise AppApplyError(
            f"Entrypoint template is missing YAML frontmatter: {template_path}"
        )
    try:
        frontmatter = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        raise AppApplyError(
            f"Entrypoint template frontmatter is invalid YAML: {exc}"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise AppApplyError(
            f"Entrypoint template frontmatter must be a YAML mapping: {template_path}"
        )

    frontmatter["app"] = installed_app.app_id
    frontmatter["app_version"] = installed_app.app_version
    frontmatter["app_source"] = _source_ref_from_lock(installed_app)
    frontmatter["app_commit"] = installed_app.lock.commit_sha
    frontmatter["app_manifest_sha"] = installed_app.lock.manifest_sha
    frontmatter["app_bundle_sha"] = installed_app.lock.bundle_sha

    return render_markdown_frontmatter(
        frontmatter,
        _append_app_inputs_section(body, app_inputs),
    )


def _validate_declared_inputs(
    installed_app: InstalledAppInfo,
    app_inputs: dict[str, Any],
) -> None:
    declared = installed_app.manifest.inputs
    if not declared:
        return

    unknown = sorted(set(app_inputs) - set(declared))
    if unknown:
        raise AppApplyError(
            "Unknown app inputs: " + ", ".join(unknown) + ". "
            "Pass only keys declared in the app manifest."
        )

    missing_required = sorted(
        key for key, spec in declared.items() if spec.required and key not in app_inputs
    )
    if missing_required:
        raise AppApplyError(
            "Missing required app inputs: " + ", ".join(missing_required)
        )


def _append_app_inputs_section(body: str, app_inputs: dict[str, Any]) -> str:
    normalized_body = body.lstrip("\n").rstrip()
    section_lines = ["## App Inputs", ""]
    if app_inputs:
        for key in sorted(app_inputs):
            section_lines.append(f"- `{key}`: `{_format_input_value(app_inputs[key])}`")
    else:
        section_lines.append("- None provided.")
    section = "\n".join(section_lines)
    if not normalized_body:
        return section
    return normalized_body + "\n\n" + section


def _format_input_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _source_ref_from_lock(installed_app: InstalledAppInfo) -> str:
    lock = installed_app.lock
    return f"{lock.source_repo_id}:{lock.source_path}@{lock.source_ref}"


def _collect_ticket_indices(ticket_dir: Path) -> list[int]:
    indices: list[int] = []
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return indices
    for path in ticket_dir.iterdir():
        if not path.is_file():
            continue
        idx = parse_ticket_index(path.name)
        if idx is None:
            continue
        indices.append(idx)
    return indices


def _next_available_ticket_index(existing: list[int]) -> int:
    if not existing:
        return 1
    seen = set(existing)
    candidate = 1
    while candidate in seen:
        candidate += 1
    return candidate


def _ticket_filename(index: int, *, suffix: str, width: int) -> str:
    return f"TICKET-{index:0{width}d}{suffix}.md"


def _normalize_ticket_suffix(suffix: Optional[str]) -> str:
    if not suffix:
        return ""
    cleaned = suffix.strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned:
        raise AppApplyError("Ticket suffix may not include path separators.")
    if not cleaned.startswith("-"):
        return f"-{cleaned}"
    return cleaned


__all__ = [
    "AppApplyError",
    "AppliedAppTicket",
    "apply_app_entrypoint",
]
