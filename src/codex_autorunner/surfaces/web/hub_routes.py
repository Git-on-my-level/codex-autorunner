from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from .schemas import HubJobResponse

if TYPE_CHECKING:
    from .app_state import HubAppContext


def register_simple_hub_routes(app: FastAPI, context: "HubAppContext") -> None:
    @app.get("/hub/version")
    def hub_version():
        return {"asset_version": app.state.asset_version}

    @app.get("/hub/jobs/{job_id}", response_model=HubJobResponse)
    async def get_hub_job(job_id: str):
        job = await context.job_manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()
