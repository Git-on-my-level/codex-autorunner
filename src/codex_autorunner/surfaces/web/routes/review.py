"""
Review workflow routes.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..review import ReviewBusyError, ReviewError, ReviewService
from ..schemas import (
    ReviewControlResponse,
    ReviewStartRequest,
    ReviewStatusResponse,
)


def _review(request: Request) -> ReviewService:
    """Get a ReviewService instance from request."""
    manager = getattr(request.app.state, "review_manager", None)
    if manager is None:
        engine = request.app.state.engine
        manager = ReviewService(
            engine,
            app_server_supervisor=getattr(
                request.app.state, "app_server_supervisor", None
            ),
            opencode_supervisor=getattr(request.app.state, "opencode_supervisor", None),
            logger=getattr(request.app.state, "logger", None),
        )
        request.app.state.review_manager = manager
    return manager


def build_review_routes() -> APIRouter:
    """Build routes for review workflow."""
    router = APIRouter()

    @router.get("/api/review/status")
    async def review_status(request: Request):
        try:
            service = _review(request)
            status = service.status()
            return ReviewStatusResponse(review=status)
        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/review/start")
    async def review_start(request: Request, payload: ReviewStartRequest):
        try:
            service = _review(request)
            state = service.start(payload=payload.model_dump(exclude_none=True))
            return ReviewControlResponse(
                status=state.get("status", "unknown"),
                detail="Review started",
            )
        except ReviewBusyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/review/stop")
    async def review_stop(request: Request):
        try:
            service = _review(request)
            state = service.stop()
            return ReviewControlResponse(
                status=state.get("status", "unknown"),
                detail="Review stopped",
            )
        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/review/reset")
    async def review_reset(request: Request):
        try:
            service = _review(request)
            state = service.reset()
            return ReviewControlResponse(
                status=state.get("status", "idle"),
                detail="Review state reset",
            )
        except ReviewBusyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/api/review/artifact")
    async def review_artifact(
        request: Request,
        kind: str = Query(
            ..., description="final_report|workflow_log|scratchpad_bundle"
        ),
    ):
        try:
            service = _review(request)
            status = service.status()

            mapping = {
                "final_report": status.get("final_output_path"),
                "workflow_log": status.get("run_dir"),
                "scratchpad_bundle": status.get("scratchpad_bundle_path"),
            }

            raw_path = mapping.get(kind)
            if not raw_path:
                raise HTTPException(status_code=404, detail="Artifact not found")

            target = Path(raw_path).expanduser().resolve()
            allowed_root = request.app.state.engine.repo_root.resolve()

            try:
                target.relative_to(allowed_root)
                if ".codex-autorunner" not in target.parts:
                    raise HTTPException(status_code=403, detail="Access denied")
            except ValueError:
                raise HTTPException(status_code=403, detail="Access denied") from None

            if not target.exists():
                raise HTTPException(status_code=404, detail="Artifact not found")

            if kind == "workflow_log" and target.is_dir():
                target = target / "review.log"

            media_type = "text/plain"
            if target.suffix == ".md":
                media_type = "text/markdown"
            elif target.suffix == ".zip":
                media_type = "application/zip"

            return FileResponse(target, media_type=media_type, filename=target.name)

        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
