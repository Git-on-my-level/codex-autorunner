"""
Modular API routes for the codex-autorunner server.

This package splits the monolithic api_routes.py into focused modules:
- base: Index, state streaming, and general endpoints
- agents: Agent harness models and event streaming
- app_server: App-server thread registry endpoints
- docs: Document management (read/write) and chat
- github: GitHub integration endpoints
- repos: Run control (start/stop/resume/reset)
- runs: Run telemetry and artifacts
- sessions: Terminal session registry endpoints
- settings: Session settings for autorunner overrides
- voice: Voice transcription and config
- terminal_images: Terminal image uploads
"""

from pathlib import Path

from fastapi import APIRouter

from .agents import build_agents_routes
from .app_server import build_app_server_routes
from .base import build_base_routes
from .docs import build_docs_routes
from .github import build_github_routes
from .repos import build_repos_routes
from .review import build_review_routes
from .runs import build_runs_routes
from .sessions import build_sessions_routes
from .settings import build_settings_routes
from .system import build_system_routes
from .terminal_images import build_terminal_image_routes
from .voice import build_voice_routes


def build_repo_router(static_dir: Path) -> APIRouter:
    """
    Build the complete API router by combining all route modules.

    Args:
        static_dir: Path to the static assets directory

    Returns:
        Combined APIRouter with all endpoints
    """
    router = APIRouter()

    # Include all route modules
    router.include_router(build_base_routes(static_dir))
    router.include_router(build_agents_routes())
    router.include_router(build_app_server_routes())
    router.include_router(build_docs_routes())
    router.include_router(build_github_routes())
    router.include_router(build_repos_routes())
    router.include_router(build_review_routes())
    router.include_router(build_runs_routes())
    router.include_router(build_sessions_routes())
    router.include_router(build_settings_routes())
    router.include_router(build_system_routes())
    router.include_router(build_terminal_image_routes())
    router.include_router(build_voice_routes())

    return router


__all__ = ["build_repo_router"]
