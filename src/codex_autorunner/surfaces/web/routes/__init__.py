"""
Modular API routes for the codex-autorunner server.

This package splits monolithic api_routes.py into focused modules:
- base: Index, WebSocket terminal, and general endpoints
- agents: Agent harness models and event streaming
- app_server: App-server thread registry endpoints
- workspace: Optional workspace docs (active_context/decisions/spec)
- flows: Flow runtime management (start/stop/resume/status/events/artifacts)
- messages: Inbox/message wrappers over ticket_flow dispatch + reply histories
- repos: Run control (start/stop/resume/reset)
- sessions: Terminal session registry endpoints
- settings: Session settings for autorunner overrides
- file_chat: Unified file chat (tickets + workspace docs)
- voice: Voice transcription and config
- terminal_images: Terminal image uploads
"""

from pathlib import Path

from fastapi import APIRouter

from .agents import build_agents_routes
from .analytics import build_analytics_routes
from .app_server import build_app_server_routes
from .archive import build_archive_routes
from .base import build_base_routes, build_frontend_routes
from .file_chat import build_file_chat_routes
from .flows import build_flow_routes
from .messages import build_messages_routes
from .repos import build_repos_routes
from .review import build_review_routes
from .sessions import build_sessions_routes
from .settings import build_settings_routes
from .system import build_system_routes
from .terminal_images import build_terminal_image_routes
from .usage import build_usage_routes
from .voice import build_voice_routes
from .workspace import build_workspace_routes


def build_repo_router(static_dir: Path) -> APIRouter:
    """
    Build complete API router by combining all route modules.

    Args:
        static_dir: Path to static assets directory

    Returns:
        Combined APIRouter with all endpoints
    """
    router = APIRouter()

    # Include all route modules
    router.include_router(build_base_routes(static_dir))
    router.include_router(build_analytics_routes())
    router.include_router(build_archive_routes())
    router.include_router(build_agents_routes())
    router.include_router(build_app_server_routes())
    router.include_router(build_workspace_routes())
    router.include_router(build_flow_routes())
    router.include_router(build_file_chat_routes())
    router.include_router(build_messages_routes())
    router.include_router(build_repos_routes())
    router.include_router(build_review_routes())
    router.include_router(build_sessions_routes())
    router.include_router(build_settings_routes())
    router.include_router(build_system_routes())
    router.include_router(build_terminal_image_routes())
    router.include_router(build_usage_routes())
    router.include_router(build_voice_routes())
    # Include frontend routes last to avoid shadowing API routes
    router.include_router(build_frontend_routes(static_dir))

    return router


__all__ = ["build_repo_router"]
