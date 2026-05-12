from __future__ import annotations

from collections.abc import Iterable

from scripts.web_ui_screens import (
    DEFAULT_ROUTES,
    DEFAULT_VIEWPORTS,
    PRIMARY_LOADING_MARKERS,
)

from tests.web_ui_lab.scenario_models import (
    EvidenceArtifactSet,
    ExpectedReadModelFixture,
    ExpectedScreenBehavior,
    LoadingBehavior,
    ScenarioTag,
    SeedFixtureKind,
    Viewport,
    ViewportName,
    WebUiScenario,
)

_VIEWPORT_NAMES = tuple(ViewportName)
if len(_VIEWPORT_NAMES) != len(DEFAULT_VIEWPORTS):
    raise ValueError("web UI viewport labels must match DEFAULT_VIEWPORTS")

DEFAULT_WEB_UI_VIEWPORTS: tuple[Viewport, ...] = tuple(
    Viewport(name, width, height)
    for name, (width, height) in zip(_VIEWPORT_NAMES, DEFAULT_VIEWPORTS)
)

SCRIPT_ROUTE_PACK: dict[str, str] = dict(DEFAULT_ROUTES)
_PRIMARY_LOADING_MARKERS = tuple(PRIMARY_LOADING_MARKERS)


def _evidence(scenario_id: str) -> EvidenceArtifactSet:
    return EvidenceArtifactSet(
        screenshot=f"{scenario_id}/screenshot.png",
        accessibility_snapshot=f"{scenario_id}/a11y_snapshot.json",
        dom_summary=f"{scenario_id}/dom_summary.json",
        console_log=f"{scenario_id}/console.log",
        network_failures=f"{scenario_id}/network_failures.json",
        layout_diagnostics=f"{scenario_id}/layout_diagnostics.json",
    )


def _scenario(
    *,
    scenario_id: str,
    title: str,
    route_name: str,
    seed_fixture: SeedFixtureKind,
    visible_landmarks: tuple[str, ...],
    read_model_routes: tuple[str, ...] = (),
    api_routes: tuple[str, ...] = (),
    frontend_mappers: tuple[str, ...] = (),
    tags: tuple[ScenarioTag, ...] = (ScenarioTag.FAST,),
    loading_behavior: LoadingBehavior = LoadingBehavior.CLEARS,
    empty_state_landmarks: tuple[str, ...] = (),
    notes: str = "",
) -> WebUiScenario:
    return WebUiScenario(
        scenario_id=scenario_id,
        title=title,
        route_name=route_name,
        route_path=SCRIPT_ROUTE_PACK[route_name],
        viewports=DEFAULT_WEB_UI_VIEWPORTS,
        seed_fixture=seed_fixture,
        screen=ExpectedScreenBehavior(
            visible_landmarks=visible_landmarks,
            loading_behavior=loading_behavior,
            loading_markers=_PRIMARY_LOADING_MARKERS,
            empty_state_landmarks=empty_state_landmarks,
        ),
        read_models=ExpectedReadModelFixture(
            fixture_kind=seed_fixture,
            read_model_routes=read_model_routes,
            api_routes=api_routes,
            frontend_mappers=frontend_mappers,
        ),
        evidence=_evidence(scenario_id),
        tags=tags,
        notes=notes,
    )


WEB_UI_SCENARIOS: tuple[WebUiScenario, ...] = (
    _scenario(
        scenario_id="empty_hub_home",
        title="Empty hub shell",
        route_name="hub",
        seed_fixture=SeedFixtureKind.EMPTY_HUB,
        visible_landmarks=("Hub", "Workspace"),
        read_model_routes=(
            "/hub/read-models/repo-worktree/topology?kind=all&limit=200",
            "/hub/read-models/repo-worktree/runtime?kind=all&limit=200",
        ),
        api_routes=(
            "/hub/messages?sections=inbox,managed_threads,pma_files_detail,automation,action_queue,freshness",
        ),
        frontend_mappers=("readModelViewModels.ts", "repoWorktree.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        empty_state_landmarks=("No repositories", "No active runs"),
    ),
    _scenario(
        scenario_id="repo_index_seeded",
        title="Seeded repo index",
        route_name="repos",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Repositories", "smoke-repo"),
        read_model_routes=(
            "/hub/read-models/repo-worktree/topology?kind=repo&limit=200",
            "/hub/tickets",
        ),
        frontend_mappers=("repoWorktree.ts", "ticket.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="repo_detail_seeded_ticket_state",
        title="Seeded repo detail with ticket state",
        route_name="repo-detail",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Repository", "smoke-repo", "Repo tickets"),
        read_model_routes=(
            "/hub/read-models/repos/smoke-repo/detail",
            "/repos/smoke-repo/api/flows/runs?flow_type=ticket_flow",
        ),
        frontend_mappers=("repoWorktree.ts", "ticket.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="repo_tickets_seeded_queue",
        title="Repo ticket queue",
        route_name="repo-tickets",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Repo ticket queue", "TICKET-350-smoke-fixture"),
        read_model_routes=("/hub/tickets?repo=smoke-repo",),
        api_routes=("/repos/smoke-repo/api/flows/ticket_flow/tickets",),
        frontend_mappers=("scopedTicketQueue.ts", "ticket.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="repo_ticket_detail_seeded",
        title="Repo ticket detail",
        route_name="repo-ticket-detail",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Workspace ticket detail", "Ticket context"),
        read_model_routes=(
            "/hub/read-models/tickets/TICKET-350-smoke-fixture?owner_kind=repo&owner_id=smoke-repo",
        ),
        api_routes=(
            "/repos/smoke-repo/api/flows/ticket_flow/tickets/TICKET-350-smoke-fixture",
        ),
        frontend_mappers=("ticket.ts", "TicketViews.svelte"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER),
    ),
    _scenario(
        scenario_id="worktree_detail_running",
        title="Worktree detail under running PMA pressure",
        route_name="worktree-detail",
        seed_fixture=SeedFixtureKind.PMA_RUNNING,
        visible_landmarks=("Repo worktree detail", "smoke-repo--review"),
        read_model_routes=(
            "/hub/read-models/worktrees/smoke-repo--review/detail",
            "/repos/smoke-repo--review/api/flows/runs?flow_type=ticket_flow",
        ),
        frontend_mappers=("repoWorktree.ts", "pmaChat.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        loading_behavior=LoadingBehavior.MAY_STREAM,
    ),
    _scenario(
        scenario_id="worktree_contextspace_seeded",
        title="Worktree contextspace",
        route_name="worktree-contextspace",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Workspace contextspace", "Durable shared context"),
        api_routes=("/repos/smoke-repo--review/api/contextspace",),
        frontend_mappers=("routes.ts", "ScopedContextspacePage.svelte"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER),
    ),
    _scenario(
        scenario_id="worktree_tickets_large_queue",
        title="Worktree tickets under large queue pressure",
        route_name="worktree-tickets",
        seed_fixture=SeedFixtureKind.LARGE_LIST_WINDOWING,
        visible_landmarks=("Worktree ticket queue", "TICKET-350-smoke-fixture"),
        read_model_routes=("/hub/tickets?worktree=smoke-repo--review",),
        api_routes=("/repos/smoke-repo--review/api/flows/ticket_flow/tickets",),
        frontend_mappers=("scopedTicketQueue.ts", "ticket.ts"),
        tags=(
            ScenarioTag.FAST,
            ScenarioTag.BROWSER,
            ScenarioTag.CRITICAL,
            ScenarioTag.LARGE_STATE,
        ),
    ),
    _scenario(
        scenario_id="global_tickets_large_queue",
        title="Global tickets under large queue pressure",
        route_name="tickets",
        seed_fixture=SeedFixtureKind.LARGE_LIST_WINDOWING,
        visible_landmarks=("Tickets", "All tickets"),
        read_model_routes=("/hub/tickets",),
        frontend_mappers=("ticket.ts",),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.LARGE_STATE),
    ),
    _scenario(
        scenario_id="global_ticket_detail_final",
        title="Global ticket detail with final PMA state",
        route_name="ticket-detail",
        seed_fixture=SeedFixtureKind.PMA_FINAL,
        visible_landmarks=("Workspace ticket detail", "Ticket context"),
        read_model_routes=("/hub/read-models/tickets/TICKET-350-smoke-fixture",),
        frontend_mappers=("ticket.ts", "pmaChat.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="chat_index_seeded",
        title="Chat list",
        route_name="chat",
        seed_fixture=SeedFixtureKind.CHAT_LIST_DETAIL,
        visible_landmarks=("Chats", "Search chats, repos, tickets"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/patches",
        ),
        api_routes=("/hub/pma/threads",),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="pma_new_chat_creation",
        title="PMA new chat creation",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_NEW_CHAT,
        visible_landmarks=("Chats", "Search chats, repos, tickets"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/patches",
        ),
        api_routes=("/hub/pma/threads",),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        empty_state_landmarks=("New chat",),
    ),
    _scenario(
        scenario_id="pma_queued_turn_visibility",
        title="PMA queued turn visibility",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_QUEUED,
        visible_landmarks=("Chats", "Queued follow-up"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        loading_behavior=LoadingBehavior.MAY_STREAM,
    ),
    _scenario(
        scenario_id="pma_running_progress_timeline",
        title="PMA running progress timeline",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_RUNNING,
        visible_landmarks=("Chats", "Reading files", "rg"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        loading_behavior=LoadingBehavior.MAY_STREAM,
    ),
    _scenario(
        scenario_id="pma_final_assistant_delivery",
        title="PMA final assistant delivery",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_FINAL,
        visible_landmarks=("Chats", "Final answer delivered."),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="pma_failed_error_turn_display",
        title="PMA failed turn display",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_ERROR,
        visible_landmarks=("Chats", "Turn failed"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="pma_approval_request_affordance",
        title="PMA approval request affordance",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_APPROVAL,
        visible_landmarks=("Chats", "Run pnpm install?"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="pma_interrupt_reconciliation",
        title="PMA interrupt affordance and reconciliation",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_INTERRUPT,
        visible_landmarks=("Chats", "Change direction now"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
        loading_behavior=LoadingBehavior.MAY_STREAM,
    ),
    _scenario(
        scenario_id="pma_attachment_rendering",
        title="PMA attachment rendering",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_ATTACHMENT,
        visible_landmarks=("Chats", "Review this file", "report.md"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="pma_duplicate_delivery_snapshot_repair",
        title="PMA duplicate delivery and snapshot repair",
        route_name="chat",
        seed_fixture=SeedFixtureKind.PMA_DUPLICATE_REPAIR,
        visible_landmarks=("Chats", "Summary complete.", "Snapshot repair"),
        read_model_routes=(
            "/hub/chat/index?view=active&limit=25",
            "/hub/read-models/chats/chat-smoke-1/timeline",
        ),
        frontend_mappers=("pmaChat.ts", "readModelClients.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER, ScenarioTag.CRITICAL),
    ),
    _scenario(
        scenario_id="worktree_index_seeded",
        title="Worktree index",
        route_name="worktrees",
        seed_fixture=SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        visible_landmarks=("Repo worktree variants", "smoke-repo--review"),
        read_model_routes=(
            "/hub/read-models/repo-worktree/topology?kind=worktree&limit=200",
            "/hub/read-models/repo-worktree/runtime?kind=worktree&limit=200",
        ),
        frontend_mappers=("repoWorktree.ts",),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER),
    ),
    _scenario(
        scenario_id="contextspace_local_pending",
        title="Local contextspace while PMA is pending",
        route_name="contextspace",
        seed_fixture=SeedFixtureKind.PMA_PENDING,
        visible_landmarks=("Workspace contextspace", "Durable shared context"),
        api_routes=("/repos/local/api/contextspace", "/hub/pma/docs"),
        frontend_mappers=("ScopedContextspacePage.svelte",),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER),
        loading_behavior=LoadingBehavior.MAY_STREAM,
    ),
    _scenario(
        scenario_id="settings_models_loaded",
        title="Settings and model catalog",
        route_name="settings",
        seed_fixture=SeedFixtureKind.EMPTY_HUB,
        visible_landmarks=("Settings", "Models"),
        api_routes=("/api/session/settings", "/hub/pma/agents"),
        frontend_mappers=("ticketSettingsContract.ts", "modelPickers.ts"),
        tags=(ScenarioTag.FAST, ScenarioTag.BROWSER),
    ),
)


def iter_scenarios() -> Iterable[WebUiScenario]:
    return iter(WEB_UI_SCENARIOS)
