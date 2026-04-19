"""Thin shared helpers for web route tests.

Provides small, focused factory functions that consolidate repeated
FastAPI / HubSupervisor bootstrap sequences across the web route test suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import flows as flow_routes
from codex_autorunner.surfaces.web.routes import messages as messages_routes
from tests.conftest import write_test_config


def build_flow_app(repo_root: Path, monkeypatch) -> FastAPI:
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(repo_root))
    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    return app


def build_messages_app(
    repo_root: Path,
    monkeypatch,
    *,
    repo_id: str = "repo",
) -> FastAPI:
    monkeypatch.setattr(messages_routes, "find_repo_root", lambda: Path(repo_root))
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(repo_root))
    app = FastAPI()
    app.state.repo_id = repo_id
    app.include_router(messages_routes.build_messages_routes())
    app.include_router(flow_routes.build_flow_routes())
    return app


def create_test_hub_supervisor(
    hub_root: Path,
    *,
    cfg: Optional[dict] = None,
) -> HubSupervisor:
    config = cfg if cfg is not None else json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, config)
    return HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )


def enable_pma(hub_root: Path, *, enable_github_polling: bool = False) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if enable_github_polling:
        repo_defaults = cfg.setdefault("repo_defaults", {})
        if isinstance(repo_defaults, dict):
            github = repo_defaults.setdefault("github", {})
            if isinstance(github, dict):
                automation = github.setdefault("automation", {})
                if isinstance(automation, dict):
                    polling = automation.setdefault("polling", {})
                    if isinstance(polling, dict):
                        polling["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def build_pma_hub_app(
    hub_root: Path,
    *,
    enable_github_polling: bool = False,
) -> FastAPI:
    enable_pma(hub_root, enable_github_polling=enable_github_polling)
    return create_hub_app(hub_root)
