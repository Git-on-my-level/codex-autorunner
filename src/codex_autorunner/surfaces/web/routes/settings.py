from fastapi import APIRouter, HTTPException, Request

from ....core.state import load_state
from ..schemas import SessionSettingsRequest, SessionSettingsResponse
from ..services import session_settings as session_settings_service
from ..services.session_settings import SessionSettingsError


def build_settings_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/session/settings", response_model=SessionSettingsResponse)
    def get_session_settings(request: Request):
        state = load_state(request.app.state.engine.state_path)
        return session_settings_service.session_settings_from_state(state).to_response()

    @router.post("/api/session/settings", response_model=SessionSettingsResponse)
    def update_session_settings(request: Request, payload: SessionSettingsRequest):
        updates = payload.model_dump(exclude_unset=True)
        manager = request.app.state.manager
        registry = request.app.state.app_server_threads
        try:
            result = session_settings_service.update_session_settings(
                state_path=request.app.state.engine.state_path,
                updates=updates,
                is_run_active=lambda: bool(manager.running),
                reset_thread=registry.reset_thread,
            )
        except SessionSettingsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return result.settings.to_response()

    return router
