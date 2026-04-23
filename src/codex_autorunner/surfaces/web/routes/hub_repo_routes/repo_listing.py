from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .....core import hub_read_model as _core_hub_read_model
from .....core.hub_read_model import (
    REPO_LISTING_SECTIONS,
    HubReadModelService,
    get_hub_read_model_service,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher

_DEFAULT_MONOTONIC = _core_hub_read_model._monotonic


def _monotonic() -> float:
    return _DEFAULT_MONOTONIC()


def normalize_repo_listing_sections(raw: Optional[str]) -> set[str]:
    if raw is None:
        return set(REPO_LISTING_SECTIONS)
    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not requested:
        return set(REPO_LISTING_SECTIONS)
    invalid = requested - REPO_LISTING_SECTIONS
    if invalid:
        invalid_text = ", ".join(sorted(invalid))
        allowed_text = ", ".join(sorted(REPO_LISTING_SECTIONS))
        raise ValueError(
            f"Unsupported hub repo sections: {invalid_text}. Allowed: {allowed_text}."
        )
    return requested


class HubRepoListingService(HubReadModelService):
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
    ) -> None:
        super().__init__(
            context,
            repo_projection_provider=enricher,
            prepare_repo_snapshots=mount_manager.refresh_mounts,
        )

    async def list_repos(
        self, *, sections: Optional[set[str]] = None
    ) -> dict[str, object]:
        _core_hub_read_model._monotonic = _monotonic
        return await super().list_repos(sections=sections)

    async def scan_repos(self) -> dict[str, object]:
        _core_hub_read_model._monotonic = _monotonic
        return await super().scan_repos()


def build_hub_repo_listing_router(
    context: HubAppContext,
    mount_manager: HubMountManager,
    enricher: HubRepoEnricher,
) -> APIRouter:
    from fastapi import APIRouter, HTTPException

    router = APIRouter()
    listing_service = get_hub_read_model_service(
        context,
        repo_projection_provider=enricher,
        prepare_repo_snapshots=mount_manager.refresh_mounts,
    )

    @router.get("/hub/repos")
    async def list_repos(sections: Optional[str] = None):
        try:
            requested_sections = normalize_repo_listing_sections(sections)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await listing_service.list_repos(sections=requested_sections)

    @router.post("/hub/repos/scan")
    async def scan_repos():
        return await listing_service.scan_repos()

    @router.post("/hub/jobs/scan")
    async def scan_repos_job():
        from .....core.request_context import get_request_id

        async def _run_scan():
            await listing_service.scan_repos()
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.scan_repos",
            _run_scan,
            request_id=get_request_id(),
        )
        return job.to_dict()

    return router
