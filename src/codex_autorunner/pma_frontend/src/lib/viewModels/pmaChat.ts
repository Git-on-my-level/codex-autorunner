import type {
  AgentWorkspaceSummary,
  PmaChatMessage,
  PmaChatSummary,
  PmaRunProgress,
  RepoSummary,
  SensitiveApprovalRequest,
  SurfaceArtifact,
  WorktreeSummary,
  WorkStatus
} from './domain';

export type PmaChatFilter = 'all' | 'active' | 'waiting' | 'done';

export type PendingAttachmentKind = 'file' | 'image' | 'link';

export type PendingAttachment = {
  id: string;
  kind: PendingAttachmentKind;
  title: string;
  sizeLabel: string | null;
  url: string | null;
  uploadedName: string | null;
  uploadState: 'pending' | 'uploaded' | 'error';
};

export type ModelSelectorState = {
  state: 'loading' | 'empty' | 'error' | 'loaded';
  label: string;
  disabled: boolean;
};

export type ArtifactCardView = {
  label: string;
  tone: 'neutral' | 'media' | 'success' | 'warning' | 'danger' | 'link';
  primaryAction: string | null;
  preview: 'image' | 'link' | 'text' | 'file' | 'none';
  detailLabel: string;
};

export type PmaCard =
  | { kind: 'message'; id: string; message: PmaChatMessage }
  | { kind: 'ticket'; id: string; title: string; summary: string | null; ticketId: string }
  | { kind: 'artifact'; id: string; artifact: SurfaceArtifact };

export type PmaLiveActivity = {
  state: WorkStatus;
  title: string;
  summary: string;
  elapsedLabel: string | null;
  steps: SurfaceArtifact[];
};

export type ManagedThreadCreatePayload = {
  agent?: string;
  name: string;
  workspace_root?: string;
  resource_kind?: 'repo' | 'agent_workspace';
  resource_id?: string;
};

export type PmaChatScopeOption =
  | {
      id: 'local';
      kind: 'local';
      label: string;
      detail: string;
      workspaceRoot: string;
    }
  | {
      id: string;
      kind: 'repo';
      label: string;
      detail: string;
      resourceKind: 'repo';
      resourceId: string;
    }
  | {
      id: string;
      kind: 'worktree';
      label: string;
      detail: string;
      workspaceRoot: string;
      resourceId: string;
    }
  | {
      id: string;
      kind: 'agent_workspace';
      label: string;
      detail: string;
      resourceKind: 'agent_workspace';
      resourceId: string;
      agentId: string | null;
    };

export type ManagedThreadMessagePayload = {
  message: string;
  model?: string;
  busy_policy?: 'queue';
};

const activeStatuses: WorkStatus[] = ['running'];
const waitingStatuses: WorkStatus[] = ['waiting', 'blocked'];
const doneStatuses: WorkStatus[] = ['done', 'failed', 'idle'];

export function filterPmaChats(
  chats: PmaChatSummary[],
  filter: PmaChatFilter,
  query: string
): PmaChatSummary[] {
  const needle = query.trim().toLowerCase();
  return chats
    .filter((chat) => {
      if (filter === 'active') return activeStatuses.includes(chat.status);
      if (filter === 'waiting') return waitingStatuses.includes(chat.status);
      if (filter === 'done') return doneStatuses.includes(chat.status);
      return true;
    })
    .filter((chat) => {
      if (!needle) return true;
      return [
        chat.title,
        chat.repoId,
        chat.worktreeId,
        chat.ticketId,
        chat.agentId,
        chat.model,
        chat.raw.resource_kind,
        chat.raw.resource_id
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
}

export function summarizeFilterCounts(chats: PmaChatSummary[]): Record<PmaChatFilter, number> {
  return {
    all: chats.length,
    active: chats.filter((chat) => activeStatuses.includes(chat.status)).length,
    waiting: chats.filter((chat) => waitingStatuses.includes(chat.status)).length,
    done: chats.filter((chat) => doneStatuses.includes(chat.status)).length
  };
}

export function chooseActiveChatId(
  chats: PmaChatSummary[],
  currentId: string | null,
  requestedId: string | null = null
): string | null {
  if (requestedId && chats.some((chat) => chat.id === requestedId)) return requestedId;
  if (currentId && chats.some((chat) => chat.id === currentId)) return currentId;
  return chats[0]?.id ?? null;
}

export function buildPmaCards(
  messages: PmaChatMessage[],
  progress: PmaRunProgress | null,
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[]
): PmaCard[] {
  const cards: PmaCard[] = messages.flatMap((message) => {
    const messageCards: PmaCard[] = [];
    if (message.text.trim()) {
      messageCards.push({
        kind: 'message' as const,
        id: message.id,
        message
      });
    }
    messageCards.push(
      ...message.artifacts.map((artifact) => ({
        kind: 'artifact' as const,
        id: `message-${message.id}-${artifact.id}`,
        artifact
      }))
    );
    return messageCards;
  });

  if (chat?.ticketId) {
    cards.push({
      kind: 'ticket',
      id: `ticket-${chat.ticketId}`,
      ticketId: chat.ticketId,
      title: chat.ticketId,
      summary: chat.title
    });
  }

  for (const artifact of artifacts.slice(0, 4)) {
    cards.push({ kind: 'artifact', id: `artifact-${artifact.id}`, artifact });
  }

  return cards;
}

export function buildPmaLiveActivity(progress: PmaRunProgress | null): PmaLiveActivity | null {
  if (!progress) return null;
  const steps = progress.events.filter(isPrimaryProgressArtifact).slice(-4);
  const phase = progress.phase?.replace(/_/g, ' ') ?? null;
  const status = progress.status;
  const title =
    status === 'running'
      ? phase
        ? `Working · ${phase}`
        : 'Working'
      : status === 'waiting'
        ? phase
          ? `Waiting · ${phase}`
          : 'Waiting'
        : status === 'failed'
          ? 'Run failed'
          : status === 'blocked'
            ? 'Blocked'
          : status === 'done'
            ? 'Run complete'
            : 'Idle';
  const summary =
    progress.guidance ??
    (steps.length
      ? steps.at(-1)?.summary ?? steps.at(-1)?.title ?? 'PMA is updating the workspace.'
      : status === 'running'
        ? 'PMA is streaming activity.'
        : `Last update ${formatRelativeTime(progress.lastEventAt)}.`);
  const elapsedLabel = formatElapsedProgress(progress.elapsedSeconds, progress.idleSeconds);
  return { state: status, title, summary, elapsedLabel, steps };
}

export function isPrimaryProgressArtifact(artifact: SurfaceArtifact): boolean {
  const eventType = stringValue(artifact.raw.event_type ?? artifact.raw.type ?? artifact.raw.kind).toLowerCase();
  const summary = [artifact.title, artifact.summary].filter(Boolean).join(' ').toLowerCase();
  if (eventType.includes('token_usage') || summary === 'token usage updated') return false;
  if (['turn_completed', 'prompt_completed', 'session_idle'].includes(eventType)) return false;
  if (eventType === 'progress' && /^(turn completed|token usage updated)$/i.test(artifact.title)) return false;
  return ['assistant_update', 'tool_started', 'tool_completed', 'tool_failed', 'turn_failed', 'turn_interrupted'].some(
    (type) => eventType.includes(type)
  );
}

export function formatRelativeTime(value: string | null, now = new Date()): string {
  if (!value) return 'No activity yet';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const seconds = Math.max(0, Math.round((now.getTime() - parsed.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function progressPercent(chat: PmaChatSummary, progress: PmaRunProgress | null = null): number {
  if (typeof chat.progressPercent === 'number') return clampPercent(chat.progressPercent);
  if (progress?.status === 'done') return 100;
  if (progress?.status === 'failed') return 100;
  if (progress?.status === 'running') return 64;
  if (progress?.status === 'waiting') return 28;
  if (chat.status === 'running') return 58;
  if (chat.status === 'waiting' || chat.status === 'blocked') return 24;
  if (chat.status === 'done' || chat.status === 'failed') return 100;
  return 0;
}

export function statusLabel(status: WorkStatus): string {
  return status.replace('_', ' ');
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; value >= 1024 && index < units.length; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
}

function formatElapsedProgress(elapsedSeconds: number | null, idleSeconds: number | null): string | null {
  const parts: string[] = [];
  if (elapsedSeconds !== null) parts.push(`${formatDuration(elapsedSeconds)} elapsed`);
  if (idleSeconds !== null && idleSeconds > 0) parts.push(`${formatDuration(idleSeconds)} idle`);
  return parts.length ? parts.join(' · ') : null;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`;
}

export function removePendingAttachment(
  attachments: PendingAttachment[],
  attachmentId: string
): PendingAttachment[] {
  return attachments.filter((attachment) => attachment.id !== attachmentId);
}

export function composeMessageWithAttachments(
  draft: string,
  attachments: PendingAttachment[]
): string {
  const message = draft.trim();
  const lines = attachments.map((attachment) => {
    const label = attachment.kind === 'image' ? 'Image' : attachment.kind === 'link' ? 'Link' : 'File';
    const target = attachment.url || attachment.uploadedName || attachment.title;
    return `- ${label}: ${attachment.title}${target && target !== attachment.title ? ` (${target})` : ''}`;
  });
  if (!lines.length) return message;
  return [message, 'Attachments:', ...lines].filter(Boolean).join('\n');
}

export function buildManagedThreadCreatePayload(
  agent: string,
  scope: PmaChatScopeOption = localPmaChatScopeOption(),
  name = 'New PMA chat'
): ManagedThreadCreatePayload {
  const base = {
    agent: agent || undefined,
    name
  };
  if (scope.kind === 'repo' || scope.kind === 'agent_workspace') {
    return {
      ...base,
      resource_kind: scope.resourceKind,
      resource_id: scope.resourceId
    };
  }
  return {
    ...base,
    workspace_root: scope.workspaceRoot
  };
}

export function localPmaChatScopeOption(): PmaChatScopeOption {
  return {
    id: 'local',
    kind: 'local',
    label: 'Local hub',
    detail: 'Current workspace',
    workspaceRoot: '.'
  };
}

export function buildPmaChatScopeOptions(
  repos: RepoSummary[],
  worktrees: WorktreeSummary[],
  agentWorkspaces: AgentWorkspaceSummary[]
): PmaChatScopeOption[] {
  return [
    localPmaChatScopeOption(),
    ...repos.map((repo) => ({
      id: `repo:${repo.id}`,
      kind: 'repo' as const,
      label: repo.name || repo.id,
      detail: `Repo · ${repo.id}`,
      resourceKind: 'repo' as const,
      resourceId: repo.id
    })),
    ...worktrees
      .filter((worktree) => Boolean(worktree.path))
      .map((worktree) => ({
        id: `worktree:${worktree.id}`,
        kind: 'worktree' as const,
        label: worktree.name || worktree.id,
        detail: `Worktree · ${worktree.repoId ?? worktree.id}`,
        workspaceRoot: worktree.path || '.',
        resourceId: worktree.id
      })),
    ...agentWorkspaces.map((workspace) => ({
      id: `agent_workspace:${workspace.id}`,
      kind: 'agent_workspace' as const,
      label: workspace.name || workspace.id,
      detail: `Agent workspace · ${workspace.runtime || workspace.id}`,
      resourceKind: 'agent_workspace' as const,
      resourceId: workspace.id,
      agentId: workspace.runtime || null
    }))
  ];
}

export function pmaChatScopeLabel(scope: PmaChatScopeOption | null): string {
  if (!scope) return 'Workspace scope';
  if (scope.kind === 'local') return 'Local hub · current workspace';
  if (scope.kind === 'repo') return `Repo · ${scope.resourceId}`;
  if (scope.kind === 'agent_workspace') return `Agent workspace · ${scope.resourceId}`;
  return `Worktree · ${scope.resourceId}`;
}

export function pmaChatScopeLabelFromChat(chat: PmaChatSummary | null): string {
  if (!chat) return 'Choose a scope before creating a chat';
  const resourceKind = stringValue(chat.raw.resource_kind).toLowerCase();
  const resourceId = stringValue(chat.raw.resource_id);
  if (resourceKind === 'agent_workspace' && resourceId) return `Agent workspace · ${resourceId}`;
  if (chat.worktreeId) return `Worktree · ${chat.worktreeId}`;
  if (chat.repoId) return `Repo · ${chat.repoId}`;
  const workspaceRoot = stringValue(chat.raw.workspace_root);
  if (workspaceRoot && workspaceRoot !== '.') return `Workspace · ${workspaceRoot}`;
  return 'Local hub · current workspace';
}

export function buildManagedThreadMessagePayload(
  message: string,
  model: string,
  isRunning: boolean
): ManagedThreadMessagePayload {
  return {
    message,
    model: model || undefined,
    busy_policy: isRunning ? 'queue' : undefined
  };
}

export function modelSelectorState(
  loading: boolean,
  errorMessage: string | null,
  modelCount: number
): ModelSelectorState {
  if (loading) {
    return { state: 'loading', label: 'Loading models', disabled: true };
  }
  if (errorMessage) {
    return { state: 'error', label: errorMessage, disabled: true };
  }
  if (modelCount === 0) {
    return { state: 'empty', label: 'No models exposed', disabled: true };
  }
  return { state: 'loaded', label: 'Model', disabled: false };
}

export function artifactCardView(artifact: SurfaceArtifact): ArtifactCardView {
  switch (artifact.kind) {
    case 'screenshot':
      return {
        label: 'Screenshot',
        tone: 'media',
        primaryAction: artifact.url ? 'Open screenshot' : null,
        preview: artifact.url ? 'image' : 'text',
        detailLabel: 'Screenshot details'
      };
    case 'image':
      return {
        label: 'Image',
        tone: 'media',
        primaryAction: artifact.url ? 'Open image' : null,
        preview: artifact.url ? 'image' : 'text',
        detailLabel: 'Image details'
      };
    case 'file':
      return {
        label: 'File',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open file' : null,
        preview: 'file',
        detailLabel: 'File details'
      };
    case 'preview_url':
      return {
        label: 'Preview URL',
        tone: 'link',
        primaryAction: artifact.url ? 'Open preview' : null,
        preview: 'link',
        detailLabel: 'Preview details'
      };
    case 'test_result':
      return {
        label: 'Test result',
        tone: artifact.summary?.toLowerCase().includes('fail') ? 'danger' : 'success',
        primaryAction: artifact.url ? 'Open test output' : null,
        preview: 'text',
        detailLabel: 'Test details'
      };
    case 'command_summary':
      return {
        label: 'Command summary',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open command output' : null,
        preview: 'text',
        detailLabel: 'Command details'
      };
    case 'diff_summary':
      return {
        label: 'Diff summary',
        tone: 'warning',
        primaryAction: artifact.url ? 'Open diff' : null,
        preview: 'text',
        detailLabel: 'Diff details'
      };
    case 'link':
      return {
        label: 'PR / link',
        tone: 'link',
        primaryAction: artifact.url ? 'Open link' : null,
        preview: 'link',
        detailLabel: 'Link details'
      };
    case 'final_report':
      return {
        label: 'PMA final report',
        tone: 'success',
        primaryAction: artifact.url ? 'Open report' : null,
        preview: 'text',
        detailLabel: 'Report details'
      };
    case 'error':
      return {
        label: 'Error / blocker',
        tone: 'danger',
        primaryAction: artifact.url ? 'Open details' : null,
        preview: 'text',
        detailLabel: 'Blocker details'
      };
    case 'progress':
      return {
        label: 'Run event',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open event' : null,
        preview: 'text',
        detailLabel: 'Event details'
      };
  }
}

const sensitiveActionPattern =
  /\b(config|secret|credential|delete|remove repo|delete repo|delete worktree|cleanup|clean slate|reset hub|reset runtime|destructive|force removal)\b/i;

export function isSensitiveCarApproval(request: SensitiveApprovalRequest): boolean {
  const haystack = [request.action, request.title, request.description].join(' ');
  return sensitiveActionPattern.test(haystack);
}

export function filterSensitiveCarApprovals(
  approvals: SensitiveApprovalRequest[]
): SensitiveApprovalRequest[] {
  return approvals.filter(isSensitiveCarApproval);
}

export function approvalScopeLabel(request: SensitiveApprovalRequest): string {
  const raw = request.raw;
  for (const key of ['target_scope', 'scope', 'repo_id', 'worktree_repo_id', 'resource_id']) {
    const value = raw[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return request.action;
}

export function approvalActionUrl(
  request: SensitiveApprovalRequest,
  decision: 'approve' | 'decline'
): string | null {
  const raw = request.raw;
  const directKeys =
    decision === 'approve'
      ? ['approve_url', 'approval_url', 'accept_url']
      : ['decline_url', 'reject_url', 'deny_url'];
  for (const key of directKeys) {
    const value = raw[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  const actions = raw.actions;
  if (actions && typeof actions === 'object') {
    const action = (actions as Record<string, unknown>)[decision] ?? (actions as Record<string, unknown>)[decision === 'approve' ? 'accept' : 'reject'];
    if (typeof action === 'string' && action.trim()) return action;
    if (action && typeof action === 'object') {
      const url = (action as Record<string, unknown>).url;
      if (typeof url === 'string' && url.trim()) return url;
    }
  }
  const decisionUrl = raw.decision_url ?? raw.route;
  return typeof decisionUrl === 'string' && decisionUrl.trim() ? decisionUrl : null;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function stringValue(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}
