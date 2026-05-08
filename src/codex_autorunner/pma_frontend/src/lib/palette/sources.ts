import type { PaletteItem, PaletteSource } from './types';
import type { PmaChatSummary, TicketSummary, ContextspaceDocument } from '$lib/viewModels/domain';
import type { RepoSummary, WorktreeSummary } from '$lib/viewModels/domain';
import {
  repoRoute,
  repoTicketRoute,
  worktreeRoute,
  worktreeTicketRoute
} from '$lib/viewModels/routes';

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

export function threadSource(threads: PmaChatSummary[]): PaletteSource {
  return {
    group: 'Threads',
    priority: 10,
    load: () =>
      threads.map((thread) => ({
        id: `thread:${thread.id}`,
        label: thread.title,
        group: 'Threads',
        keywords: `${thread.id} ${thread.agentId ?? ''} ${thread.ticketId ?? ''} ${thread.model ?? ''}`,
        action: { kind: 'navigate', href: `/chats?chat=${encodeURIComponent(thread.id)}` }
      }))
  };
}

export function scopeSource(
  repos: RepoSummary[],
  worktrees: WorktreeSummary[]
): PaletteSource {
  return {
    group: 'Scopes',
    priority: 20,
    load: () => {
      const items: PaletteItem[] = [];
      items.push({
        id: 'scope:chats',
        label: 'Chats',
        group: 'Scopes',
        keywords: 'chats hub home',
        action: { kind: 'navigate', href: '/chats' }
      });
      items.push({
        id: 'scope:repos',
        label: 'Repos',
        group: 'Scopes',
        keywords: 'repos repositories',
        action: { kind: 'navigate', href: '/repos' }
      });
      items.push({
        id: 'scope:settings',
        label: 'Settings',
        group: 'Scopes',
        keywords: 'settings preferences configuration',
        action: { kind: 'navigate', href: '/settings' }
      });
      for (const repo of repos) {
        items.push({
          id: `scope:repo:${repo.id}`,
          label: repo.name,
          group: 'Scopes',
          keywords: `repo ${repo.id} ${repo.path ?? ''}`,
          action: { kind: 'navigate', href: repoRoute(repo.id) }
        });
      }
      for (const wt of worktrees) {
        items.push({
          id: `scope:worktree:${wt.id}`,
          label: wt.name,
          group: 'Scopes',
          keywords: `worktree ${wt.id} ${wt.branch ?? ''} ${wt.repoId ?? ''}`,
          action: { kind: 'navigate', href: worktreeRoute(wt.id, wt.repoId ?? null) }
        });
      }
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
  return items.filter((item) => {
    const haystack = `${item.label} ${item.group} ${item.keywords}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}
