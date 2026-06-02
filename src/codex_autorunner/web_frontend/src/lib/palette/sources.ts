import type { PaletteItem, PaletteSource } from './types';
import type { ChatSummary, TicketSummary, ContextspaceDocument } from '$lib/viewModels/domain';
import type { RepoSummary, WorktreeSummary } from '$lib/viewModels/domain';
import {
  chatRoute,
  repoRoute,
  repoTicketRoute,
  worktreeRoute,
  worktreeTicketRoute
} from '$lib/viewModels/routes';
import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';

const RECENT_ACTIONS_MAX = 20;

let recentActions: PaletteItem[] = [];

export function clearRecentActions(): void {
  recentActions = [];
}

export function recordRecentAction(item: PaletteItem): void {
  recentActions = recentActions.filter((r) => r.id !== item.id);
  recentActions.unshift(item);
  if (recentActions.length > RECENT_ACTIONS_MAX) {
    recentActions = recentActions.slice(0, RECENT_ACTIONS_MAX);
  }
}

export function getRecentActions(): PaletteItem[] {
  return [...recentActions];
}

export function threadSource(threads: ChatSummary[]): PaletteSource {
  return {
    group: 'Chats',
    priority: 10,
    load: () =>
      [...threads].sort(compareActivity).map((thread) => ({
        id: `thread:${thread.id}`,
        label: thread.title,
        group: 'Chats',
        keywords: `${thread.id} ${thread.agentId ?? ''} ${thread.ticketId ?? ''} ${thread.model ?? ''} ${thread.repoId ?? ''} ${thread.worktreeId ?? ''}`,
        glyph: repoInitials(thread.title),
        accent: repoAccent(thread.repoId ?? thread.worktreeId ?? thread.title),
        meta: compactMeta([thread.agentId, thread.repoId, thread.worktreeId, relativeActivity(thread.updatedAt)]),
        chip: shortId(thread.id),
        lastActivityAt: thread.updatedAt,
        action: { kind: 'navigate', href: chatRoute(thread.id) }
      }))
  };
}

export function repoSource(repos: RepoSummary[]): PaletteSource {
  return {
    group: 'Repos',
    priority: 20,
    load: () =>
      [...repos].sort(compareActivity).map((repo) => ({
        id: `repo:${repo.id}`,
        label: repo.name,
        group: 'Repos',
        keywords: `repo repository ${repo.id} ${repo.path ?? ''} ${repo.defaultBranch ?? ''}`,
        glyph: repoInitials(repo.name),
        accent: repoAccent(repo.name),
        meta: compactMeta([repo.defaultBranch, repo.path, relativeActivity(repo.lastActivityAt)]),
        chip: shortId(repo.id),
        lastActivityAt: repo.lastActivityAt,
        action: { kind: 'navigate', href: repoRoute(repo.id) }
      }))
  };
}

export function worktreeSource(worktrees: WorktreeSummary[]): PaletteSource {
  return {
    group: 'Worktrees',
    priority: 30,
    load: () =>
      [...worktrees].sort(compareActivity).map((wt) => ({
        id: `worktree:${wt.id}`,
        label: wt.name,
        group: 'Worktrees',
        keywords: `worktree ${wt.id} ${wt.branch ?? ''} ${wt.repoId ?? ''} ${wt.path ?? ''}`,
        glyph: repoInitials(wt.name),
        accent: repoAccent(wt.repoId ?? wt.name),
        meta: compactMeta([wt.branch, wt.repoId, relativeActivity(wt.lastActivityAt)]),
        chip: shortId(wt.id),
        lastActivityAt: wt.lastActivityAt,
        action: { kind: 'navigate', href: worktreeRoute(wt.id, wt.repoId ?? null) }
      }))
  };
}

export function scopeSource(): PaletteSource {
  return {
    group: 'Navigation',
    priority: 40,
    load: () => {
      const items: PaletteItem[] = [];
      items.push({
        id: 'scope:chats',
        label: 'Chats',
        group: 'Navigation',
        keywords: 'chats hub home',
        glyph: 'C',
        accent: 'var(--color-accent)',
        action: { kind: 'navigate', href: '/chats' }
      });
      items.push({
        id: 'scope:repos',
        label: 'Repos',
        group: 'Navigation',
        keywords: 'repos repositories',
        glyph: 'R',
        accent: 'var(--color-accent)',
        action: { kind: 'navigate', href: '/repos' }
      });
      items.push({
        id: 'scope:automations',
        label: 'Automations',
        group: 'Navigation',
        keywords: 'automations schedules periodic jobs ticket flows',
        glyph: 'A',
        accent: 'var(--color-accent)',
        action: { kind: 'navigate', href: '/automations' }
      });
      items.push({
        id: 'scope:settings',
        label: 'Settings',
        group: 'Navigation',
        keywords: 'settings preferences configuration',
        glyph: 'S',
        accent: 'var(--color-accent)',
        action: { kind: 'navigate', href: '/settings' }
      });
      return items;
    }
  };
}

export function ticketSource(
  tickets: TicketSummary[],
  getScopeHref?: (ticket: TicketSummary) => string | null
): PaletteSource {
  return {
    group: 'Tickets',
    priority: 30,
    load: () =>
      tickets.map((ticket) => {
        let href: string | null = null;
        if (ticket.repoId) {
          href = repoTicketRoute(ticket.repoId, ticket.id);
        } else if (ticket.worktreeId) {
          href = worktreeTicketRoute(ticket.worktreeId, null, ticket.id);
        }
        if (!href && getScopeHref) href = getScopeHref(ticket);
        return {
          id: `ticket:${ticket.id}`,
          label: ticket.title,
          group: 'Tickets',
          keywords: `ticket ${ticket.id} ${ticket.number ?? ''} ${ticket.agentId ?? ''} ${ticket.workspaceId ?? ''}`,
          action: href
            ? { kind: 'navigate' as const, href }
            : { kind: 'command' as const, handler: () => {} }
        };
      })
  };
}

export function contextspaceSource(
  docs: ContextspaceDocument[],
  scopeId?: string
): PaletteSource {
  return {
    group: 'Contextspace',
    priority: 40,
    load: () =>
      docs.map((doc) => {
        let href = '/settings?memory=1';
        if (scopeId && doc.kind) {
          const base = `/repos/${encodeURIComponent(scopeId)}`;
          href = `${base}/contextspace`;
        }
        return {
          id: `contextspace:${doc.id}`,
          label: doc.name,
          group: 'Contextspace',
          keywords: `contextspace doc ${doc.kind} ${doc.name}`,
          action: { kind: 'navigate', href }
        };
      })
  };
}

export function recentActionsSource(): PaletteSource {
  return {
    group: 'Recent',
    priority: 0,
    load: () => getRecentActions()
  };
}

export function commandSource(commands: PaletteItem[]): PaletteSource {
  return {
    group: 'Commands',
    priority: 5,
    load: () => commands
  };
}

export function loadAllItems(sources: PaletteSource[]): PaletteItem[] {
  const sorted = [...sources].sort((a, b) => a.priority - b.priority);
  const items: PaletteItem[] = [];
  const seenIds = new Set<string>();
  for (const source of sorted) {
    for (const item of source.load()) {
      if (!seenIds.has(item.id)) {
        seenIds.add(item.id);
        items.push(item);
      }
    }
  }
  return items;
}

export function filterItems(items: PaletteItem[], query: string): PaletteItem[] {
  if (!query.trim()) return items;
  const lower = query.toLowerCase();
  const terms = lower.split(/\s+/).filter(Boolean);
  return items.map((item) => {
    const haystack = `${item.label} ${item.group} ${item.keywords}`.toLowerCase();
    const termScores = terms.map((term) => fuzzyScore(haystack, term));
    const score = termScores.every((termScore) => termScore > 0)
      ? termScores.reduce((total, termScore) => total + termScore, 0)
      : 0;
    return { item, score };
  }).filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score)
    .map((entry) => entry.item);
}

function fuzzyScore(haystack: string, needle: string): number {
  if (!needle) return 1;
  const exact = haystack.indexOf(needle);
  if (exact >= 0) return 1000 - exact;
  let cursor = 0;
  let score = 0;
  for (const char of needle) {
    const found = haystack.indexOf(char, cursor);
    if (found < 0) return 0;
    score += Math.max(1, 40 - (found - cursor));
    cursor = found + 1;
  }
  return score;
}

function compareActivity<T extends { lastActivityAt?: string | null; updatedAt?: string | null }>(a: T, b: T): number {
  return activityMillis(b) - activityMillis(a);
}

function activityMillis(value: { lastActivityAt?: string | null; updatedAt?: string | null }): number {
  const raw = value.lastActivityAt ?? value.updatedAt;
  return raw ? Date.parse(raw) || 0 : 0;
}

function compactMeta(values: Array<string | null | undefined>): string | null {
  const parts = values.map((value) => value?.trim()).filter((value): value is string => Boolean(value));
  return parts.length ? parts.join(' · ') : null;
}

function shortId(id: string): string {
  const cleaned = id.trim();
  if (!cleaned) return '';
  const tail = cleaned.includes('-') ? cleaned.split('-').filter(Boolean).at(-1) ?? cleaned : cleaned;
  return `#${tail.slice(0, 7)}`;
}

function relativeActivity(value: string | null | undefined): string | null {
  if (!value) return null;
  const millis = Date.parse(value);
  if (!Number.isFinite(millis)) return null;
  const delta = Date.now() - millis;
  if (delta < 60_000) return 'just now';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`;
  return `${Math.floor(delta / 86_400_000)}d ago`;
}
