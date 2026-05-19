import type { WorkStatus } from './domain';
import { stripInjectedContextBlocks } from './injectedContext';
import type { ScopeRef, SurfaceRef } from './scope';
import { scopeFromApiPayload } from './scope';

export type ThreadSummary = {
  id: string;
  scope: ScopeRef;
  surface: SurfaceRef | null;
  title: string;
  status: WorkStatus;
  agentId: string | null;
  model: string | null;
  ticketId: string | null;
  progressPercent: number | null;
  updatedAt: string | null;
  raw: Record<string, unknown>;
};

export type ThreadDetail = ThreadSummary & {
  body: string | null;
  turnCount: number | null;
  lastTurnId: string | null;
};

export function mapThreadSummary(raw: Record<string, unknown>): ThreadSummary {
  const id = stringValue(
    raw.thread_target_id ?? raw.managed_thread_id ?? raw.thread_id ?? raw.id,
    'unknown-thread'
  );
  const scope = scopeFromApiPayload(raw);
  const surface = surfaceRefFromThreadRaw(raw);
  const latest = asRecord(raw.latest_execution ?? raw.latest_turn ?? raw.turn);
  const resourceKind = nullableString(raw.resource_kind);
  const resourceId = nullableString(raw.resource_id);
  const repoId = nullableString(raw.repo_id) ?? (resourceKind === 'repo' ? resourceId : null);
  const worktreeId =
    nullableString(raw.worktree_repo_id ?? raw.worktree_id) ??
    (resourceKind === 'worktree' ? resourceId : null);
  const ticketId = extractTicketId(raw);

  return {
    id,
    scope,
    surface,
    title: buildThreadTitle(raw, id, ticketId, scope),
    status: normalizeWorkStatus(
      raw.normalized_status ?? raw.runtime_status ?? raw.status ?? latest.status ?? raw.lifecycle_status
    ),
    agentId: nullableString(raw.agent_id ?? raw.agent),
    model: nullableString(raw.model ?? latest.model),
    ticketId,
    progressPercent: numberOrNull(raw.progress_percent ?? raw.progress),
    updatedAt: dateString(
      raw.updated_at ?? raw.last_activity_at ?? latest.finished_at ?? latest.started_at
    ),
    raw
  };
}

export function mapThreadDetail(raw: Record<string, unknown>): ThreadDetail {
  const summary = mapThreadSummary(raw);
  return {
    ...summary,
    body: nullableString(raw.body ?? raw.content ?? raw.markdown),
    turnCount: numberOrNull(raw.turn_count ?? raw.iteration_count),
    lastTurnId: nullableString(raw.last_turn_id ?? raw.managed_turn_id)
  };
}

export function surfaceRefFromThreadRaw(raw: Record<string, unknown>): SurfaceRef | null {
  const surfaceUrn = nullableString(raw.surface_urn);
  if (surfaceUrn) {
    const colonPos = surfaceUrn.indexOf(':');
    if (colonPos > 0) {
      return {
        kind: surfaceUrn.slice(0, colonPos),
        key: decodeURIComponent(surfaceUrn.slice(colonPos + 1))
      };
    }
  }
  const surfaceKind = nullableString(raw.surface_kind ?? raw.channel_kind);
  const surfaceKey = nullableString(raw.surface_key ?? raw.channel_key);
  if (surfaceKind && surfaceKey) return { kind: surfaceKind, key: surfaceKey };
  return null;
}

function buildThreadTitle(
  raw: Record<string, unknown>,
  fallback: string,
  ticketId: string | null,
  scope: ScopeRef
): string {
  const explicitRaw = stringValue(raw.display_name ?? raw.name ?? raw.title, fallback);
  let explicit = stripInjectedContextBlocks(explicitRaw).trim();
  if (!explicit) {
    explicit = explicitRaw === fallback ? fallback : '';
  }
  if (
    !isGenericChatTitle(explicit) &&
    !isGenericTicketFlowTitle(explicit) &&
    !isCarTicketFlowControlPrompt(explicit)
  ) {
    return explicit;
  }

  const firstMessageExcerpt = firstUserMessageExcerpt(raw);
  if (firstMessageExcerpt) return firstMessageExcerpt;

  if (isGenericChatTitle(explicit) && !ticketId && !isCarTicketFlowControlPrompt(explicit)) {
    const scopeStr = scopeLabelStr(scope);
    return scopeStr ? `Chat · ${scopeStr}` : explicit;
  }

  const parts = ['Ticket flow'];
  if (ticketId) parts.push(ticketId);
  const scopeStr = scopeLabelStr(scope);
  if (scopeStr) parts.push(scopeStr);
  return parts.join(' · ');
}

type WorkStatusAlias =
  | 'running'
  | 'waiting'
  | 'idle'
  | 'done'
  | 'failed'
  | 'blocked';

function normalizeWorkStatus(value: unknown): WorkStatusAlias {
  const text = String(value ?? '').trim().toLowerCase();
  if (['running', 'active', 'in_progress', 'progress'].includes(text)) return 'running';
  if (['waiting', 'paused', 'needs_user', 'queued', 'pending'].includes(text)) return 'waiting';
  if (['ok', 'done', 'complete', 'completed', 'interrupted', 'cancelled', 'canceled', 'aborted'].includes(text)) return 'done';
  if (text === 'idle') return 'idle';
  if (['failed', 'error', 'errored'].includes(text)) return 'failed';
  if (['blocked', 'stalled'].includes(text)) return 'blocked';
  return 'idle';
}

function scopeLabelStr(scope: ScopeRef): string | null {
  switch (scope.kind) {
    case 'hub': return null;
    case 'repo': return scope.id;
    case 'worktree': return `worktree ${scope.id}`;
    case 'filesystem': return scope.path.split(/[\\/]/).filter(Boolean).at(-1) ?? scope.path;
  }
}

function isGenericChatTitle(value: string): boolean {
  const text = value.trim().toLowerCase();
  return text === 'new pma chat' || text === 'new chat' || text === 'untitled chat' || text === '';
}

function isGenericTicketFlowTitle(value: string): boolean {
  return /^ticket-flow(?::\S+)?$/i.test(value.trim());
}

function isCarTicketFlowControlPrompt(value: string): boolean {
  const text = value.trim();
  return text.startsWith('<CAR_TICKET_FLOW_PROMPT') || text.includes('<CAR_CURRENT_TICKET_FILE>');
}

function firstUserMessageExcerpt(raw: Record<string, unknown>): string | null {
  const candidate = stripInjectedContextBlocks(
    firstText(
      raw.first_user_visible_text,
      raw.user_visible_text,
      raw.title_seed,
      raw.first_message_excerpt,
      raw.first_user_message,
      raw.last_user_message,
      raw.last_message_preview,
      raw.prompt_preview
    )
  );
  if (!candidate) return null;
  const trimmed = candidate.trim();
  if (!trimmed) return null;
  if (isCarTicketFlowControlPrompt(trimmed)) return null;
  const oneLine = trimmed.split(/\r?\n/)[0]?.trim() ?? '';
  if (!oneLine) return null;
  return oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine;
}

function extractTicketId(raw: Record<string, unknown>): string | null {
  const direct = nullableString(raw.ticket_id ?? raw.current_ticket_id ?? raw.current_ticket);
  const text = firstText(
    raw.last_message_preview,
    raw.prompt_preview,
    raw.prompt,
    raw.prompt_text,
    raw.message,
    raw.name,
    raw.title
  );
  return extractTicketIdFromText(text) ?? direct;
}

function extractTicketIdFromText(value: string): string | null {
  const ticketNumber = value.match(/\bTICKET-\d+[A-Za-z0-9_-]*\b/);
  if (ticketNumber) return ticketNumber[0];
  const frontmatterId = value.match(/\bticket_id:\s*["']?([A-Za-z0-9_.:-]+)["']?/);
  return frontmatterId?.[1] ?? null;
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

function stringValue(value: unknown, fallback: string): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function dateString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return new Date(value * 1000).toISOString();
  return null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}
