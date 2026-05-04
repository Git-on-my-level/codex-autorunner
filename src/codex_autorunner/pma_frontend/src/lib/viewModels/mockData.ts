import type {
  ContextspaceDocument,
  DashboardSummary,
  PmaChatMessage,
  PmaChatSummary,
  PmaRunProgress,
  RepoSummary,
  SensitiveApprovalRequest,
  SurfaceArtifact,
  TicketDetail,
  TicketSummary,
  WorktreeSummary
} from './domain';

export const mockArtifact: SurfaceArtifact = {
  id: 'artifact-preview',
  kind: 'preview_url',
  title: 'Preview ready',
  summary: 'Local preview is available.',
  url: 'http://127.0.0.1:5173/pma',
  createdAt: '2026-05-04T00:00:00Z',
  raw: {}
};

export const mockRunProgress: PmaRunProgress = {
  id: 'run-1',
  chatId: 'chat-1',
  status: 'running',
  phase: 'testing',
  guidance: null,
  queueDepth: 0,
  elapsedSeconds: 120,
  idleSeconds: 2,
  lastEventId: 12,
  lastEventAt: '2026-05-04T00:02:00Z',
  events: [mockArtifact],
  raw: {}
};

export const mockChatSummary: PmaChatSummary = {
  id: 'chat-1',
  title: 'Hub rewrite foundation',
  status: 'running',
  agentId: 'codex',
  model: 'configured model',
  repoId: 'codex-autorunner',
  worktreeId: null,
  ticketId: 'TICKET-110',
  progressPercent: 60,
  updatedAt: '2026-05-04T00:02:00Z',
  raw: {}
};

export const mockChatMessage: PmaChatMessage = {
  id: 'msg-1',
  chatId: 'chat-1',
  role: 'assistant',
  text: 'The PMA data layer is ready for components.',
  createdAt: '2026-05-04T00:02:00Z',
  status: 'running',
  artifacts: [mockArtifact],
  raw: {}
};

export const mockDashboardSummary: DashboardSummary = {
  activeRuns: 1,
  waitingForUser: 0,
  failedOrBlocked: 0,
  openTickets: 3,
  repos: 1,
  worktrees: 2,
  recentArtifacts: [mockArtifact],
  raw: {}
};

export const mockRepoSummary: RepoSummary = {
  id: 'repo-1',
  name: 'codex-autorunner',
  path: '/workspace/codex-autorunner',
  status: 'running',
  defaultBranch: 'main',
  worktreeCount: 2,
  activeRuns: 1,
  openTickets: 3,
  lastActivityAt: '2026-05-04T00:02:00Z',
  raw: {}
};

export const mockWorktreeSummary: WorktreeSummary = {
  id: 'worktree-1',
  repoId: 'repo-1',
  name: 'discord-5',
  path: '/workspace/codex-autorunner--discord-5',
  branch: 'ticket-110',
  status: 'running',
  activeRuns: 1,
  openTickets: 1,
  lastActivityAt: '2026-05-04T00:02:00Z',
  raw: {}
};

export const mockTicketSummary: TicketSummary = {
  id: 'TICKET-110',
  number: 110,
  title: 'Implement typed UI API client and view models',
  status: 'running',
  repoId: 'repo-1',
  worktreeId: 'worktree-1',
  path: '.codex-autorunner/tickets/TICKET-110.md',
  agentId: 'codex',
  chatKey: 'ticket:TICKET-110',
  runId: 'run-1',
  updatedAt: '2026-05-04T00:02:00Z',
  durationSeconds: 120,
  diffStats: { insertions: 80, deletions: 5, filesChanged: 4 },
  errors: [],
  raw: {}
};

export const mockTicketDetail: TicketDetail = {
  ...mockTicketSummary,
  body: 'Create typed client modules and stable view models.',
  progress: mockRunProgress,
  artifacts: [mockArtifact]
};

export const mockContextspaceDocument: ContextspaceDocument = {
  id: 'spec',
  name: 'spec.md',
  kind: 'spec',
  content: '# Spec',
  updatedAt: '2026-05-04T00:02:00Z',
  isPinned: true,
  raw: {}
};

export const mockSensitiveApprovalRequest: SensitiveApprovalRequest = {
  id: 'approval-1',
  title: 'Delete worktree',
  description: 'Sensitive CAR control-plane operation.',
  risk: 'high',
  action: 'delete_worktree',
  createdAt: '2026-05-04T00:02:00Z',
  raw: {}
};
