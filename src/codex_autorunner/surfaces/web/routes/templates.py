from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Request

from ....agents.registry import validate_agent_id
from ....core.config import (
    ConfigError,
    RepoConfig,
    load_hub_config,
    load_repo_config,
    update_override_templates,
)
from ....core.git_utils import GitError
from ....core.templates import (
    FetchedTemplate,
    NetworkUnavailableError,
    RefNotFoundError,
    RepoNotConfiguredError,
    TemplateNotFoundError,
    fetch_template,
    inject_provenance,
    parse_template_ref,
)
from ....core.templates.scan_cache import TemplateScanRecord, get_scan_record, scan_lock
from ....integrations.templates import (
    TemplateScanError,
    TemplateScanRejectedError,
    format_template_scan_rejection,
    run_template_scan,
)
from ....tickets.files import normalize_ticket_dir, safe_relpath
from ....tickets.frontmatter import split_markdown_frontmatter
from ....tickets.lint import parse_ticket_index
from ..schemas import (
    TemplateApplyRequest,
    TemplateApplyResponse,
    TemplateFetchRequest,
    TemplateFetchResponse,
    TemplateRepoCreateRequest,
    TemplateReposResponse,
    TemplateRepoUpdateRequest,
)


def _error_detail(code: str, message: str, meta: Optional[dict] = None) -> dict:
    payload = {"code": code, "message": message}
    if meta:
        payload["meta"] = meta
    return payload


def _require_templates_enabled(config: RepoConfig) -> None:
    if not config.templates.enabled:
        raise HTTPException(
            status_code=403,
            detail=_error_detail(
                "templates_disabled",
                "Templates are disabled. Set templates.enabled=true in the hub config to enable.",
            ),
        )


def _find_template_repo(config: RepoConfig, repo_id: str):
    for repo in config.templates.repos:
        if repo.id == repo_id:
            return repo
    return None


def _resolve_hub_root(repo_root: Path) -> Path:
    try:
        hub_config = load_hub_config(repo_root)
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_error_detail(
                "hub_config_error",
                str(exc),
            ),
        ) from exc
    return hub_config.root


def _reload_repo_config(request: Request) -> RepoConfig:
    engine = request.app.state.engine
    try:
        new_config = load_repo_config(engine.repo_root)
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("config_reload_failed", str(exc)),
        ) from exc
    # RuntimeContext stores config on a private attribute.
    engine._config = new_config
    request.app.state.config = new_config
    return new_config


def _normalize_required_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must be a string"),
        )
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must not be empty"),
        )
    if "\n" in cleaned or "\r" in cleaned:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must be single-line"),
        )
    return cleaned


def _normalize_optional_string(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must be a string"),
        )
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must not be empty"),
        )
    if "\n" in cleaned or "\r" in cleaned:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", f"{field} must be single-line"),
        )
    return cleaned


def _validate_repo_url(url: str) -> None:
    if any(ch.isspace() for ch in url):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("validation_error", "url must not contain whitespace"),
        )
    # Keep this intentionally permissive: https://, ssh://, git@host:org/repo.git, etc.
    if "://" not in url and not url.startswith("git@"):
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "validation_error",
                "url must look like a git remote (expected '://...' or 'git@...')",
            ),
        )


def _repos_to_dicts(repos) -> list[dict]:
    return [
        {
            "id": repo.id,
            "url": repo.url,
            "trusted": bool(repo.trusted),
            "default_ref": repo.default_ref,
        }
        for repo in repos
    ]


async def _fetch_template_with_scan(
    template: str, request: Request
) -> tuple[FetchedTemplate, Optional[TemplateScanRecord], Path]:
    try:
        parsed = parse_template_ref(template)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("template_ref_invalid", str(exc)),
        ) from exc

    config: RepoConfig = request.app.state.config
    repo_cfg = _find_template_repo(config, parsed.repo_id)
    if repo_cfg is None:
        raise HTTPException(
            status_code=404,
            detail=_error_detail(
                "template_repo_missing",
                f"Template repo not configured: {parsed.repo_id}",
            ),
        )

    hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
    try:
        fetched = fetch_template(
            repo=repo_cfg,
            hub_root=hub_root,
            template_ref=template,
        )
    except NetworkUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=_error_detail(
                "template_network_unavailable",
                str(exc),
            ),
        ) from exc
    except (RepoNotConfiguredError, RefNotFoundError, TemplateNotFoundError) as exc:
        raise HTTPException(
            status_code=404,
            detail=_error_detail("template_not_found", str(exc)),
        ) from exc
    except GitError as exc:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("template_git_error", str(exc)),
        ) from exc

    scan_record: Optional[TemplateScanRecord] = None
    if not fetched.trusted:
        with scan_lock(hub_root, fetched.blob_sha):
            scan_record = get_scan_record(hub_root, fetched.blob_sha)
            if scan_record is None:
                try:
                    scan_record = await run_template_scan(
                        ctx=request.app.state.engine, template=fetched
                    )
                except TemplateScanRejectedError as exc:
                    raise HTTPException(
                        status_code=403,
                        detail=_error_detail("template_scan_rejected", str(exc)),
                    ) from exc
                except TemplateScanError as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=_error_detail("template_scan_failed", str(exc)),
                    ) from exc
            elif scan_record.decision != "approve":
                raise HTTPException(
                    status_code=403,
                    detail=_error_detail(
                        "template_scan_rejected",
                        format_template_scan_rejection(scan_record),
                    ),
                )

    return fetched, scan_record, hub_root


def _resolve_ticket_dir(repo_root: Path, ticket_dir: Optional[str]) -> Path:
    try:
        return normalize_ticket_dir(repo_root, ticket_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("ticket_dir_invalid", str(exc)),
        ) from exc


def _collect_ticket_indices(ticket_dir: Path) -> list[int]:
    indices: list[int] = []
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return indices
    for (
        path
    ) in (
        ticket_dir.iterdir()
    ):  # codeql[py/path-injection] validated by normalize_ticket_dir
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
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "ticket_suffix_invalid",
                "Ticket suffix may not include path separators.",
            ),
        )
    if not cleaned.startswith("-"):
        return f"-{cleaned}"
    return cleaned


def _apply_agent_override(content: str, agent: str) -> str:
    fm_yaml, body = split_markdown_frontmatter(content)
    if fm_yaml is None:
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "template_frontmatter_missing",
                "Template is missing YAML frontmatter; cannot set agent.",
            ),
        )
    try:
        data = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "template_frontmatter_invalid",
                f"Template frontmatter is invalid YAML: {exc}",
            ),
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "template_frontmatter_invalid",
                "Template frontmatter must be a YAML mapping to set agent.",
            ),
        )
    data["agent"] = agent
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---{body}"


def _format_fetch_response(
    fetched: FetchedTemplate, scan_record: Optional[TemplateScanRecord]
) -> TemplateFetchResponse:
    return TemplateFetchResponse(
        content=fetched.content,
        repo_id=fetched.repo_id,
        path=fetched.path,
        ref=fetched.ref,
        commit_sha=fetched.commit_sha,
        blob_sha=fetched.blob_sha,
        trusted=fetched.trusted,
        scan_decision=(
            scan_record.to_dict(include_evidence=False) if scan_record else None
        ),
    )


def build_templates_routes() -> APIRouter:
    router = APIRouter(prefix="/api/templates", tags=["templates"])

    @router.get("/repos", response_model=TemplateReposResponse)
    def list_template_repos(request: Request):
        config: RepoConfig = request.app.state.config
        return TemplateReposResponse(
            enabled=config.templates.enabled,
            repos=[
                {
                    "id": repo.id,
                    "url": repo.url,
                    "trusted": repo.trusted,
                    "default_ref": repo.default_ref,
                }
                for repo in config.templates.repos
            ],
        )

    @router.post("/repos", response_model=TemplateReposResponse)
    def add_template_repo(request: Request, payload: TemplateRepoCreateRequest):
        config: RepoConfig = request.app.state.config
        repo_id = _normalize_required_string(payload.id, "id")
        url = _normalize_required_string(payload.url, "url")
        _validate_repo_url(url)
        default_ref = _normalize_required_string(payload.default_ref, "default_ref")
        trusted = bool(payload.trusted)

        if any(repo.id == repo_id for repo in config.templates.repos):
            raise HTTPException(
                status_code=409,
                detail=_error_detail(
                    "template_repo_conflict", f"Template repo already exists: {repo_id}"
                ),
            )

        updated = _repos_to_dicts(config.templates.repos)
        updated.append(
            {
                "id": repo_id,
                "url": url,
                "trusted": trusted,
                "default_ref": default_ref,
            }
        )
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        update_override_templates(hub_root, updated)
        new_config = _reload_repo_config(request)
        return TemplateReposResponse(
            enabled=new_config.templates.enabled,
            repos=_repos_to_dicts(new_config.templates.repos),
        )

    @router.put("/repos/{repo_id}", response_model=TemplateReposResponse)
    def update_template_repo(
        request: Request, repo_id: str, payload: TemplateRepoUpdateRequest
    ):
        config: RepoConfig = request.app.state.config
        existing = _find_template_repo(config, repo_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=_error_detail(
                    "template_repo_missing", f"Template repo not configured: {repo_id}"
                ),
            )
        updates = payload.model_dump(exclude_unset=True)
        url = (
            _normalize_optional_string(updates.get("url"), "url")
            if "url" in updates
            else None
        )
        if url is not None:
            _validate_repo_url(url)
        default_ref = (
            _normalize_optional_string(updates.get("default_ref"), "default_ref")
            if "default_ref" in updates
            else None
        )
        trusted_val = updates.get("trusted") if "trusted" in updates else None
        if trusted_val is not None and not isinstance(trusted_val, bool):
            raise HTTPException(
                status_code=400,
                detail=_error_detail("validation_error", "trusted must be boolean"),
            )
        trusted = bool(trusted_val) if trusted_val is not None else None

        updated: list[dict] = []
        for repo in config.templates.repos:
            if repo.id != repo_id:
                updated.append(
                    {
                        "id": repo.id,
                        "url": repo.url,
                        "trusted": bool(repo.trusted),
                        "default_ref": repo.default_ref,
                    }
                )
                continue
            updated.append(
                {
                    "id": repo.id,
                    "url": url if url is not None else repo.url,
                    "trusted": trusted if trusted is not None else bool(repo.trusted),
                    "default_ref": (
                        default_ref if default_ref is not None else repo.default_ref
                    ),
                }
            )

        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        update_override_templates(hub_root, updated)
        new_config = _reload_repo_config(request)
        return TemplateReposResponse(
            enabled=new_config.templates.enabled,
            repos=_repos_to_dicts(new_config.templates.repos),
        )

    @router.delete("/repos/{repo_id}", response_model=TemplateReposResponse)
    def delete_template_repo(request: Request, repo_id: str):
        config: RepoConfig = request.app.state.config
        if _find_template_repo(config, repo_id) is None:
            raise HTTPException(
                status_code=404,
                detail=_error_detail(
                    "template_repo_missing", f"Template repo not configured: {repo_id}"
                ),
            )
        updated = [
            {
                "id": repo.id,
                "url": repo.url,
                "trusted": bool(repo.trusted),
                "default_ref": repo.default_ref,
            }
            for repo in config.templates.repos
            if repo.id != repo_id
        ]
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        update_override_templates(hub_root, updated)
        new_config = _reload_repo_config(request)
        return TemplateReposResponse(
            enabled=new_config.templates.enabled,
            repos=_repos_to_dicts(new_config.templates.repos),
        )

    @router.post("/fetch", response_model=TemplateFetchResponse)
    async def fetch_template_route(request: Request, payload: TemplateFetchRequest):
        config: RepoConfig = request.app.state.config
        _require_templates_enabled(config)
        fetched, scan_record, _hub_root = await _fetch_template_with_scan(
            payload.template, request
        )
        return _format_fetch_response(fetched, scan_record)

    @router.post("/apply", response_model=TemplateApplyResponse)
    async def apply_template_route(request: Request, payload: TemplateApplyRequest):
        config: RepoConfig = request.app.state.config
        _require_templates_enabled(config)
        fetched, scan_record, _hub_root = await _fetch_template_with_scan(
            payload.template, request
        )

        resolved_dir = _resolve_ticket_dir(
            request.app.state.engine.repo_root, payload.ticket_dir
        )
        if resolved_dir.exists() and not resolved_dir.is_dir():
            raise HTTPException(
                status_code=400,
                detail=_error_detail(
                    "ticket_dir_invalid",
                    f"Ticket dir is not a directory: {resolved_dir}",
                ),
            )
        try:
            resolved_dir.mkdir(
                parents=True, exist_ok=True
            )  # codeql[py/path-injection] validated by normalize_ticket_dir
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_error_detail("ticket_dir_error", str(exc)),
            ) from exc

        if payload.at is None and not payload.next_index:
            raise HTTPException(
                status_code=400,
                detail=_error_detail(
                    "ticket_index_missing",
                    "Specify at or leave next_index enabled to pick an index.",
                ),
            )
        if payload.at is not None and payload.at < 1:
            raise HTTPException(
                status_code=400,
                detail=_error_detail(
                    "ticket_index_invalid", "Ticket index must be >= 1."
                ),
            )

        existing_indices = _collect_ticket_indices(resolved_dir)
        if payload.at is None:
            index = _next_available_ticket_index(existing_indices)
        else:
            index = payload.at
            if index in existing_indices:
                raise HTTPException(
                    status_code=409,
                    detail=_error_detail(
                        "ticket_index_conflict",
                        f"Ticket index {index} already exists.",
                    ),
                )

        normalized_suffix = _normalize_ticket_suffix(payload.suffix)
        width = max(3, max([len(str(i)) for i in existing_indices + [index]]))
        filename = _ticket_filename(index, suffix=normalized_suffix, width=width)
        path = resolved_dir / filename
        if path.exists():
            raise HTTPException(
                status_code=409,
                detail=_error_detail(
                    "ticket_exists",
                    f"Ticket already exists: {path}",
                ),
            )

        content = fetched.content
        if payload.set_agent:
            if payload.set_agent != "user":
                try:
                    validate_agent_id(payload.set_agent)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=_error_detail("agent_invalid", str(exc)),
                    ) from exc
            content = _apply_agent_override(content, payload.set_agent)

        if payload.include_provenance:
            content = inject_provenance(content, fetched, scan_record)

        try:
            path.write_text(
                content, encoding="utf-8"
            )  # codeql[py/path-injection] validated by normalize_ticket_dir
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_error_detail("ticket_write_failed", str(exc)),
            ) from exc

        metadata = {
            "repo_id": fetched.repo_id,
            "path": fetched.path,
            "ref": fetched.ref,
            "commit_sha": fetched.commit_sha,
            "blob_sha": fetched.blob_sha,
            "trusted": fetched.trusted,
            "scan_decision": (
                scan_record.to_dict(include_evidence=False) if scan_record else None
            ),
        }
        return TemplateApplyResponse(
            created_path=safe_relpath(path, request.app.state.engine.repo_root),
            index=index,
            filename=filename,
            metadata=metadata,
        )

    return router
