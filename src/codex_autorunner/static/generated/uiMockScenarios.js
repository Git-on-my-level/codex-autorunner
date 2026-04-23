// GENERATED FILE - do not edit directly. Source: static_src/
function baseRepo(partial) {
    const id = partial.id;
    return {
        path: partial.path ?? `/tmp/car-mock/${id}`,
        display_name: partial.display_name,
        enabled: true,
        auto_run: true,
        worktree_setup_commands: null,
        kind: "base",
        worktree_of: null,
        branch: "main",
        exists_on_disk: true,
        is_clean: true,
        initialized: true,
        init_error: null,
        status: "idle",
        lock_status: "unlocked",
        last_run_id: 42,
        last_exit_code: 0,
        last_run_started_at: "2025-12-10T16:00:00.000Z",
        last_run_finished_at: "2025-12-10T16:12:00.000Z",
        last_run_duration_seconds: 720,
        runner_pid: null,
        effective_destination: { kind: "path", path: `~/work/${id}` },
        mounted: true,
        mount_error: null,
        chat_bound: false,
        has_car_state: true,
        resource_kind: "repo",
        ...partial,
    };
}
function baseWorkspace(partial) {
    return {
        runtime: "mock",
        enabled: true,
        exists_on_disk: true,
        effective_destination: { kind: "path", path: partial.path },
        resource_kind: "agent_workspace",
        ...partial,
    };
}
const isoScan = "2025-12-10T20:00:00.000Z";
const defaultHubData = {
    repos: [],
    agent_workspaces: [],
    last_scan_at: isoScan,
    pinned_parent_repo_ids: [],
};
const defaultUsage = {
    codex_home: "~/.codex",
    status: "ok",
    repos: [],
    unmatched: { events: 0, totals: { total_tokens: 0 } },
};
const defaultChannels = { entries: [] };
function bundle(id, label, description, patch) {
    const hubMerged = {
        ...defaultHubData,
        ...patch.hubData,
        repos: patch.hubData?.repos ?? defaultHubData.repos,
        agent_workspaces: patch.hubData?.agent_workspaces ?? defaultHubData.agent_workspaces,
    };
    return {
        id,
        label,
        description,
        hubData: hubMerged,
        hubUsage: { ...defaultUsage, ...patch.hubUsage },
        channels: { entries: patch.channels?.entries ?? defaultChannels.entries },
        hubVersion: { asset_version: "mock", ...patch.hubVersion },
        systemUpdateStatus: patch.systemUpdateStatus ?? null,
        pmaAgents: patch.pmaAgents ?? null,
    };
}
const mockPmaAgentsList = {
    default: "hermes",
    agents: [
        { id: "hermes", label: "Hermes (mock)", model: "gpt-5" },
        { id: "codex", label: "Codex CLI (mock)" },
    ],
};
const healthyScenario = bundle("healthy", "Healthy multi-repo", "Two base repos, one agent workspace, token usage, pinned parent.", {
    hubData: {
        repos: [
            baseRepo({
                id: "acme-app",
                display_name: "acme / app",
                last_run_id: 1201,
                pma_chat_bound_thread_count: 1,
                discord_chat_bound_thread_count: 0,
            }),
            baseRepo({
                id: "internal-tools",
                display_name: "Internal tools",
                last_run_id: 88,
                status: "idle",
                is_clean: true,
            }),
        ],
        agent_workspaces: [
            baseWorkspace({
                id: "ws-notebooks",
                display_name: "Notebooks",
                path: "/Volumes/agents/ws-notebooks",
            }),
        ],
        last_scan_at: isoScan,
        pinned_parent_repo_ids: ["acme-app"],
    },
    hubUsage: {
        codex_home: "~/.codex",
        status: "ok",
        repos: [
            {
                id: "acme-app",
                events: 42,
                totals: { total_tokens: 1200000, input_tokens: 400000, cached_input_tokens: 200000 },
            },
            {
                id: "internal-tools",
                events: 6,
                totals: { total_tokens: 18000, input_tokens: 9000, cached_input_tokens: 2000 },
            },
        ],
        unmatched: { events: 1, totals: { total_tokens: 200 } },
    },
});
const pmaHealthyScenario = {
    ...healthyScenario,
    id: "pma-healthy",
    label: "Healthy hub + PMA",
    description: "Same as “healthy” but with /hub/pma/agents mocked so the Project Manager toggle works without real PMA config.",
    pmaAgents: mockPmaAgentsList,
};
const scenarios = {
    empty: bundle("empty", "Empty hub", "No repos, no agent workspaces, no channel rows.", {
        hubData: { repos: [], agent_workspaces: [], last_scan_at: null },
    }),
    healthy: healthyScenario,
    "pma-healthy": pmaHealthyScenario,
    running: bundle("running", "Run in progress", "One repo in running state; others idle (for status pills and activity column).", {
        hubData: {
            repos: [
                baseRepo({
                    id: "acme-app",
                    display_name: "acme / app",
                    status: "running",
                    last_run_id: 1305,
                    runner_pid: 99123,
                    is_clean: false,
                    last_run_duration_seconds: null,
                    last_run_started_at: "2025-12-10T20:15:00.000Z",
                    last_run_finished_at: null,
                }),
                baseRepo({
                    id: "docs-site",
                    display_name: "docs-site",
                    status: "idle",
                    is_clean: true,
                    last_run_id: 4,
                    last_run_finished_at: "2025-12-10T18:00:00.000Z",
                }),
            ],
            last_scan_at: isoScan,
        },
        hubUsage: {
            status: "ok",
            codex_home: "~/.codex",
            repos: [
                { id: "acme-app", events: 3, totals: { total_tokens: 95000 } },
                { id: "docs-site", events: 1, totals: { total_tokens: 2000 } },
            ],
        },
    }),
    "worktrees-and-flow": bundle("worktrees-and-flow", "Base + worktrees + ticket flow", "Base repo with worktree children and an active multi-step ticket flow (progress + labels).", {
        hubData: {
            repos: [
                baseRepo({
                    id: "monorepo",
                    display_name: "monorepo",
                    status: "idle",
                    ticket_flow: {
                        status: "running",
                        done_count: 1,
                        total_count: 4,
                        current_step: 1,
                    },
                    ticket_flow_display: {
                        status: "running",
                        status_label: "In progress",
                        status_icon: "⏱",
                        is_active: true,
                        done_count: 1,
                        total_count: 4,
                        run_id: "flow-mock-1",
                    },
                    last_run_id: 900,
                    last_run_finished_at: "2025-12-10T19:00:00.000Z",
                }),
                {
                    ...baseRepo({
                        id: "monorepo--feature-auth",
                        display_name: "monorepo — feature/auth",
                        kind: "worktree",
                        worktree_of: "monorepo",
                        branch: "feature/auth",
                    }),
                    status: "running",
                    last_run_id: 901,
                    last_run_started_at: "2025-12-10T20:10:00.000Z",
                    last_run_finished_at: null,
                    runner_pid: 44112,
                    is_clean: false,
                    ticket_flow: null,
                    ticket_flow_display: null,
                },
                {
                    ...baseRepo({
                        id: "monorepo--chore-bump",
                        display_name: "monorepo — chore/bump",
                        kind: "worktree",
                        worktree_of: "monorepo",
                        branch: "chore/bump",
                    }),
                    status: "idle",
                    is_clean: true,
                    last_run_id: 15,
                    last_run_finished_at: "2025-12-10T12:00:00.000Z",
                },
            ],
            last_scan_at: isoScan,
            pinned_parent_repo_ids: ["monorepo"],
        },
    }),
    "error-and-missing": bundle("error-and-missing", "Init error + missing on disk", "Surface warning/error pills, init error text, and missing repo row.", {
        hubData: {
            repos: [
                baseRepo({
                    id: "good-repo",
                    display_name: "healthy-repo",
                    status: "idle",
                    exists_on_disk: true,
                    initialized: true,
                    is_clean: true,
                    init_error: null,
                    last_run_id: 1,
                    last_run_finished_at: "2025-12-01T10:00:00.000Z",
                }),
                baseRepo({
                    id: "broken-init",
                    display_name: "broken-init",
                    status: "init_error",
                    initialized: false,
                    init_error: "Failed to run git fetch: network unreachable (mock).",
                    exists_on_disk: true,
                    is_clean: null,
                    last_run_id: null,
                    last_run_finished_at: null,
                }),
                baseRepo({
                    id: "zombie",
                    display_name: "zombie (missing)",
                    status: "missing",
                    exists_on_disk: false,
                    initialized: true,
                    is_clean: null,
                    last_run_id: 3,
                    last_run_finished_at: "2024-11-01T00:00:00.000Z",
                }),
            ],
            last_scan_at: isoScan,
        },
    }),
    "channel-directory": bundle("channel-directory", "Chat channel sidebar rows", "Repos stay minimal; channel directory is populated (Discord + PMA-style rows) for the channel column.", {
        hubData: {
            repos: [
                baseRepo({ id: "acme-app", display_name: "acme / app", pma_chat_bound_thread_count: 1 }),
            ],
            last_scan_at: isoScan,
        },
        channels: {
            entries: [
                {
                    key: "d:acme#123",
                    display: "acme#123 — release checklist",
                    repo_id: "acme-app",
                    source: "discord",
                    channel_status: "active",
                    status_label: "Open",
                    seen_at: "2025-12-10T19:00:00.000Z",
                    diff_stats: { insertions: 12, deletions: 3, files_changed: 2 },
                    token_usage: {
                        total_tokens: 24000,
                        input_tokens: 10000,
                        output_tokens: 12000,
                        turn_id: "turn-mock-1",
                        timestamp: "2025-12-10T19:05:00.000Z",
                    },
                    provenance: { platform: "discord", run_id: "r1" },
                },
                {
                    key: "pma:thread-abc",
                    display: "PMA: Thread abc — “Plan December sprint”",
                    repo_id: "acme-app",
                    source: "pma_thread",
                    channel_status: "idle",
                    status_label: "Idle",
                    seen_at: "2025-12-10T12:00:00.000Z",
                    active_thread_id: "thread-abc",
                    token_usage: {
                        total_tokens: 4200,
                        input_tokens: 1200,
                        output_tokens: 2900,
                    },
                    provenance: { platform: "pma", thread_kind: "managed" },
                },
            ],
        },
    }),
    "usage-loading": bundle("usage-loading", "Usage still aggregating", "Hub works but usage summary returns loading (triggers client retry + cached columns).", {
        hubData: {
            repos: [baseRepo({ id: "acme-app", display_name: "acme / app" })],
            last_scan_at: isoScan,
        },
        hubUsage: {
            status: "loading",
            repos: undefined,
            codex_home: "–",
        },
    }),
    "pma-agents-ok": bundle("pma-agents-ok", "PMA enabled (probe + agents list)", "Empty hub but PMA agents list present — use with ?view=pma for the PMA shell without real repos.", {
        hubData: {
            repos: [],
            agent_workspaces: [],
            last_scan_at: null,
            pinned_parent_repo_ids: [],
        },
        pmaAgents: mockPmaAgentsList,
    }),
};
export const UI_MOCK_SCENARIO_ORDER = [
    "empty",
    "healthy",
    "pma-healthy",
    "running",
    "worktrees-and-flow",
    "error-and-missing",
    "channel-directory",
    "usage-loading",
    "pma-agents-ok",
];
export function getUiMockScenarioList() {
    return UI_MOCK_SCENARIO_ORDER.map((id) => {
        const s = scenarios[id];
        return { id: s.id, label: s.label, description: s.description };
    });
}
export function getUiMockScenarioOrDefault(id) {
    const normalized = String(id || "")
        .trim()
        .toLowerCase();
    if (normalized && scenarios[normalized]) {
        return { scenario: scenarios[normalized], resolvedId: normalized, fallback: false };
    }
    if (normalized && !scenarios[normalized]) {
        console.warn(`[uiMock] Unknown scenario ${JSON.stringify(id)} — using "empty". Valid: ${Object.keys(scenarios).join(", ")}`);
        return { scenario: scenarios.empty, resolvedId: "empty", fallback: true };
    }
    return { scenario: scenarios.empty, resolvedId: "empty", fallback: false };
}
