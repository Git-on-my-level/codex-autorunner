import { describe, expect, it, beforeEach } from 'vitest';
import {
  threadSource,
  scopeSource,
  ticketSource,
  contextspaceSource,
  recentActionsSource,
  commandSource,
  loadAllItems,
  filterItems,
  recordRecentAction,
  getRecentActions,
  clearRecentActions
} from './sources';
import type { PaletteSource } from './types';
import type { PmaChatSummary, TicketSummary, ContextspaceDocument, RepoSummary, WorktreeSummary } from '$lib/viewModels/domain';

beforeEach(() => {
  clearRecentActions();
});

describe('threadSource', () => {
  it('produces palette items from threads', () => {
    const threads: PmaChatSummary[] = [
      {
        id: 'thread-1',
        title: 'Fix login bug',
        lifecycleStatus: 'active',
        status: 'idle',
        agentId: 'codex',
        agentProfile: null,
        model: null,
        repoId: null,
        worktreeId: null,
        ticketId: null,
        isTicketFlow: false,
        progressPercent: null,
        updatedAt: null,
        raw: {}
      }
    ];
    const source = threadSource(threads);
    expect(source.group).toBe('Threads');
    const items = source.load();
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe('Fix login bug');
    expect(items[0].action.kind).toBe('navigate');
    if (items[0].action.kind === 'navigate') {
      expect(items[0].action.href).toContain('thread-1');
    }
  });
});

describe('scopeSource', () => {
  it('produces navigation items for repos and worktrees', () => {
    const repos: RepoSummary[] = [
      { id: 'repo-1', name: 'my-repo', path: null, status: 'idle', defaultBranch: null, worktreeCount: 0, activeRuns: 0, openTickets: 0, lastActivityAt: null, raw: {} }
    ];
    const worktrees: WorktreeSummary[] = [
      { id: 'wt-1', repoId: 'repo-1', name: 'feature-branch', path: null, branch: 'feature', status: 'idle', activeRuns: 0, openTickets: 0, lastActivityAt: null, raw: {} }
    ];
    const source = scopeSource(repos, worktrees);
    const items = source.load();
    const labels = items.map((i) => i.label);
    expect(labels).toContain('Chats');
    expect(labels).toContain('Repos');
    expect(labels).toContain('Settings');
    expect(labels).toContain('my-repo');
    expect(labels).toContain('feature-branch');
  });
});

describe('ticketSource', () => {
  it('produces items from tickets', () => {
    const tickets: TicketSummary[] = [
      {
        id: 'TICKET-001',
        number: 1,
        title: 'Add palette',
        status: 'idle',
        workspaceKind: 'repo',
        workspaceId: 'repo-1',
        workspacePath: null,
        repoId: 'repo-1',
        worktreeId: null,
        path: null,
        ticketPath: null,
        agentId: null,
        chatKey: null,
        runId: null,
        updatedAt: null,
        durationSeconds: null,
        diffStats: null,
        errors: [],
        raw: {}
      }
    ];
    const source = ticketSource(tickets);
    const items = source.load();
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe('Add palette');
  });
});

describe('contextspaceSource', () => {
  it('produces items from contextspace docs', () => {
    const docs: ContextspaceDocument[] = [
      { id: 'spec', name: 'spec.md', kind: 'spec', content: '', updatedAt: null, isPinned: true, raw: {} }
    ];
    const source = contextspaceSource(docs);
    const items = source.load();
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe('spec.md');
  });
});

describe('recentActionsSource', () => {
  it('returns empty list when no recent actions', () => {
    const source = recentActionsSource();
    expect(source.load()).toEqual([]);
  });

  it('returns recorded actions', () => {
    recordRecentAction({
      id: 'test-1',
      label: 'Test action',
      group: 'Test',
      keywords: '',
      action: { kind: 'navigate', href: '/test' }
    });
    const source = recentActionsSource();
    const items = source.load();
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe('Test action');
  });
});

describe('commandSource', () => {
  it('passes through command items', () => {
    const commands = [
      { id: 'cmd:toggle', label: 'Toggle', group: 'Commands', keywords: '', action: { kind: 'command' as const, handler: () => {} } }
    ];
    const source = commandSource(commands);
    expect(source.load()).toEqual(commands);
  });
});

describe('loadAllItems', () => {
  it('merges and deduplicates items from multiple sources', () => {
    const s1: ReturnType<typeof commandSource> = {
      group: 'A',
      priority: 10,
      load: () => [{ id: 'x', label: 'X', group: 'A', keywords: '', action: { kind: 'navigate', href: '/x' } }]
    };
    const s2: ReturnType<typeof commandSource> = {
      group: 'B',
      priority: 20,
      load: () => [{ id: 'x', label: 'X', group: 'A', keywords: '', action: { kind: 'navigate', href: '/x' } }]
    };
    const items = loadAllItems([s1, s2]);
    expect(items).toHaveLength(1);
    expect(items[0].id).toBe('x');
  });

  it('sorts by priority', () => {
    const s1: PaletteSource = { group: 'B', priority: 20, load: () => [{ id: 'b', label: 'B', group: 'B', keywords: '', action: { kind: 'navigate' as const, href: '/b' } }] };
    const s2: PaletteSource = { group: 'A', priority: 10, load: () => [{ id: 'a', label: 'A', group: 'A', keywords: '', action: { kind: 'navigate' as const, href: '/a' } }] };
    const items = loadAllItems([s1, s2]);
    expect(items[0].id).toBe('a');
    expect(items[1].id).toBe('b');
  });
});

describe('filterItems', () => {
  const items = [
    { id: '1', label: 'Fix login bug', group: 'Threads', keywords: 'thread-1 codex', action: { kind: 'navigate' as const, href: '/1' } },
    { id: '2', label: 'my-repo', group: 'Scopes', keywords: 'repo my-repo', action: { kind: 'navigate' as const, href: '/2' } },
    { id: '3', label: 'Add palette', group: 'Tickets', keywords: 'ticket-1', action: { kind: 'navigate' as const, href: '/3' } }
  ];

  it('returns all items with empty query', () => {
    expect(filterItems(items, '')).toHaveLength(3);
  });

  it('filters by label', () => {
    const result = filterItems(items, 'login');
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('1');
  });

  it('filters by group', () => {
    const result = filterItems(items, 'scopes');
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('2');
  });

  it('filters by keywords', () => {
    const result = filterItems(items, 'codex');
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('1');
  });

  it('supports multi-term search', () => {
    const result = filterItems(items, 'add palette');
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('3');
  });

  it('returns empty for no matches', () => {
    expect(filterItems(items, 'xyz')).toHaveLength(0);
  });
});

describe('recordRecentAction / getRecentActions', () => {
  it('records and retrieves actions', () => {
    recordRecentAction({
      id: 'r1',
      label: 'Recent 1',
      group: 'Test',
      keywords: '',
      action: { kind: 'navigate', href: '/r1' }
    });
    const actions = getRecentActions();
    expect(actions).toHaveLength(1);
    expect(actions[0].id).toBe('r1');
  });

  it('deduplicates by id', () => {
    recordRecentAction({ id: 'r1', label: 'First', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } });
    recordRecentAction({ id: 'r1', label: 'Second', group: 'G', keywords: '', action: { kind: 'navigate', href: '/b' } });
    expect(getRecentActions()).toHaveLength(1);
    expect(getRecentActions()[0].label).toBe('Second');
  });

  it('limits to 20 items', () => {
    for (let i = 0; i < 25; i++) {
      recordRecentAction({ id: `r${i}`, label: `Item ${i}`, group: 'G', keywords: '', action: { kind: 'navigate', href: `/${i}` } });
    }
    expect(getRecentActions()).toHaveLength(20);
    expect(getRecentActions()[0].id).toBe('r24');
  });
});
