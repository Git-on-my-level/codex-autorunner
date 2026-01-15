"""
GitHub integration routes.
"""

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from ..integrations.github.service import GitHubError, GitHubService
from ..web.schemas import GithubContextRequest, GithubIssueRequest, GithubPrSyncRequest

_GITHUB_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_GITHUB_CACHE_LOCK = asyncio.Lock()
_GITHUB_STATUS_TTL_SECONDS = 20.0
_GITHUB_PR_TTL_SECONDS = 60.0


async def _get_cached_status_payload(
    request: Request,
    *,
    kind: str,
    ttl_seconds: float,
) -> dict:
    repo_root = request.app.state.engine.repo_root.resolve()
    key = (str(repo_root), kind)
    now = time.monotonic()
    task: Optional[asyncio.Task] = None

    async with _GITHUB_CACHE_LOCK:
        entry = _GITHUB_CACHE.get(key) or {}
        value = entry.get("value")
        expires_at = float(entry.get("expires_at", 0) or 0)
        task = entry.get("task")

        if value is not None and expires_at > now:
            return value
        if task is None:
            task = asyncio.create_task(
                asyncio.to_thread(_github(request).status_payload)
            )
            _GITHUB_CACHE[key] = {
                "value": value,
                "expires_at": expires_at,
                "task": task,
            }

    if task is None:
        task = asyncio.create_task(asyncio.to_thread(_github(request).status_payload))
        async with _GITHUB_CACHE_LOCK:
            _GITHUB_CACHE[key] = {"task": task}

    try:
        value = await task
    except Exception:
        async with _GITHUB_CACHE_LOCK:
            current = _GITHUB_CACHE.get(key) or {}
            if current.get("task") is task:
                _GITHUB_CACHE.pop(key, None)
        raise

    async with _GITHUB_CACHE_LOCK:
        _GITHUB_CACHE[key] = {
            "value": value,
            "expires_at": now + ttl_seconds,
        }
    return value


def _github(request) -> GitHubService:
    """Get a GitHubService instance from the request."""
    engine = request.app.state.engine
    return GitHubService(engine.repo_root, raw_config=engine.config.raw)


def build_github_routes() -> APIRouter:
    """Build routes for GitHub integration."""
    router = APIRouter()

    @router.get("/api/github/status")
    async def github_status(request: Request):
        try:
            return await _get_cached_status_payload(
                request,
                kind="status",
                ttl_seconds=_GITHUB_STATUS_TTL_SECONDS,
            )
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/api/github/pr")
    async def github_pr(request: Request):
        try:
            status = await _get_cached_status_payload(
                request,
                kind="pr",
                ttl_seconds=_GITHUB_PR_TTL_SECONDS,
            )
            return {
                "status": "ok",
                "git": status.get("git"),
                "pr": status.get("pr"),
                "links": status.get("pr_links"),
                "link": status.get("link") or {},
            }
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/github/link-issue")
    async def github_link_issue(request: Request, payload: GithubIssueRequest):
        issue = payload.issue
        try:
            state = await asyncio.to_thread(_github(request).link_issue, str(issue))
            return {"status": "ok", "link": state}
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/github/spec/from-issue")
    async def github_spec_from_issue(request: Request, payload: GithubIssueRequest):
        issue = payload.issue

        doc_chat = request.app.state.doc_chat
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        if doc_chat.doc_busy():
            raise HTTPException(
                status_code=409, detail="Doc chat already running for spec"
            )

        svc = _github(request)
        try:
            prompt, link_state = await asyncio.to_thread(
                svc.build_spec_prompt_from_issue, str(issue)
            )
            doc_req = doc_chat.parse_request(
                {"message": prompt, "stream": False}, kind="spec"
            )
            async with doc_chat.doc_lock():
                result = await doc_chat.execute(doc_req)
            if result.get("status") != "ok":
                detail = result.get("detail") or "SPEC generation failed"
                raise HTTPException(status_code=500, detail=detail)
            result["github"] = {"issue": link_state.get("issue")}
            return result
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/github/pr/sync")
    async def github_pr_sync(request: Request, payload: GithubPrSyncRequest):
        if payload.mode is not None:
            raise HTTPException(
                status_code=400,
                detail="Repo mode does not support worktrees; create a hub worktree repo instead.",
            )
        draft = payload.draft
        title = payload.title
        body = payload.body
        try:
            return await asyncio.to_thread(
                _github(request).sync_pr,
                draft=draft,
                title=str(title) if title else None,
                body=str(body) if body else None,
            )
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/github/context")
    async def github_context(request: Request, payload: GithubContextRequest):
        url = payload.url
        try:
            result = await asyncio.to_thread(
                _github(request).build_context_file_from_url, str(url)
            )
            if not result:
                return {"status": "ok", "injected": False}
            return {"status": "ok", "injected": True, **result}
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
