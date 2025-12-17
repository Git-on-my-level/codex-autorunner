"""
GitHub integration routes.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..github import GitHubError, GitHubService


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
            return await asyncio.to_thread(_github(request).status_payload)
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/github/pr")
    async def github_pr(request: Request):
        svc = _github(request)
        try:
            status = await asyncio.to_thread(svc.status_payload)
            return {
                "status": "ok",
                "git": status.get("git"),
                "pr": status.get("pr"),
                "links": status.get("pr_links"),
                "link": status.get("link") or {},
            }
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/link-issue")
    async def github_link_issue(request: Request, payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        issue = payload.get("issue")
        if not issue:
            raise HTTPException(status_code=400, detail="Missing issue")
        try:
            state = await asyncio.to_thread(_github(request).link_issue, str(issue))
            return {"status": "ok", "link": state}
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/spec/from-issue")
    async def github_spec_from_issue(request: Request, payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        issue = payload.get("issue")
        if not issue:
            raise HTTPException(status_code=400, detail="Missing issue")

        doc_chat = request.app.state.doc_chat
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        if doc_chat.doc_busy("spec"):
            raise HTTPException(
                status_code=409, detail="Doc chat already running for spec"
            )

        svc = _github(request)
        try:
            prompt, link_state = await asyncio.to_thread(
                svc.build_spec_prompt_from_issue, str(issue)
            )
            doc_req = doc_chat.parse_request(
                "spec", {"message": prompt, "stream": False}
            )
            async with doc_chat.doc_lock("spec"):
                result = await doc_chat.execute(doc_req)
            if result.get("status") != "ok":
                detail = result.get("detail") or "SPEC generation failed"
                raise HTTPException(status_code=500, detail=detail)
            result["github"] = {"issue": link_state.get("issue")}
            return result
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/pr/sync")
    async def github_pr_sync(request: Request, payload: Optional[dict] = None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        if payload.get("mode") is not None:
            raise HTTPException(
                status_code=400,
                detail="Repo mode does not support worktrees; create a hub worktree repo instead.",
            )
        draft = bool(payload.get("draft", True))
        title = payload.get("title")
        body = payload.get("body")
        try:
            return await asyncio.to_thread(
                _github(request).sync_pr,
                draft=draft,
                title=str(title) if title else None,
                body=str(body) if body else None,
            )
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return router
