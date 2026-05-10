"""
Modular API routes for the codex-autorunner server.

This package splits monolithic api_routes.py into focused modules:
- base: WebSocket terminal and general endpoints
- agents: Agent harness models and event streaming
- app_server: App-server thread registry endpoints
- contextspace: Optional contextspace docs (active_context/decisions/spec)
- flows: Flow runtime management (start/stop/resume/status/events/artifacts)
- messages: Inbox/message wrappers over ticket_flow dispatch + reply histories
- repos: Run control (start/stop/resume/reset)
- sessions: Terminal session registry endpoints
- settings: Session settings for autorunner overrides
- file_chat: Unified file chat (tickets + contextspace docs)
- voice: Voice transcription and config
- terminal_images: Terminal image uploads
"""

from fastapi import APIRouter

from .agents import build_agents_routes
from .app_server import build_app_server_routes
from .archive import build_archive_routes
from .base import build_base_routes
from .contextspace import build_contextspace_routes
from .file_chat import build_file_chat_routes
from .filebox import build_filebox_routes
from .flows import build_flow_routes
from .interactions import build_interaction_routes
from .messages import build_messages_routes
from .repos import build_repos_routes
from .sessions import build_sessions_routes
from .settings import build_settings_routes
from .system import build_system_routes
from .templates import build_templates_routes
from .terminal_images import build_terminal_image_routes
from .voice import build_voice_routes


def build_repo_router() -> APIRouter:
    """
    Build complete API router by combining all route modules.

    Returns:
        Combined APIRouter with all endpoints
    """
    router = APIRouter()

    # Include all route modules
    router.include_router(build_base_routes())
    router.include_router(build_archive_routes())
    router.include_router(build_agents_routes())
    router.include_router(build_app_server_routes())
    router.include_router(build_contextspace_routes())
    router.include_router(build_flow_routes())
    router.include_router(build_filebox_routes())
    router.include_router(build_file_chat_routes())
    router.include_router(build_interaction_routes())
    router.include_router(build_messages_routes())
    router.include_router(build_repos_routes())
    router.include_router(build_sessions_routes())
    router.include_router(build_settings_routes())
    router.include_router(build_system_routes())
    router.include_router(build_templates_routes())
    router.include_router(build_terminal_image_routes())
    router.include_router(build_voice_routes())

    return router


__all__ = ["build_repo_router"]
