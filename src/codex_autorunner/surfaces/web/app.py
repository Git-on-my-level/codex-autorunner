from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from ...core.config_validation import is_loopback_host
from .app_builders import create_repo_app
from .app_factory import CacheStaticFiles, resolve_allowed_hosts, resolve_auth_token
from .app_state import (
    ServerOverrides,
    apply_hub_context,
    build_app_context,
    build_hub_context,
)
from .hub_routes import register_simple_hub_routes
from .middleware import (
    AuthTokenMiddleware,
    BasePathRouterMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from .routes.auth import build_auth_routes
from .routes.automations import build_automation_routes
from .routes.chat_events import build_chat_surface_event_routes
from .routes.contextspace import build_contextspace_routes
from .routes.feedback_reports import build_feedback_report_routes
from .routes.filebox import build_hub_filebox_routes
from .routes.flows import build_flow_routes
from .routes.hub_chat_read_models import build_hub_chat_read_model_router
from .routes.hub_control_plane import build_hub_control_plane_routes
from .routes.hub_messages import build_hub_messages_routes
from .routes.hub_repos import HubMountManager, build_hub_repo_routes
from .routes.hub_state import build_hub_state_routes
from .routes.interactions import build_interaction_routes
from .routes.pma import build_pma_routes
from .routes.pma_routes import PmaRuntimeState
from .routes.scm_webhooks import build_scm_webhook_routes
from .routes.settings import build_settings_routes
from .routes.system import build_system_routes
from .routes.voice import build_voice_routes
from .services.browser_auth import SESSION_COOKIE_NAME, BrowserAuthStore
from .services.hub_startup import HubStartupService
from .services.pma import create_pma_application_container
from .static_assets import (
    render_web_index_html,
    resolve_web_static_dir,
    web_index_response_headers,
)

__all__ = ["create_hub_app", "create_repo_app"]


def create_hub_app(
    hub_root: Optional[Path] = None,
    base_path: Optional[str] = None,
    endpoint_host: Optional[str] = None,
    endpoint_port: Optional[int] = None,
) -> ASGIApp:
    context = build_hub_context(hub_root, base_path)
    app = FastAPI(redirect_slashes=False)
    apply_hub_context(app, context)
    repo_context = build_app_context(
        context.config.root, context.base_path, context.config
    )
    app.state.engine = repo_context.engine
    app.state.manager = repo_context.manager
    app.state.app_server_threads = repo_context.app_server_threads
    app.state.voice_config = repo_context.voice_config
    app.state.voice_missing_reason = repo_context.voice_missing_reason
    app.state.voice_service = repo_context.voice_service
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.state.orchestration_housekeeping = None
    web_static_dir, web_static_context = resolve_web_static_dir()
    app.state.web_static_dir = web_static_dir
    app.state.web_static_assets_context = web_static_context
    bind_host = endpoint_host or context.config.server_host
    remote_bind = not is_loopback_host(bind_host)
    auth_token = resolve_auth_token(context.config.server_auth_token_env)
    browser_auth_store: Optional[BrowserAuthStore] = None
    if auth_token or remote_bind:
        browser_auth_store = BrowserAuthStore(context.config.root)
        browser_auth_store.ensure_bootstrap_token()
        app.state.browser_auth_store = browser_auth_store
        app.include_router(
            build_auth_routes(
                browser_auth_store,
                cookie_secure=context.config.browser_auth.cookie_secure,
                require_secure_claim=remote_bind,
            )
        )
    web_app_assets_dir = web_static_dir / "_app"
    if web_app_assets_dir.exists():
        app.mount(
            "/_app",
            CacheStaticFiles(directory=web_app_assets_dir),
            name="web-app-assets",
        )
    raw_config = getattr(context.config, "raw", {})
    pma_config = raw_config.get("pma", {}) if isinstance(raw_config, dict) else {}
    if isinstance(pma_config, dict) and pma_config.get("enabled"):
        pma_container = create_pma_application_container(
            runtime_state=PmaRuntimeState(),
            host_state=app.state,
        )
        app.state.pma_container = pma_container
        pma_router = build_pma_routes(container=pma_container)
        app.include_router(pma_router)
        app.state.pma_lane_worker_start = getattr(
            pma_router, "_pma_start_lane_worker", None
        )
        app.state.pma_lane_worker_stop = getattr(
            pma_router, "_pma_stop_lane_worker", None
        )
        app.state.pma_lane_worker_stop_all = getattr(
            pma_router, "_pma_stop_all_lane_workers", None
        )
    app.include_router(build_feedback_report_routes())
    app.include_router(build_scm_webhook_routes())
    app.include_router(build_automation_routes(context))
    app.include_router(build_hub_filebox_routes())
    app.include_router(build_hub_control_plane_routes())
    app.include_router(build_hub_state_routes(context))
    app.include_router(build_chat_surface_event_routes(context))
    app.include_router(build_hub_chat_read_model_router(context))

    app.state.hub_started = False
    app.state.hub_deferred_startup_complete = False
    repo_server_overrides: Optional[ServerOverrides] = None
    if context.config.repo_server_inherit:
        repo_server_overrides = ServerOverrides(
            allowed_hosts=resolve_allowed_hosts(
                context.config.server_host, context.config.server_allowed_hosts
            ),
            allowed_origins=list(context.config.server_allowed_origins),
            auth_token_env=context.config.server_auth_token_env,
        )

    mount_manager = HubMountManager(
        app,
        context,
        lambda repo_path: create_repo_app(
            repo_path,
            server_overrides=repo_server_overrides,
            hub_config=context.config,
        ),
    )

    # Mount lightweight placeholders immediately so repo URLs remain routable
    # without paying the cost of constructing every repo app up front.
    _, records = context.supervisor._manifest_records(manifest_only=True)
    initial_snapshots = [
        SimpleNamespace(
            id=record.repo.id,
            path=record.absolute_path,
            initialized=record.initialized,
            exists_on_disk=record.exists_on_disk,
        )
        for record in records
    ]

    startup_service = HubStartupService(
        context=context,
        mount_manager=mount_manager,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        base_path=base_path,
    )
    app.state.hub_startup_service = startup_service
    app.router.lifespan_context = startup_service.lifespan

    register_simple_hub_routes(app, context)

    app.include_router(build_contextspace_routes())
    app.include_router(build_flow_routes())
    app.include_router(build_interaction_routes())
    app.include_router(build_settings_routes())
    app.include_router(build_voice_routes())
    app.include_router(build_hub_messages_routes(context))
    app.include_router(build_hub_repo_routes(context, mount_manager))

    def _web_index_response():
        index_path = web_static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500,
                detail="Web Hub UI assets missing; run `pnpm web:build`",
            )
        html = render_web_index_html(web_static_dir, base_path=context.base_path)
        return HTMLResponse(
            html, headers=web_index_response_headers(web_static_dir, context.base_path)
        )

    # --- Web Hub SPA shell (deep links / refresh) ---
    # SvelteKit owns URL→screen after load. The hub must return the same index.html
    # for any refreshable path the frontend can emit, or the browser gets FastAPI's
    # JSON 404. Add a hub GET that maps to _web_index_response() when you introduce a
    # new client-only subtree under src/codex_autorunner/web_frontend/src/routes/.
    # Prefer a "{rest:path}" catch-all per top-level segment when that subtree can nest.
    # See surfaces/web/AGENTS.md (section Web Hub SPA shell).

    @app.get("/", include_in_schema=False)
    def hub_index():
        target = f"{context.base_path}/chats" if context.base_path else "/chats"
        return RedirectResponse(target, status_code=307)

    @app.get("/chats", include_in_schema=False)
    @app.get("/chats/{rest:path}", include_in_schema=False)
    @app.get("/repos", include_in_schema=False)
    @app.get("/repos/{repo_id}", include_in_schema=False)
    @app.get("/repos/{repo_id}/tickets", include_in_schema=False)
    @app.get("/repos/{repo_id}/tickets/", include_in_schema=False)
    @app.get("/repos/{repo_id}/tickets/{ticket_id}", include_in_schema=False)
    @app.get("/repos/{repo_id}/tickets/{ticket_id}/", include_in_schema=False)
    @app.get("/tickets", include_in_schema=False)
    @app.get("/tickets/{ticket_id}", include_in_schema=False)
    @app.get("/automations", include_in_schema=False)
    @app.get("/automations/{rest:path}", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/settings/{rest:path}", include_in_schema=False)
    @app.get("/hub", include_in_schema=False)
    def web_hub_index(rest: Optional[str] = None):
        return _web_index_response()

    def _resolve_worktree_parent_repo_id(worktree_id: str) -> str:
        for snapshot in context.supervisor.list_repos():
            if getattr(snapshot, "id", None) != worktree_id:
                continue
            if getattr(snapshot, "kind", None) != "worktree":
                raise HTTPException(
                    status_code=404, detail=f"Worktree not found: {worktree_id}"
                )
            parent_repo_id = str(getattr(snapshot, "worktree_of", "") or "").strip()
            if not parent_repo_id:
                raise HTTPException(
                    status_code=400,
                    detail="Worktree route requires parent repo scope",
                )
            return parent_repo_id
        raise HTTPException(
            status_code=404, detail=f"Worktree not found: {worktree_id}"
        )

    def _require_worktree_scope(repo_id: str, worktree_id: str) -> None:
        parent_repo_id = _resolve_worktree_parent_repo_id(worktree_id)
        if parent_repo_id != repo_id:
            raise HTTPException(
                status_code=404,
                detail=f"Worktree not found in repo scope: {repo_id}/{worktree_id}",
            )

    def _legacy_spa_redirect(target: str) -> RedirectResponse:
        loc = f"{context.base_path}{target}" if context.base_path else target
        return RedirectResponse(loc, status_code=308)

    @app.get("/worktrees", include_in_schema=False)
    def legacy_worktrees_hub_redirect():
        return _legacy_spa_redirect("/repos")

    @app.get("/worktrees/{worktree_id}/tickets/{ticket_id}", include_in_schema=False)
    def legacy_worktree_ticket_redirect(worktree_id: str, ticket_id: str):
        parent_repo_id = _resolve_worktree_parent_repo_id(worktree_id)
        return _legacy_spa_redirect(
            f"/repos/{parent_repo_id}/worktrees/{worktree_id}/tickets/{ticket_id}"
        )

    @app.get("/worktrees/{worktree_id}/tickets", include_in_schema=False)
    @app.get("/worktrees/{worktree_id}/tickets/", include_in_schema=False)
    def legacy_worktree_tickets_index_redirect(worktree_id: str):
        parent_repo_id = _resolve_worktree_parent_repo_id(worktree_id)
        return _legacy_spa_redirect(
            f"/repos/{parent_repo_id}/worktrees/{worktree_id}/tickets"
        )

    @app.get("/worktrees/{worktree_id}", include_in_schema=False)
    def legacy_worktree_hub_redirect(worktree_id: str):
        parent_repo_id = _resolve_worktree_parent_repo_id(worktree_id)
        return _legacy_spa_redirect(f"/repos/{parent_repo_id}/worktrees/{worktree_id}")

    @app.get("/contextspace/{workspace_id}", include_in_schema=False)
    def pma_contextspace_shell(workspace_id: str):
        return _web_index_response()

    @app.get("/repos/{repo_id}/terminal", include_in_schema=False)
    @app.get("/repos/{repo_id}/terminal/{rest:path}", include_in_schema=False)
    def legacy_repo_terminal_redirect(repo_id: str, rest: Optional[str] = None):
        segment = quote(repo_id, safe="")
        target = f"/repos/{segment}"
        loc = f"{context.base_path}{target}" if context.base_path else target
        return RedirectResponse(loc, status_code=307)

    @app.get("/repos/{repo_id}/contextspace", include_in_schema=False)
    def pma_repo_contextspace_shell(repo_id: str):
        return _web_index_response()

    @app.get(
        "/repos/{repo_id}/worktrees/{worktree_id}/contextspace",
        include_in_schema=False,
    )
    def pma_worktree_contextspace_shell(repo_id: str, worktree_id: str):
        _require_worktree_scope(repo_id, worktree_id)
        return _web_index_response()

    @app.get("/repos/{repo_id}/", include_in_schema=False)
    def pma_repo_index_slash(repo_id: str):
        return _web_index_response()

    @app.get("/repos/{repo_id}/worktrees/{worktree_id}", include_in_schema=False)
    @app.get("/repos/{repo_id}/worktrees/{worktree_id}/", include_in_schema=False)
    @app.get(
        "/repos/{repo_id}/worktrees/{worktree_id}/tickets",
        include_in_schema=False,
    )
    @app.get(
        "/repos/{repo_id}/worktrees/{worktree_id}/tickets/",
        include_in_schema=False,
    )
    @app.get(
        "/repos/{repo_id}/worktrees/{worktree_id}/tickets/{ticket_id}",
        include_in_schema=False,
    )
    @app.get(
        "/repos/{repo_id}/worktrees/{worktree_id}/tickets/{ticket_id}/",
        include_in_schema=False,
    )
    def pma_worktree_index(repo_id: str, worktree_id: str):
        _require_worktree_scope(repo_id, worktree_id)
        return _web_index_response()

    app.include_router(build_system_routes())
    mount_manager.mount_initial(initial_snapshots)

    allowed_hosts = resolve_allowed_hosts(
        context.config.server_host, context.config.server_allowed_hosts
    )
    allowed_origins = context.config.server_allowed_origins
    app.state.auth_token = auth_token
    asgi_app: ASGIApp = app
    if auth_token or browser_auth_store is not None:
        asgi_app = AuthTokenMiddleware(
            asgi_app,
            auth_token,
            context.base_path,
            session_cookie_name=SESSION_COOKIE_NAME,
            session_validator=(
                browser_auth_store.validate_session_token
                if browser_auth_store is not None
                else None
            ),
        )
    if context.base_path:
        asgi_app = BasePathRouterMiddleware(asgi_app, context.base_path)
    asgi_app = HostOriginMiddleware(
        asgi_app,
        allowed_hosts,
        allowed_origins,
        base_path=context.base_path,
    )
    asgi_app = RequestIdMiddleware(asgi_app)
    asgi_app = SecurityHeadersMiddleware(asgi_app)

    return asgi_app
