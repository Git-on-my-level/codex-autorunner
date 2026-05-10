from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ....core.interaction_inbox import (
    InteractionInboxError,
    InteractionInboxStore,
    InteractionPrompt,
    default_interaction_inbox_path,
)
from ..schemas import InteractionPromptResponseRequest


def _store_for_request(request: Request) -> InteractionInboxStore:
    return InteractionInboxStore(
        default_interaction_inbox_path(request.app.state.engine.state_path)
    )


def _http_error(exc: InteractionInboxError) -> HTTPException:
    status = {
        "not_found": 404,
        "unauthorized_actor": 403,
        "expired": 409,
        "already_answered": 409,
        "invalid_response": 400,
        "invalid_prompt": 400,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail=exc.message)


def _serialize_prompt(prompt: InteractionPrompt) -> dict[str, Any]:
    payload = prompt.to_dict()
    payload["prompt_id"] = payload["id"]
    return payload


def build_interaction_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/interactions/prompts")
    def list_interaction_prompts(
        request: Request,
        status: Optional[str] = Query(default="pending"),
        kind: Optional[str] = Query(default=None),
    ):
        statuses = None if not status else [item.strip() for item in status.split(",")]
        prompts = _store_for_request(request).list_prompts(statuses=statuses, kind=kind)
        return {"prompts": [_serialize_prompt(prompt) for prompt in prompts]}

    @router.post("/api/interactions/prompts/{prompt_id}/response")
    def respond_to_interaction_prompt(
        request: Request,
        prompt_id: str,
        payload: InteractionPromptResponseRequest,
    ):
        try:
            prompt = _store_for_request(request).respond(
                prompt_id,
                actor_user_id=payload.actor_user_id,
                response=payload.response,
            )
        except InteractionInboxError as exc:
            raise _http_error(exc) from exc
        return {"prompt": _serialize_prompt(prompt)}

    return router
