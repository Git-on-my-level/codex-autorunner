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
  ScopeUrnParseError,
  ScopeUrnKindError,
  parseSurfaceUrn,
  formatSurfaceUrn,
  type ScopeRef
} from './scope';

describe('formatScopeUrn', () => {
  it('formats hub scope', () => {
    expect(formatScopeUrn({ kind: 'hub' })).toBe('hub');
  });

  it('formats repo scope', () => {
    expect(formatScopeUrn({ kind: 'repo', id: 'my-repo' })).toBe('repo:my-repo');
  });

  it('formats worktree scope', () => {
    expect(formatScopeUrn({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base-repo' })).toBe('worktree:base-repo/wt-1');
  });

  it('formats agent_workspace scope', () => {
    expect(formatScopeUrn({ kind: 'agent_workspace', id: 'ws-1' })).toBe('agent_workspace:ws-1');
  });

  it('formats filesystem scope with encoding', () => {
    expect(formatScopeUrn({ kind: 'filesystem', path: '/Users/dev/project' })).toBe('filesystem:%2FUsers%2Fdev%2Fproject');
  });
});

describe('parseScopeUrn', () => {
  it('parses hub', () => {
    expect(parseScopeUrn('hub')).toEqual({ kind: 'hub' });
  });

  it('parses repo URN', () => {
    const scope = parseScopeUrn('repo:my-repo');
    expect(scope).toEqual({ kind: 'repo', id: 'my-repo' });
  });

  it('parses worktree URN', () => {
    const scope = parseScopeUrn('worktree:base-repo/wt-1');
    expect(scope).toEqual({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base-repo' });
  });

  it('parses agent_workspace URN', () => {
    expect(parseScopeUrn('agent_workspace:ws-1')).toEqual({ kind: 'agent_workspace', id: 'ws-1' });
  });

  it('parses filesystem URN with decoding', () => {
    const scope = parseScopeUrn('filesystem:%2FUsers%2Fdev%2Fproject');
    expect(scope).toEqual({ kind: 'filesystem', path: '/Users/dev/project' });
  });

  it('rejects filesystem URNs with malformed percent escapes like the backend parser', () => {
    expect(() => parseScopeUrn('filesystem:%2FUsers%2Gproject')).toThrow(ScopeUrnParseError);
    expect(() => parseScopeUrn('filesystem:%2FUsers%')).toThrow(ScopeUrnParseError);
  });

  it('decodes filesystem URNs with replacement for invalid UTF-8 byte sequences like the backend parser', () => {
    expect(parseScopeUrn('filesystem:%E0%A4')).toEqual({ kind: 'filesystem', path: '�' });
  });

  it('round-trips hub', () => {
    expect(formatScopeUrn(parseScopeUrn('hub'))).toBe('hub');
  });

  it('round-trips repo', () => {
    expect(formatScopeUrn(parseScopeUrn('repo:codex-autorunner'))).toBe('repo:codex-autorunner');
  });

  it('round-trips worktree', () => {
    const urn = 'worktree:codex-autorunner/discord-5';
    expect(formatScopeUrn(parseScopeUrn(urn))).toBe(urn);
  });

  it('round-trips agent_workspace', () => {
    const urn = 'agent_workspace:codex';
    expect(formatScopeUrn(parseScopeUrn(urn))).toBe(urn);
  });

  it('round-trips filesystem', () => {
    const urn = 'filesystem:%2FUsers%2Fdev%2Fproject';
    expect(formatScopeUrn(parseScopeUrn(urn))).toBe(urn);
  });

  it('rejects empty string', () => {
    expect(() => parseScopeUrn('')).toThrow(ScopeUrnParseError);
  });

  it('rejects unknown kind', () => {
    expect(() => parseScopeUrn('unknown:id')).toThrow(ScopeUrnKindError);
  });

  it('rejects repo without id', () => {
    expect(() => parseScopeUrn('repo:')).toThrow(ScopeUrnParseError);
  });

  it('rejects repo with slash', () => {
    expect(() => parseScopeUrn('repo:has/slash')).toThrow(ScopeUrnParseError);
  });

  it('rejects worktree without slash', () => {
    expect(() => parseScopeUrn('worktree:noslash')).toThrow(ScopeUrnParseError);
  });

  it('rejects worktree with leading slash', () => {
    expect(() => parseScopeUrn('worktree:/wt-1')).toThrow(ScopeUrnParseError);
  });

  it('rejects worktree with trailing slash', () => {
    expect(() => parseScopeUrn('worktree:repo/')).toThrow(ScopeUrnParseError);
  });

  it('rejects agent_workspace without id', () => {
    expect(() => parseScopeUrn('agent_workspace:')).toThrow(ScopeUrnParseError);
  });

  it('rejects hub with path component', () => {
    expect(() => parseScopeUrn('hub:extra')).toThrow(ScopeUrnParseError);
  });
});

describe('parentScope', () => {
  it('returns null for hub', () => {
    expect(parentScope({ kind: 'hub' })).toBeNull();
  });

  it('returns hub for repo', () => {
    expect(parentScope({ kind: 'repo', id: 'r1' })).toEqual({ kind: 'hub' });
  });

  it('returns parent repo for worktree', () => {
    expect(parentScope({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toEqual({ kind: 'repo', id: 'r1' });
  });

  it('returns hub for agent_workspace', () => {
    expect(parentScope({ kind: 'agent_workspace', id: 'ws-1' })).toEqual({ kind: 'hub' });
  });

  it('returns null for filesystem', () => {
    expect(parentScope({ kind: 'filesystem', path: '/tmp' })).toBeNull();
  });
});

describe('scopeLabel', () => {
  it('labels hub', () => {
    expect(scopeLabel({ kind: 'hub' })).toBe('Local hub');
  });

  it('labels repo', () => {
    expect(scopeLabel({ kind: 'repo', id: 'codex-autorunner' })).toBe('Repo: codex-autorunner');
  });

  it('labels worktree', () => {
    expect(scopeLabel({ kind: 'worktree', id: 'discord-5', parentRepoId: 'codex-autorunner' })).toBe('Worktree: discord-5');
  });

  it('labels agent_workspace', () => {
    expect(scopeLabel({ kind: 'agent_workspace', id: 'codex' })).toBe('Agent workspace: codex');
  });

  it('labels filesystem with basename', () => {
    expect(scopeLabel({ kind: 'filesystem', path: '/Users/dev/project' })).toBe('project');
  });

  it('labels filesystem roots without dropping the path', () => {
    expect(scopeLabel({ kind: 'filesystem', path: '/' })).toBe('/');
    expect(scopeShortLabel({ kind: 'filesystem', path: '/' })).toBe('/');
  });
});

describe('scopeShortLabel', () => {
  it('short-labels hub', () => {
    expect(scopeShortLabel({ kind: 'hub' })).toBe('Hub');
  });

  it('short-labels repo', () => {
    expect(scopeShortLabel({ kind: 'repo', id: 'codex-autorunner' })).toBe('codex-autorunner');
  });

  it('short-labels worktree', () => {
    expect(scopeShortLabel({ kind: 'worktree', id: 'discord-5', parentRepoId: 'base' })).toBe('discord-5');
  });
});

describe('scopeRoute', () => {
  it('routes hub to chats', () => {
    expect(scopeRoute({ kind: 'hub' })).toBe('/chats');
  });

  it('routes repo', () => {
    expect(scopeRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo');
  });

  it('routes worktree', () => {
    expect(scopeRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1');
  });

  it('routes agent_workspace scopes', () => {
    expect(scopeRoute({ kind: 'agent_workspace', id: 'ws-1' })).toBe('/agent-workspaces/ws-1');
  });

  it('routes filesystem as null', () => {
    expect(scopeRoute({ kind: 'filesystem', path: '/tmp' })).toBeNull();
  });

  it('encodes repo id with special characters', () => {
    expect(scopeRoute({ kind: 'repo', id: 'repo with spaces' })).toBe('/repos/repo%20with%20spaces');
  });
});

describe('scopeTicketRoute', () => {
  it('generates repo ticket route', () => {
    expect(scopeTicketRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo/tickets');
  });

  it('generates worktree ticket route', () => {
    expect(scopeTicketRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1/tickets');
  });

  it('returns null for hub', () => {
    expect(scopeTicketRoute({ kind: 'hub' })).toBeNull();
  });
});

describe('scopeMemoryRoute', () => {
  it('generates repo memory route', () => {
    expect(scopeMemoryRoute({ kind: 'repo', id: 'my-repo' })).toBe('/repos/my-repo/memory');
  });

  it('generates worktree memory route', () => {
    expect(scopeMemoryRoute({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' })).toBe('/repos/r1/worktrees/wt-1/memory');
  });

  it('returns null for hub', () => {
    expect(scopeMemoryRoute({ kind: 'hub' })).toBeNull();
  });
});

describe('scopeBreadcrumbs', () => {
  it('builds hub breadcrumb', () => {
    expect(scopeBreadcrumbs({ kind: 'hub' })).toEqual([{ label: 'Hub', href: null }]);
  });

  it('builds repo breadcrumb chain', () => {
    const crumbs = scopeBreadcrumbs({ kind: 'repo', id: 'my-repo' });
    expect(crumbs).toEqual([
      { label: 'my-repo', href: '/repos/my-repo' },
      { label: 'Hub', href: null }
    ]);
  });

  it('builds worktree breadcrumb chain', () => {
    const crumbs = scopeBreadcrumbs({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base' });
    expect(crumbs).toEqual([
      { label: 'wt-1', href: '/repos/base/worktrees/wt-1' },
      { label: 'base', href: '/repos/base' },
      { label: 'Hub', href: null }
    ]);
  });

  it('builds non-routable filesystem breadcrumbs without inventing a hub parent', () => {
    expect(scopeBreadcrumbs({ kind: 'filesystem', path: '/Users/dev/project' })).toEqual([
      { label: 'project', href: null }
    ]);
  });
});

describe('scopeAncestors', () => {
  it('returns just hub for hub', () => {
    expect(scopeAncestors({ kind: 'hub' })).toEqual([{ kind: 'hub' }]);
  });

  it('returns worktree then repo then hub for worktree', () => {
    const ancestors = scopeAncestors({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' });
    expect(ancestors).toEqual([
      { kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' },
      { kind: 'repo', id: 'r1' },
      { kind: 'hub' }
    ]);
  });
});

describe('scopeFromApiPayload', () => {
  it('extracts hub from empty payload', () => {
    expect(scopeFromApiPayload({})).toEqual({ kind: 'hub' });
  });

  it('extracts from scope_urn', () => {
    expect(scopeFromApiPayload({ scope_urn: 'repo:my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
  });

  it('extracts from urn', () => {
    expect(scopeFromApiPayload({ urn: 'repo:my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
  });

  it('extracts repo from resource_kind + resource_id', () => {
    expect(scopeFromApiPayload({ resource_kind: 'repo', resource_id: 'my-repo' })).toEqual({ kind: 'repo', id: 'my-repo' });
  });

  it('extracts repo from repo_id', () => {
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

  it('does not preserve incomplete worktree resource ownership from API payloads', () => {
    expect(scopeFromApiPayload({ resource_kind: 'worktree', resource_id: 'wt-1' })).toEqual({ kind: 'hub' });
  });

  it('extracts agent_workspace', () => {
    expect(scopeFromApiPayload({ resource_kind: 'agent_workspace', resource_id: 'ws-1' })).toEqual({
      kind: 'agent_workspace',
      id: 'ws-1'
    });
  });
});

describe('scopeFromTicket', () => {
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

  it('extracts worktree parent repo from legacy ticket frontmatter', () => {
    expect(scopeFromTicket({ worktree_id: 'wt-1', frontmatter: { base_repo_id: 'base' } })).toEqual({
      kind: 'worktree',
      id: 'wt-1',
      parentRepoId: 'base'
    });
  });

  it('does not preserve incomplete worktree ownership from tickets', () => {
    expect(scopeFromTicket({ workspace_kind: 'worktree', workspace_id: 'wt-1' })).toEqual({ kind: 'hub' });
    expect(scopeFromTicket({ worktree_id: 'wt-1' })).toEqual({ kind: 'hub' });
  });

  it('falls back to hub', () => {
    expect(scopeFromTicket({})).toEqual({ kind: 'hub' });
  });
});

describe('scopeMatchesResource', () => {
  it('matches repo scope to repo resource', () => {
    expect(scopeMatchesResource({ kind: 'repo', id: 'r1' }, 'repo', 'r1')).toBe(true);
  });

  it('matches worktree scope to worktree resource', () => {
    expect(scopeMatchesResource({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' }, 'worktree', 'wt-1')).toBe(true);
  });

  it('matches worktree parent repo', () => {
    expect(scopeMatchesResource({ kind: 'worktree', id: 'wt-1', parentRepoId: 'r1' }, 'repo', 'r1')).toBe(true);
  });

  it('does not match different repos', () => {
    expect(scopeMatchesResource({ kind: 'repo', id: 'r1' }, 'repo', 'r2')).toBe(false);
  });

  it('returns false for hub', () => {
    expect(scopeMatchesResource({ kind: 'hub' }, 'hub', '')).toBe(false);
  });
});

describe('scopeEquals', () => {
  it('equal repos', () => {
    expect(scopeEquals({ kind: 'repo', id: 'r1' }, { kind: 'repo', id: 'r1' })).toBe(true);
  });

  it('unequal repos', () => {
    expect(scopeEquals({ kind: 'repo', id: 'r1' }, { kind: 'repo', id: 'r2' })).toBe(false);
  });

  it('different kinds', () => {
    expect(scopeEquals({ kind: 'hub' }, { kind: 'repo', id: 'r1' })).toBe(false);
  });
});

describe('SurfaceRef URN round-trip', () => {
  it('formats and parses surface URN', () => {
    const surface = { kind: 'pma_thread', key: 'thread-1' };
    const urn = formatSurfaceUrn(surface);
    expect(parseSurfaceUrn(urn)).toEqual(surface);
  });

  it('handles encoded keys', () => {
    const surface = { kind: 'web', key: 'key with spaces' };
    const urn = formatSurfaceUrn(surface);
    expect(parseSurfaceUrn(urn)).toEqual(surface);
  });
});
