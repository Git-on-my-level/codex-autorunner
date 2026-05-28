import { describe, expect, it } from 'vitest';
import type { ChatSummary, ChatRunProgress, RepoSummary, TicketSummary, WorktreeSummary } from './domain';
import { buildRepoWorktreeDetailViewModel, buildRepoWorktreeIndexViewModel } from './repoWorktree';
import { buildSettingsViewModel } from './settings';
import { buildTicketListViewModel } from './ticket';

const runProfile = process.env.RUN_WEB_PAGE_PROFILE === '1';
const describeProfile = runProfile ? describe : describe.skip;
const now = '2026-05-15T12:00:00Z';

describeProfile('manual page view-model profile', () => {
  it('measures large hub page assembly costs', () => {
    const source = syntheticHubSource({
      repoCount: numberFromEnv('WEB_PAGE_PROFILE_REPOS', 90),
      worktreesPerRepo: numberFromEnv('WEB_PAGE_PROFILE_WORKTREES_PER_REPO', 5),
      ticketsPerWorkspace: numberFromEnv('WEB_PAGE_PROFILE_TICKETS_PER_WORKSPACE', 5),
      chatsPerWorkspace: numberFromEnv('WEB_PAGE_PROFILE_CHATS_PER_WORKSPACE', 3),
      runsPerWorkspace: numberFromEnv('WEB_PAGE_PROFILE_RUNS_PER_WORKSPACE', 2)
    });
    const owner = { kind: 'repo' as const, id: 'repo-10' };
    const settingsInput = syntheticSettingsInput(
      numberFromEnv('WEB_PAGE_PROFILE_AGENTS', 40),
      numberFromEnv('WEB_PAGE_PROFILE_MODELS_PER_AGENT', 250)
    );

    const repoIndex = measure(() => buildRepoWorktreeIndexViewModel(source));
    const repoDetail = measure(() => buildRepoWorktreeDetailViewModel(source, 'repo', owner.id));
    const ticketList = measure(() => buildTicketListViewModel(source, owner));
    const globalTicketList = measure(() => buildTicketListViewModel({
      tickets: source.tickets,
      runs: [],
      chats: [],
      artifacts: []
    }));
    const settings = measure(() => buildSettingsViewModel(settingsInput));

    const payload = {
      repos: source.repos.length,
      worktrees: source.worktrees.length,
      tickets: source.tickets.length,
      chats: source.chats.length,
      runs: source.runs.length,
      agents: settingsInput.agents?.length ?? 0,
      modelsPerAgent: settingsInput.modelCatalogs?.agent_0?.length ?? 0,
      repoIndexMs: round(repoIndex.durationMs),
      repoIndexRows: repoIndex.value.rows.length,
      repoDetailMs: round(repoDetail.durationMs),
      repoDetailTickets: repoDetail.value.ticketOverview.total,
      ticketListMs: round(ticketList.durationMs),
      ticketRows: ticketList.value.rows.length,
      globalTicketListMs: round(globalTicketList.durationMs),
      globalTicketRows: globalTicketList.value.rows.length,
      settingsMs: round(settings.durationMs),
      settingsAgents: settings.value.agents.length
    };
    console.info(`WEB_PAGE_PROFILE ${JSON.stringify(payload)}`);
    expect(repoIndex.durationMs).toBeLessThan(80);
    expect(repoDetail.durationMs).toBeLessThan(30);
    expect(ticketList.durationMs).toBeLessThan(30);
    expect(globalTicketList.durationMs).toBeLessThan(80);
    expect(settings.durationMs).toBeLessThan(30);
  }, 30_000);
});

function measure<T>(fn: () => T): { value: T; durationMs: number } {
  const start = performance.now();
  const value = fn();
  return { value, durationMs: performance.now() - start };
}

function numberFromEnv(name: string, fallback: number): number {
  const parsed = Number.parseInt(process.env[name] ?? '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function syntheticHubSource(config: {
  repoCount: number;
  worktreesPerRepo: number;
  ticketsPerWorkspace: number;
  chatsPerWorkspace: number;
  runsPerWorkspace: number;
}) {
  const repos: RepoSummary[] = [];
  const worktrees: WorktreeSummary[] = [];
  const tickets: TicketSummary[] = [];
  const chats: ChatSummary[] = [];
  const runs: ChatRunProgress[] = [];
  for (let repoIndex = 0; repoIndex < config.repoCount; repoIndex += 1) {
    const repoId = `repo-${repoIndex}`;
    repos.push(repo(repoId, repoIndex));
    addWorkspaceRows('repo', repoId, repoId, null, config, tickets, chats, runs);
    for (let worktreeIndex = 0; worktreeIndex < config.worktreesPerRepo; worktreeIndex += 1) {
      const worktreeId = `${repoId}-worktree-${worktreeIndex}`;
      worktrees.push(worktree(worktreeId, repoId, worktreeIndex));
      addWorkspaceRows('worktree', worktreeId, repoId, worktreeId, config, tickets, chats, runs);
    }
  }
  return { repos, worktrees, tickets, runs, chats, artifacts: [], ticketsListLoaded: true };
}

function addWorkspaceRows(
  kind: 'repo' | 'worktree',
  id: string,
  repoId: string,
  worktreeId: string | null,
  config: { ticketsPerWorkspace: number; chatsPerWorkspace: number; runsPerWorkspace: number },
  tickets: TicketSummary[],
  chats: ChatSummary[],
  runs: ChatRunProgress[]
): void {
  for (let ticketIndex = 0; ticketIndex < config.ticketsPerWorkspace; ticketIndex += 1) {
    const ticketId = `${id}-ticket-${ticketIndex}`;
    const runId = `${id}-run-${ticketIndex % Math.max(1, config.runsPerWorkspace)}`;
    const chatId = `${id}-chat-${ticketIndex % Math.max(1, config.chatsPerWorkspace)}`;
    tickets.push(ticket(ticketId, kind, id, repoId, worktreeId, runId, chatId, ticketIndex));
  }
  for (let chatIndex = 0; chatIndex < config.chatsPerWorkspace; chatIndex += 1) {
    chats.push(chat(`${id}-chat-${chatIndex}`, kind, id, repoId, worktreeId, chatIndex));
  }
  for (let runIndex = 0; runIndex < config.runsPerWorkspace; runIndex += 1) {
    runs.push(run(`${id}-run-${runIndex}`, kind, id, repoId, worktreeId, `${id}-chat-${runIndex % Math.max(1, config.chatsPerWorkspace)}`, runIndex));
  }
}

function repo(id: string, index: number): RepoSummary {
  return {
    id,
    name: id,
    path: `/repos/${id}`,
    status: index % 7 === 0 ? 'running' : 'idle',
    activeRuns: index % 7 === 0 ? 1 : 0,
    openTickets: 0,
    worktreeCount: 0,
    lastActivityAt: now,
    defaultBranch: 'main',
    gitStatus: null,
    raw: { has_car_state: true, is_pinned: index % 13 === 0 }
  };
}

function worktree(id: string, repoId: string, index: number): WorktreeSummary {
  return {
    id,
    name: id,
    path: `/repos/${repoId}/worktrees/${id}`,
    repoId,
    branch: `branch-${index}`,
    status: index % 5 === 0 ? 'running' : 'idle',
    activeRuns: index % 5 === 0 ? 1 : 0,
    openTickets: 0,
    lastActivityAt: now,
    gitStatus: null,
    raw: { has_car_state: true }
  };
}

function ticket(
  id: string,
  kind: 'repo' | 'worktree',
  workspaceId: string,
  repoId: string,
  worktreeId: string | null,
  runId: string,
  chatKey: string,
  index: number
): TicketSummary {
  return {
    id,
    number: index + 1,
    title: `Ticket ${id}`,
    status: index % 6 === 0 ? 'waiting' : index % 8 === 0 ? 'failed' : 'idle',
    workspaceKind: kind,
    workspaceId,
    workspacePath: `/workspace/${workspaceId}`,
    repoId,
    worktreeId,
    path: `.codex-autorunner/tickets/TICKET-${String(index + 1).padStart(3, '0')}.md`,
    ticketPath: null,
    agentId: 'codex',
    chatKey,
    runId,
    updatedAt: now,
    durationSeconds: index * 10,
    diffStats: null,
    errors: [],
    raw: {
      body: `---\nagent: codex\nmodel: gpt-5.5\n---\n\n## Goal\n${id}`,
      frontmatter: { agent: 'codex', model: 'gpt-5.5' },
      repo_id: repoId,
      ...(worktreeId ? { worktree_id: worktreeId } : {}),
      resource_kind: kind,
      resource_id: workspaceId
    }
  };
}

function chat(
  id: string,
  kind: 'repo' | 'worktree',
  workspaceId: string,
  repoId: string,
  worktreeId: string | null,
  index: number
): ChatSummary {
  return {
    id,
    title: `Chat ${id}`,
    lifecycleStatus: 'active',
    status: index % 5 === 0 ? 'waiting' : index % 7 === 0 ? 'failed' : 'idle',
    agentId: 'codex',
    agentProfile: null,
    model: 'gpt-5.5',
    repoId: kind === 'repo' ? workspaceId : repoId,
    worktreeId,
    ticketId: `${workspaceId}-ticket-${index}`,
    runId: `${workspaceId}-run-${index}`,
    isTicketFlow: true,
    progressPercent: null,
    updatedAt: now,
    raw: { resource_kind: kind, resource_id: workspaceId }
  };
}

function run(
  id: string,
  kind: 'repo' | 'worktree',
  workspaceId: string,
  repoId: string,
  worktreeId: string | null,
  chatId: string,
  index: number
): ChatRunProgress {
  return {
    id,
    chatId,
    status: index % 3 === 0 ? 'running' : 'idle',
    workStatus: index % 3 === 0 ? 'running' : 'idle',
    operatorStatus: null,
    terminal: false,
    streamShouldClose: false,
    streamCloseReason: null,
    phase: null,
    guidance: null,
    queueDepth: 0,
    elapsedSeconds: index,
    startedAt: now,
    idleSeconds: null,
    lastEventId: index,
    lastEventAt: now,
    progressPercent: null,
    events: [],
    raw: {
      resource_kind: kind,
      resource_id: workspaceId,
      repo_id: repoId,
      ...(worktreeId ? { worktree_id: worktreeId } : {})
    }
  };
}

function syntheticSettingsInput(agentCount: number, modelsPerAgent: number) {
  const agents = Array.from({ length: agentCount }, (_, index) => ({
    id: `agent_${index}`,
    name: `Agent ${index}`,
    capabilities: ['list_models'],
    capability_projection: { actions: { list_models: { allowed: true } } }
  }));
  const modelCatalogs = Object.fromEntries(
    agents.map((agent) => [
      agent.id,
      Array.from({ length: modelsPerAgent }, (_, modelIndex) => ({
        id: `${agent.id}-model-${modelIndex}`,
        display_name: `Model ${modelIndex}`
      }))
    ])
  );
  return {
    session: {
      autorunner_model_overrides: {},
      autorunner_effort_override: '',
      runner_stop_after_runs: null
    },
    agents,
    modelCatalogs,
    voiceConfig: { enabled: false, provider: 'local_whisper' }
  };
}
