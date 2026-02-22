from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

import httpx
import typer
import yaml

from ....core.config import (
    CONFIG_FILENAME,
    ConfigError,
    HubConfig,
    RepoConfig,
    _normalize_base_path,
    find_nearest_hub_config_path,
    load_hub_config,
    load_repo_config,
)
from ....core.utils import (
    RepoNotFoundError,
    find_repo_root,
    find_template_repo,
    is_within,
)
from ....manifest import load_manifest
from ....tickets.files import list_ticket_paths, read_ticket, safe_relpath
from ....tickets.lint import lint_ticket_directory, parse_ticket_index

if TYPE_CHECKING:
    from ....core.hub import HubSupervisor

logger = logging.getLogger("codex_autorunner.cli")


def get_car_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("codex-autorunner")
    except Exception:
        return "unknown"


def normalize_base_path(base_path: Optional[str]) -> str:
    return _normalize_base_path(base_path)


def resolve_auth_token(env_name: str) -> Optional[str]:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def require_auth_token(env_name: Optional[str]) -> Optional[str]:
    if not env_name:
        return None
    token = resolve_auth_token(env_name)
    if not token:
        raise_exit(
            f"server.auth_token_env is set to {env_name}, but the environment variable is missing."
        )
    return token


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def enforce_bind_auth(host: str, token_env: str) -> None:
    if is_loopback_host(host):
        return
    if resolve_auth_token(token_env):
        return
    raise_exit(
        "Refusing to bind to a non-loopback host without server.auth_token_env set."
    )


def request_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    token_env: Optional[str] = None,
) -> dict:
    headers = None
    if token_env:
        token = require_auth_token(token_env)
        headers = {"Authorization": f"Bearer {token}"}
    response = httpx.request(
        method,
        url,
        json=payload,
        timeout=2.0,
        headers=headers,
        follow_redirects=True,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        preview = ""
        try:
            preview = (response.text or "")[:200].strip()
        except Exception:
            preview = ""
        hint = f" body_preview={preview!r}" if preview else ""
        raise httpx.HTTPError(
            f"Non-JSON response from {response.url!s} (status={response.status_code}).{hint}"
        ) from exc
    return data if isinstance(data, dict) else {}


def request_form_json(
    method: str,
    url: str,
    form: Optional[dict] = None,
    token_env: Optional[str] = None,
    *,
    force_multipart: bool = False,
) -> dict:
    headers = None
    if token_env:
        token = require_auth_token(token_env)
        headers = {"Authorization": f"Bearer {token}"}
    data = form
    files: Optional[list[tuple[str, Any]]] = None
    if force_multipart:
        data = form or {}
        files = []
    response = httpx.request(
        method,
        url,
        data=data,
        files=files,
        timeout=5.0,
        headers=headers,
        follow_redirects=True,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        preview = ""
        try:
            preview = (response.text or "")[:200].strip()
        except Exception:
            preview = ""
        hint = f" body_preview={preview!r}" if preview else ""
        raise httpx.HTTPError(
            f"Non-JSON response from {response.url!s} (status={response.status_code}).{hint}"
        ) from exc
    return data if isinstance(data, dict) else {}


def require_optional_feature(
    *, feature: str, deps: list[tuple[str, str]], extra: Optional[str] = None
) -> None:
    from ....core.optional_dependencies import require_optional_dependencies

    try:
        require_optional_dependencies(feature=feature, deps=deps, extra=extra)
    except ConfigError as exc:
        raise_exit(str(exc), cause=exc)


def build_hub_supervisor(config: HubConfig) -> HubSupervisor:
    from ....agents.registry import validate_agent_id
    from ....core.hub import HubSupervisor
    from ....integrations.agents import build_backend_orchestrator
    from ....integrations.agents.wiring import (
        build_agent_backend_factory,
        build_app_server_supervisor_factory,
    )

    return HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
        agent_id_validator=validate_agent_id,
    )


def fetch_template_with_scan(template: str, ctx, hub: Optional[Path]):
    from ....core.config import ConfigError
    from ....core.git_utils import GitError
    from ....core.templates import (
        NetworkUnavailableError,
        RefNotFoundError,
        RepoNotConfiguredError,
        TemplateNotFoundError,
        fetch_template,
        get_scan_record,
        parse_template_ref,
        scan_lock,
    )
    from ....integrations.templates.scan_agent import (
        TemplateScanError,
        TemplateScanRejectedError,
        format_template_scan_rejection,
        run_template_scan,
    )

    try:
        parsed = parse_template_ref(template)
    except ValueError as exc:
        raise_exit(str(exc), cause=exc)

    repo_cfg = find_template_repo(ctx.config, parsed.repo_id)
    if repo_cfg is None:
        raise_exit(f"Template repo not configured: {parsed.repo_id}")

    hub_config_path = resolve_hub_config_path_for_cli(ctx.repo_root, hub)
    if hub_config_path is None:
        try:
            hub_config = load_hub_config(ctx.repo_root)
            hub_root = hub_config.root
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
    else:
        hub_root = hub_config_path.parent.parent.resolve()

    try:
        fetched = fetch_template(
            repo=repo_cfg, hub_root=hub_root, template_ref=template
        )
    except NetworkUnavailableError as exc:
        raise_exit(
            f"{str(exc)}\n"
            "Hint: Fetch once while online to seed the cache. "
            "If this template is untrusted, scanning may also require a working agent backend."
        )
    except (
        RepoNotConfiguredError,
        RefNotFoundError,
        TemplateNotFoundError,
        GitError,
    ) as exc:
        raise_exit(str(exc), cause=exc)

    scan_record = None
    if not fetched.trusted:
        with scan_lock(hub_root, fetched.blob_sha):
            scan_record = get_scan_record(hub_root, fetched.blob_sha)
            if scan_record is None:
                try:
                    scan_record = asyncio.run(
                        run_template_scan(ctx=ctx, template=fetched)
                    )
                except TemplateScanRejectedError as exc:
                    raise_exit(str(exc), cause=exc)
                except TemplateScanError as exc:
                    raise_exit(str(exc), cause=exc)
            elif scan_record.decision != "approve":
                raise_exit(format_template_scan_rejection(scan_record))

    return fetched, scan_record, hub_root


def raise_exit(message: str, *, cause: Optional[BaseException] = None) -> NoReturn:
    typer.echo(message, err=True)
    if cause is not None:
        raise typer.Exit(code=1) from cause
    raise typer.Exit(code=1)


def require_repo_config(repo: Optional[Path], hub: Optional[Path]):
    from ....core.runtime import RuntimeContext

    try:
        repo_root = find_repo_root(repo or Path.cwd())
    except RepoNotFoundError as exc:
        raise_exit("No .git directory found for repo commands.", cause=exc)
    try:
        config = load_repo_config(repo_root, hub_path=hub)
    except ConfigError as exc:
        raise_exit(str(exc), cause=exc)
    return RuntimeContext(repo_root=repo_root, config=config)


def require_hub_config(path: Optional[Path]) -> HubConfig:
    try:
        if path is None:
            return load_hub_config(Path.cwd())
        candidate = path
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        return load_hub_config(candidate)
    except ConfigError as exc:
        raise_exit(str(exc), cause=exc)


def require_templates_enabled(config: RepoConfig) -> None:
    if not config.templates.enabled:
        raise_exit(
            "Templates are disabled. Set templates.enabled=true in the hub config to enable."
        )


def resolve_hub_config_path_for_cli(
    repo_root: Path, hub: Optional[Path]
) -> Optional[Path]:
    if hub:
        candidate = hub
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        return candidate if candidate.exists() else None
    return find_nearest_hub_config_path(repo_root)


def build_server_url(
    config, path: str, *, base_path_override: Optional[str] = None
) -> str:
    base_path = (
        _normalize_base_path(base_path_override)
        if base_path_override is not None
        else (config.server_base_path or "")
    )
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}{path}"


def resolve_ticket_dir(repo_root: Path, ticket_dir: Optional[Path]) -> Path:
    if ticket_dir is None:
        return repo_root / ".codex-autorunner" / "tickets"
    if ticket_dir.is_absolute():
        return ticket_dir
    return repo_root / ticket_dir


def collect_ticket_indices(ticket_dir: Path) -> list[int]:
    from ....tickets.lint import parse_ticket_index

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


def next_available_ticket_index(existing: list[int]) -> int:
    if not existing:
        return 1
    seen = set(existing)
    candidate = 1
    while candidate in seen:
        candidate += 1
    return candidate


def ticket_filename(index: int, *, suffix: str, width: int) -> str:
    return f"TICKET-{index:0{width}d}{suffix}.md"


def normalize_ticket_suffix(suffix: Optional[str]) -> str:
    if not suffix:
        return ""
    cleaned = suffix.strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned:
        raise_exit("Ticket suffix may not include path separators.")
    if not cleaned.startswith("-"):
        return f"-{cleaned}"
    return cleaned


def apply_agent_override(content: str, agent: str) -> str:
    from ....tickets.frontmatter import split_markdown_frontmatter

    fm_yaml, body = split_markdown_frontmatter(content)
    if fm_yaml is None:
        raise_exit("Template is missing YAML frontmatter; cannot set agent.")
    try:
        data = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        raise_exit(f"Template frontmatter is invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise_exit("Template frontmatter must be a YAML mapping to set agent.")
    data["agent"] = agent
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---{body}"


def resolve_hub_repo_root(config: HubConfig, repo_id: str) -> Path:
    manifest = load_manifest(config.manifest_path, config.root)
    entry = manifest.get(repo_id)
    if entry is None:
        raise_exit(f"Repo id not found in hub manifest: {repo_id}")
    repo_root = (config.root / entry.path).resolve()
    if not repo_root.exists():
        raise_exit(f"Repo path does not exist: {repo_root}")
    return repo_root


def parse_renumber(value: Optional[str]) -> Optional[dict[str, int]]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    pairs: dict[str, int] = {}
    for part in parts:
        if "=" not in part:
            raise_exit("Renumber format must be start=<n>,step=<n>.")
        key, raw = [segment.strip() for segment in part.split("=", 1)]
        if key not in ("start", "step"):
            raise_exit("Renumber keys must be start and step.")
        try:
            value_int = int(raw)
        except ValueError as exc:
            raise_exit(f"Renumber {key} must be an integer.", cause=exc)
        pairs[key] = value_int
    if "start" not in pairs or "step" not in pairs:
        raise_exit("Renumber requires both start=<n> and step=<n>.")
    if pairs["start"] < 1 or pairs["step"] < 1:
        raise_exit("Renumber start/step must be >= 1.")
    return pairs


def ticket_lint_details(ticket_dir: Path) -> dict[str, list[str]]:
    details: dict[str, list[str]] = {
        "invalid_filenames": [],
        "duplicate_indices": [],
        "frontmatter": [],
    }
    if not ticket_dir.exists():
        return details

    ticket_root = ticket_dir.parent
    for path in sorted(ticket_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name == "AGENTS.md":
            continue
        if parse_ticket_index(path.name) is None:
            rel_path = safe_relpath(path, ticket_root)
            details["invalid_filenames"].append(
                f"{rel_path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
            )

    details["duplicate_indices"].extend(lint_ticket_directory(ticket_dir))

    ticket_paths = list_ticket_paths(ticket_dir)
    for path in ticket_paths:
        _, ticket_errors = read_ticket(path)
        for err in ticket_errors:
            details["frontmatter"].append(
                f"{path.relative_to(path.parent.parent)}: {err}"
            )

    return details


def validate_tickets(ticket_dir: Path) -> list[str]:
    errors: list[str] = []

    if not ticket_dir.exists():
        return errors

    details = ticket_lint_details(ticket_dir)
    errors.extend(details["invalid_filenames"])
    errors.extend(details["duplicate_indices"])
    errors.extend(details["frontmatter"])

    return errors


def render_ticket_markdown(frontmatter: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---\n\n{body.rstrip()}\n"


def guard_unregistered_hub_repo(repo_root: Path, hub: Optional[Path]) -> None:
    hub_config_path = resolve_hub_config_path_for_cli(repo_root, hub)
    if hub_config_path is None:
        return
    try:
        hub_config = load_hub_config(hub_config_path)
    except ConfigError as exc:
        raise_exit(str(exc), cause=exc)

    repo_root = repo_root.resolve()
    under_repos = is_within(hub_config.repos_root, repo_root)
    under_worktrees = is_within(hub_config.worktrees_root, repo_root)
    if not (under_repos or under_worktrees):
        return

    manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    if manifest.get_by_path(hub_config.root, repo_root) is not None:
        return

    lines = [
        "Repo not registered in hub manifest. Run car hub scan or create via car hub worktree create.",
        f"Detected hub root: {hub_config.root}",
        f"Repo path: {repo_root}",
        "Runs won't show up in the hub UI until registered.",
    ]
    if under_worktrees:
        lines.append(
            "Hint: Worktree names should look like <base_repo_id>--<branch> under "
            f"{hub_config.worktrees_root}"
        )
    raise_exit("\n".join(lines))


_DURATION_PART_RE = re.compile(r"(\d+)([smhdw])")


def parse_bool_text(value: str, *, flag: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise_exit(f"Invalid value for {flag}: {value!r} (expected true|false)")


def parse_duration(value: str):
    from datetime import timedelta

    raw = (value or "").strip().lower()
    if not raw:
        raise_exit("Duration must not be empty.")
    matches = list(_DURATION_PART_RE.finditer(raw))
    if not matches or "".join(m.group(0) for m in matches) != raw:
        raise_exit(
            f"Invalid duration {value!r}. Use forms like 30m, 2h, 7d, or combined 1h30m."
        )
    total_seconds = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for match in matches:
        total_seconds += int(match.group(1)) * multipliers[match.group(2)]
    if total_seconds <= 0:
        raise_exit("Duration must be greater than zero.")
    return timedelta(seconds=total_seconds)
