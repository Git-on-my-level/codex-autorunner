from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Request

from ....agents.registry import validate_agent_id
from ....core.config import ConfigError, RepoConfig, load_hub_config
from ....core.git_utils import GitError
from ....core.templates import (
    FetchedTemplate,
    NetworkUnavailableError,
    RefNotFoundError,
    RepoNotConfiguredError,
    TemplateNotFoundError,
    fetch_template,
    parse_template_ref,
)
from ....core.templates.scan_cache import TemplateScanRecord, get_scan_record, scan_lock
from ....integrations.templates import (
    TemplateScanError,
    TemplateScanRejectedError,
    format_template_scan_rejection,
    run_template_scan,
)
from ....tickets.files import safe_relpath
from ....tickets.frontmatter import split_markdown_frontmatter
from ....tickets.lint import parse_ticket_index
from ..schemas import (
    TemplateApplyRequest,
    TemplateApplyResponse,
    TemplateFetchRequest,
    TemplateFetchResponse,
    TemplateReposResponse,
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
    if not ticket_dir:
        return repo_root / ".codex-autorunner" / "tickets"
    candidate = Path(ticket_dir)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    allowed_root = (repo_root / ".codex-autorunner").resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "ticket_dir_invalid",
                "Ticket directory must live under .codex-autorunner.",
            ),
        ) from exc
    return candidate


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
        scan_decision=scan_record.to_dict() if scan_record else None,
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
            resolved_dir.mkdir(parents=True, exist_ok=True)
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

        try:
            path.write_text(content, encoding="utf-8")
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
            "scan_decision": scan_record.to_dict() if scan_record else None,
        }
        return TemplateApplyResponse(
            created_path=safe_relpath(path, request.app.state.engine.repo_root),
            index=index,
            filename=filename,
            metadata=metadata,
        )

    return router
