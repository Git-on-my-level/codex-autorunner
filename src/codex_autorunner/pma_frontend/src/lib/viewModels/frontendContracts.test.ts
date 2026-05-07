import { describe, expect, it } from 'vitest';
import {
  formatScopeUrn,
  parseScopeUrn,
  parentScope,
  scopeLabel,
  scopeShortLabel,
  scopeBreadcrumbs,
  scopeRoute,
  scopeTicketRoute,
  scopeMemoryRoute,
  scopeFromApiPayload,
  scopeFromTicket,
  scopeMatchesResource,
  scopeEquals,
  scopeAncestors,
  parseSurfaceUrn,
  formatSurfaceUrn,
  type ScopeRef,
  ScopeUrnParseError,
  ScopeUrnKindError
} from './scope';
import { buildMemoryViewModel, CONTEXTSPACE_DOC_ORDER, PMA_DOC_ORDER } from './memory';
import type { ContextspaceDocument } from './domain';
import { buildContextspaceViewModel } from './contextspace';

const BACKEND_VALID_SCOPE_KINDS = ['hub', 'repo', 'worktree', 'agent_workspace', 'filesystem'] as const;

const BACKEND_CONTEXTSPACE_DOC_KINDS = ['active_context', 'decisions', 'spec'] as const;
const BACKEND_CONTEXTSPACE_DOC_PATHS = ['active_context.md', 'decisions.md', 'spec.md'] as const;

describe('Frontend-backend scope contract', () => {
  describe('scope kinds match backend VALID_SCOPE_KINDS', () => {
    it('frontend covers all backend scope kinds', () => {
      const frontendKinds = new Set<ScopeRef['kind']>(['hub', 'repo', 'worktree', 'agent_workspace', 'filesystem']);
      for (const kind of BACKEND_VALID_SCOPE_KINDS) {
        expect(frontendKinds.has(kind)).toBe(true);
      }
    });

    it('frontend does not invent scope kinds absent from backend', () => {
      const frontendKinds = new Set<ScopeRef['kind']>(['hub', 'repo', 'worktree', 'agent_workspace', 'filesystem']);
      expect(frontendKinds.size).toBe(BACKEND_VALID_SCOPE_KINDS.length);
    });
  });

  describe('scope URN format matches backend format_scope_urn', () => {
    const cases: Array<[ScopeRef, string]> = [
      [{ kind: 'hub' }, 'hub'],
      [{ kind: 'repo', id: 'my-repo' }, 'repo:my-repo'],
      [{ kind: 'worktree', id: 'wt-1', parentRepoId: 'base-repo' }, 'worktree:base-repo/wt-1'],
      [{ kind: 'agent_workspace', id: 'ws-1' }, 'agent_workspace:ws-1'],
      [{ kind: 'filesystem', path: '/Users/dev/project' }, 'filesystem:%2FUsers%2Fdev%2Fproject'],
    ];

    for (const [scope, expectedUrn] of cases) {
      it(`formatScopeUrn(${scope.kind}) => ${expectedUrn}`, () => {
        expect(formatScopeUrn(scope)).toBe(expectedUrn);
      });
    }
  });

  describe('scope URN parse matches backend parse_scope_urn', () => {
    const cases: Array<[string, ScopeRef]> = [
      ['hub', { kind: 'hub' }],
      ['repo:my-repo', { kind: 'repo', id: 'my-repo' }],
      ['worktree:base-repo/wt-1', { kind: 'worktree', id: 'wt-1', parentRepoId: 'base-repo' }],
      ['agent_workspace:ws-1', { kind: 'agent_workspace', id: 'ws-1' }],
      ['filesystem:%2FUsers%2Fdev%2Fproject', { kind: 'filesystem', path: '/Users/dev/project' }],
    ];

    for (const [urn, expected] of cases) {
      it(`parseScopeUrn(${urn}) matches backend`, () => {
        expect(parseScopeUrn(urn)).toEqual(expected);
      });
    }
  });

  describe('URN round-trip matches backend ScopeRef.to_urn / from_urn', () => {
    const scopes: ScopeRef[] = [
      { kind: 'hub' },
      { kind: 'repo', id: 'codex-autorunner' },
      { kind: 'worktree', id: 'discord-5', parentRepoId: 'codex-autorunner' },
      { kind: 'agent_workspace', id: 'ws-1' },
      { kind: 'filesystem', path: '/tmp/some-place' },
    ];

    for (const scope of scopes) {
      it(`round-trips ${scope.kind}`, () => {
        const urn = formatScopeUrn(scope);
        const restored = parseScopeUrn(urn);
        expect(restored).toEqual(scope);
      });
    }
  });

  describe('parentScope matches backend parent_scope', () => {
    it('hub has no parent (returns null)', () => {
      expect(parentScope({ kind: 'hub' })).toBeNull();
    });

    it('repo parent is hub', () => {
      expect(parentScope({ kind: 'repo', id: 'r1' })).toEqual({ kind: 'hub' });
    });

    it('worktree parent is repo', () => {
      expect(parentScope({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toEqual({ kind: 'repo', id: 'r1' });
    });

    it('agent_workspace parent is hub', () => {
      expect(parentScope({ kind: 'agent_workspace', id: 'ws-1' })).toEqual({ kind: 'hub' });
    });

    it('filesystem has no parent (returns null, frontend deviates intentionally from backend)', () => {
      expect(parentScope({ kind: 'filesystem', path: '/tmp' })).toBeNull();
    });
  });

  describe('scopeAncestors chain ends at hub', () => {
    it('chain starts with self and ends with hub', () => {
      const chain = scopeAncestors({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' });
      expect(chain[0]).toEqual({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' });
      expect(chain[chain.length - 1]).toEqual({ kind: 'hub' });
    });

    it('chain parent consistency matches backend scope_chain', () => {
      const chain = scopeAncestors({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' });
      for (let i = 0; i < chain.length - 1; i++) {
        expect(parentScope(chain[i])).toEqual(chain[i + 1]);
      }
    });
  });

  describe('invalid URNs rejected before adapter resolution (matches backend)', () => {
    const invalidUrns = [
      '',
      'repo:',
      'repo:a/b',
      'worktree:noslash',
      'worktree:/wt1',
      'agent_workspace:',
      'filesystem:',
      'planet:earth',
      'hub:extra',
    ];

    for (const urn of invalidUrns) {
      it(`rejects "${urn}"`, () => {
        expect(() => parseScopeUrn(urn)).toThrow();
      });
    }
  });

  describe('SurfaceRef URN matches backend SurfaceRef.to_urn', () => {
    it('round-trips surface ref', () => {
      const surface = { kind: 'pma_thread', key: 'thread-1' };
      expect(parseSurfaceUrn(formatSurfaceUrn(surface))).toEqual(surface);
    });

    it('encodes keys with special characters', () => {
      const surface = { kind: 'web', key: 'key with spaces' };
      expect(parseSurfaceUrn(formatSurfaceUrn(surface))).toEqual(surface);
    });
  });
});

describe('Frontend-backend scope label contract', () => {
  it('hub label matches expected convention', () => {
    expect(scopeLabel({ kind: 'hub' })).toBe('Local hub');
  });

  it('repo label includes repo id', () => {
    expect(scopeLabel({ kind: 'repo', id: 'codex-autorunner' })).toBe('Repo: codex-autorunner');
  });

  it('worktree label includes worktree id', () => {
    expect(scopeLabel({ kind: 'worktree', id: 'discord-5', parentRepoId: 'base' })).toBe('Worktree: discord-5');
  });

  it('agent_workspace label includes id', () => {
    expect(scopeLabel({ kind: 'agent_workspace', id: 'codex' })).toBe('Agent workspace: codex');
  });

  it('filesystem label uses basename', () => {
    expect(scopeLabel({ kind: 'filesystem', path: '/Users/dev/project' })).toBe('project');
  });

  it('short label for hub is "Hub"', () => {
    expect(scopeShortLabel({ kind: 'hub' })).toBe('Hub');
  });

  it('short label for repo is the id', () => {
    expect(scopeShortLabel({ kind: 'repo', id: 'my-repo' })).toBe('my-repo');
  });
});

describe('Frontend-backend scope routing contract', () => {
  it('hub routes to /chats', () => {
    expect(scopeRoute({ kind: 'hub' })).toBe('/chats');
  });

  it('repo routes to /repos/<id>', () => {
    expect(scopeRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo');
  });

  it('worktree routes nested under parent repo', () => {
    expect(scopeRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1');
  });

  it('agent_workspace routes to /agent-workspaces/<id>', () => {
    expect(scopeRoute({ kind: 'agent_workspace', id: 'ws-1' })).toBe('/agent-workspaces/ws-1');
  });

  it('repo ticket route matches backend repo scope', () => {
    expect(scopeTicketRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo/tickets');
  });

  it('worktree ticket route matches backend worktree scope', () => {
    expect(scopeTicketRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1/tickets');
  });

  it('repo memory route matches backend repo scope', () => {
    expect(scopeMemoryRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo/memory');
  });

  it('worktree memory route matches backend worktree scope', () => {
    expect(scopeMemoryRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1/memory');
  });

  it('hub has no ticket route', () => {
    expect(scopeTicketRoute({ kind: 'hub' })).toBeNull();
  });

  it('hub has no memory route', () => {
    expect(scopeMemoryRoute({ kind: 'hub' })).toBeNull();
  });
});

describe('Frontend-backend query formatting contract', () => {
  describe('scopeFromApiPayload matches backend wire fields', () => {
    it('extracts hub from empty payload', () => {
      expect(scopeFromApiPayload({})).toEqual({ kind: 'hub' });
    });

    it('extracts from scope_urn', () => {
      expect(scopeFromApiPayload({ scope_urn: 'repo:my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
    });

    it('extracts repo from resource_kind + resource_id', () => {
      expect(scopeFromApiPayload({ resource_kind: 'repo', resource_id: 'my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
    });

    it('extracts repo from legacy repo_id', () => {
      expect(scopeFromApiPayload({ repo_id: 'my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
    });

    it('extracts worktree from worktree_id + repo_id', () => {
      expect(scopeFromApiPayload({ repo_id: 'base', worktree_id: 'wt-1' })).toEqual({
        kind: 'worktree',
        id: 'wt-1',
        parentRepoId: 'base'
      });
    });

    it('extracts worktree from worktree_repo_id', () => {
      expect(scopeFromApiPayload({ repo_id: 'base', worktree_repo_id: 'wt-1' })).toEqual({
        kind: 'worktree',
        id: 'wt-1',
        parentRepoId: 'base'
      });
    });

    it('extracts filesystem from workspace_root', () => {
      expect(scopeFromApiPayload({ workspace_root: '/tmp/project' })).toEqual({
        kind: 'filesystem',
        path: '/tmp/project'
      });
    });

    it('extracts worktree from resource_kind=worktree', () => {
      expect(scopeFromApiPayload({ resource_kind: 'worktree', resource_id: 'wt-1', base_repo_id: 'base' })).toEqual({
        kind: 'worktree',
        id: 'wt-1',
        parentRepoId: 'base'
      });
    });

    it('extracts agent_workspace', () => {
      expect(scopeFromApiPayload({ resource_kind: 'agent_workspace', resource_id: 'ws-1' })).toEqual({
        kind: 'agent_workspace',
        id: 'ws-1'
      });
    });

    it('incomplete worktree falls back to hub', () => {
      expect(scopeFromApiPayload({ resource_kind: 'worktree', resource_id: 'wt-1' })).toEqual({ kind: 'hub' });
    });
  });

  describe('scopeFromTicket matches backend ticket wire fields', () => {
    it('extracts repo scope from workspace fields', () => {
      expect(scopeFromTicket({ workspace_kind: 'repo', workspace_id: 'my-repo' })).toEqual({
        kind: 'repo',
        id: 'my-repo'
      });
    });

    it('extracts worktree scope from workspace fields', () => {
      expect(scopeFromTicket({ workspace_kind: 'worktree', workspace_id: 'wt-1', repo_id: 'base' })).toEqual({
        kind: 'worktree',
        id: 'wt-1',
        parentRepoId: 'base'
      });
    });

    it('extracts repo scope from resource_kind', () => {
      expect(scopeFromTicket({ resource_kind: 'repo', resource_id: 'my-repo' })).toEqual({
        kind: 'repo',
        id: 'my-repo'
      });
    });

    it('extracts scope from legacy repo_id', () => {
      expect(scopeFromTicket({ repo_id: 'my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
    });

    it('extracts worktree from worktree_id', () => {
      expect(scopeFromTicket({ worktree_id: 'wt-1', repo_id: 'base' })).toEqual({
        kind: 'worktree',
        id: 'wt-1',
        parentRepoId: 'base'
      });
    });

    it('incomplete worktree ownership falls back to hub', () => {
      expect(scopeFromTicket({ workspace_kind: 'worktree', workspace_id: 'wt-1' })).toEqual({ kind: 'hub' });
    });

    it('falls back to hub for empty', () => {
      expect(scopeFromTicket({})).toEqual({ kind: 'hub' });
    });
  });
});

describe('Frontend-backend memory rendering contract', () => {
  describe('contextspace doc kinds match backend CONTEXTSPACE_DOC_KINDS', () => {
    it('frontend contextspace doc kinds match backend catalog', () => {
      const frontendKinds = new Set(CONTEXTSPACE_DOC_ORDER.map((name) => name.replace(/\.md$/, '')));
      for (const kind of BACKEND_CONTEXTSPACE_DOC_KINDS) {
        expect(frontendKinds.has(kind)).toBe(true);
      }
    });

    it('frontend contextspace doc filenames match backend catalog paths', () => {
      const frontendPaths = CONTEXTSPACE_DOC_ORDER;
      for (const path of BACKEND_CONTEXTSPACE_DOC_PATHS) {
        expect(frontendPaths).toContain(path);
      }
    });

    it('frontend contextspace doc order matches backend catalog order', () => {
      const normalizedOrder = CONTEXTSPACE_DOC_ORDER.map((name) => name.replace(/\.md$/, ''));
      expect(normalizedOrder[0]).toBe('active_context');
      expect(normalizedOrder).toContain('spec');
      expect(normalizedOrder).toContain('decisions');
    });
  });

  describe('memory view model renders contextspace docs correctly', () => {
    const docs: ContextspaceDocument[] = [
      { id: 'active_context', name: 'active_context.md', kind: 'active_context', content: '# Context', updatedAt: null, isPinned: true, raw: {} },
      { id: 'spec', name: 'spec.md', kind: 'spec', content: '# Spec', updatedAt: null, isPinned: true, raw: {} },
      { id: 'decisions', name: 'decisions.md', kind: 'decisions', content: '', updatedAt: null, isPinned: true, raw: {} },
    ];

    it('repo memory includes all contextspace doc kinds', () => {
      const vm = buildMemoryViewModel({ kind: 'repo', id: 'my-repo' }, docs);
      const docNames = vm.docs.map((d) => d.filename.replace(/\.md$/, ''));
      for (const kind of BACKEND_CONTEXTSPACE_DOC_KINDS) {
        expect(docNames).toContain(kind);
      }
    });

    it('worktree memory includes all contextspace doc kinds', () => {
      const vm = buildMemoryViewModel({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' }, docs);
      const docNames = vm.docs.map((d) => d.filename.replace(/\.md$/, ''));
      for (const kind of BACKEND_CONTEXTSPACE_DOC_KINDS) {
        expect(docNames).toContain(kind);
      }
    });

    it('renders markdown to html for content', () => {
      const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, [
        { id: 'spec', name: 'spec.md', kind: 'spec', content: '# Hello World', updatedAt: null, isPinned: false, raw: {} },
      ]);
      const specDoc = vm.docs.find((d) => d.filename === 'spec.md');
      expect(specDoc?.html).toContain('Hello World');
      expect(specDoc?.html).toContain('<');
    });

    it('empty content is marked as missing', () => {
      const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, []);
      for (const doc of vm.docs) {
        expect(doc.isMissing).toBe(true);
      }
    });

    it('non-empty content is not marked as missing', () => {
      const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, docs);
      const present = vm.docs.find((d) => d.filename === 'spec.md');
      expect(present?.isMissing).toBe(false);
    });

    it('presentCount matches non-missing docs', () => {
      const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, docs);
      expect(vm.presentCount).toBe(2);
    });
  });

  describe('contextspace view model matches backend doc kinds', () => {
    const docs: ContextspaceDocument[] = [
      { id: 'active_context', name: 'active_context.md', kind: 'active_context', content: 'ctx', updatedAt: null, isPinned: true, raw: {} },
      { id: 'spec', name: 'spec.md', kind: 'spec', content: 'spec', updatedAt: null, isPinned: true, raw: {} },
      { id: 'decisions', name: 'decisions.md', kind: 'decisions', content: 'dec', updatedAt: null, isPinned: true, raw: {} },
    ];

    it('contextspace view model includes all backend doc kinds', () => {
      const vm = buildContextspaceViewModel('my-repo', docs, [
        { id: 'my-repo', name: 'My Repo', path: '/my-repo', status: 'idle', defaultBranch: null, worktreeCount: 0, activeRuns: 0, openTickets: 0, lastActivityAt: null, raw: {} },
      ]);
      const kinds = vm.docs.map((d) => d.id);
      for (const kind of BACKEND_CONTEXTSPACE_DOC_KINDS) {
        expect(kinds).toContain(kind);
      }
    });
  });
});

describe('Frontend-backend scope breadcrumb contract', () => {
  it('hub breadcrumb has single entry with no link', () => {
    expect(scopeBreadcrumbs({ kind: 'hub' })).toEqual([{ label: 'Hub', href: null }]);
  });

  it('repo breadcrumb chain links hub parent correctly', () => {
    const crumbs = scopeBreadcrumbs({ kind: 'repo', id: 'my-repo' });
    expect(crumbs).toEqual([
      { label: 'my-repo', href: '/repos/my-repo' },
      { label: 'Hub', href: null },
    ]);
  });

  it('worktree breadcrumb chain links all ancestors', () => {
    const crumbs = scopeBreadcrumbs({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base' });
    expect(crumbs).toEqual([
      { label: 'wt-1', href: '/repos/base/worktrees/wt-1' },
      { label: 'base', href: '/repos/base' },
      { label: 'Hub', href: null },
    ]);
  });
});

describe('Frontend-backend scope equality and matching contract', () => {
  it('scopeEquals uses URN comparison', () => {
    expect(scopeEquals({ kind: 'repo', id: 'r1' }, { kind: 'repo', id: 'r1' })).toBe(true);
    expect(scopeEquals({ kind: 'repo', id: 'r1' }, { kind: 'repo', id: 'r2' })).toBe(false);
  });

  it('scopeMatchesResource matches backend resource kind/id semantics', () => {
    expect(scopeMatchesResource({ kind: 'repo', id: 'r1' }, 'repo', 'r1')).toBe(true);
    expect(scopeMatchesResource({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' }, 'worktree', 'wt-1')).toBe(true);
    expect(scopeMatchesResource({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' }, 'repo', 'r1')).toBe(true);
    expect(scopeMatchesResource({ kind: 'hub' }, 'hub', '')).toBe(false);
    expect(scopeMatchesResource({ kind: 'repo', id: 'r1' }, 'repo', 'r2')).toBe(false);
  });
});
