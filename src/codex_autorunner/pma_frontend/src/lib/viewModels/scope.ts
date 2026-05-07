export type ScopeKind = 'hub' | 'repo' | 'worktree' | 'agent_workspace' | 'filesystem';

export type ScopeRef =
  | { kind: 'hub' }
  | { kind: 'repo'; id: string }
  | { kind: 'worktree'; id: string; parentRepoId: string }
  | { kind: 'agent_workspace'; id: string }
  | { kind: 'filesystem'; path: string };

export type SurfaceRef = {
  kind: string;
  key: string;
};

const VALID_SCOPE_KINDS: Set<string> = new Set(['hub', 'repo', 'worktree', 'agent_workspace', 'filesystem']);

export function formatScopeUrn(scope: ScopeRef): string {
  switch (scope.kind) {
    case 'hub':
      return 'hub';
    case 'repo':
      return `repo:${scope.id}`;
    case 'worktree':
      return `worktree:${scope.parentRepoId}/${scope.id}`;
    case 'agent_workspace':
      return `agent_workspace:${scope.id}`;
    case 'filesystem':
      return `filesystem:${encodeURIComponent(scope.path)}`;
  }
}

export function parseScopeUrn(urn: string): ScopeRef {
  if (typeof urn !== 'string' || !urn.trim()) {
    throw new ScopeUrnParseError(repr(urn), 'URN must be a non-empty string');
  }

  if (urn === 'hub') {
    return { kind: 'hub' };
  }

  const colonPos = urn.indexOf(':');
  if (colonPos < 0) {
    throw new ScopeUrnParseError(urn, "URN must contain ':' separator or be 'hub'");
  }

  const kind = urn.slice(0, colonPos);
  const path = urn.slice(colonPos + 1);

  if (!VALID_SCOPE_KINDS.has(kind)) {
    throw new ScopeUrnKindError(urn, kind);
  }

  if (kind === 'hub') {
    throw new ScopeUrnParseError(urn, 'hub scope must not have a path component');
  }

  if (kind === 'repo') {
    if (!path) throw new ScopeUrnParseError(urn, 'repo scope requires an id after ":"');
    if (path.includes('/')) throw new ScopeUrnParseError(urn, "repo scope id must not contain '/'");
    return { kind: 'repo', id: path };
  }

  if (kind === 'worktree') {
    const slashPos = path.indexOf('/');
    if (slashPos <= 0 || slashPos === path.length - 1) {
      throw new ScopeUrnParseError(urn, "worktree scope requires '<repo_id>/<worktree_id>' path");
    }
    return {
      kind: 'worktree',
      id: path.slice(slashPos + 1),
      parentRepoId: path.slice(0, slashPos)
    };
  }

  if (kind === 'agent_workspace') {
    if (!path) throw new ScopeUrnParseError(urn, 'agent_workspace scope requires an id after ":"');
    if (path.includes('/')) throw new ScopeUrnParseError(urn, "agent_workspace scope id must not contain '/'");
    return { kind: 'agent_workspace', id: path };
  }

  if (kind === 'filesystem') {
    if (!path) throw new ScopeUrnParseError(urn, 'filesystem scope requires a path');
    validatePercentEscapes(path, urn);
    const decoded = decodePercentEncoded(path);
    if (!decoded) throw new ScopeUrnParseError(urn, 'filesystem scope requires a path');
    return { kind: 'filesystem', path: decoded };
  }

  throw new ScopeUrnKindError(urn, kind);
}

export function parseSurfaceUrn(urn: string): SurfaceRef {
  if (typeof urn !== 'string' || !urn.includes(':')) {
    throw new Error("SurfaceRef URN requires '<kind>:<key>'");
  }
  const colonPos = urn.indexOf(':');
  const kind = urn.slice(0, colonPos);
  const key = decodeURIComponent(urn.slice(colonPos + 1));
  if (!kind) throw new Error('SurfaceRef URN requires a kind');
  if (!key) throw new Error('SurfaceRef URN requires a key');
  return { kind, key };
}

export function formatSurfaceUrn(surface: SurfaceRef): string {
  return `${surface.kind}:${encodeURIComponent(surface.key)}`;
}

export function parentScope(scope: ScopeRef): ScopeRef | null {
  switch (scope.kind) {
    case 'hub':
      return null;
    case 'repo':
      return { kind: 'hub' };
    case 'worktree':
      return { kind: 'repo', id: scope.parentRepoId };
    case 'agent_workspace':
      return { kind: 'hub' };
    case 'filesystem':
      return null;
  }
}

export function scopeLabel(scope: ScopeRef): string {
  switch (scope.kind) {
    case 'hub':
      return 'Local hub';
    case 'repo':
      return `Repo: ${scope.id}`;
    case 'worktree':
      return `Worktree: ${scope.id}`;
    case 'agent_workspace':
      return `Agent workspace: ${scope.id}`;
    case 'filesystem':
      return scope.path.split(/[\\/]/).filter(Boolean).at(-1) ?? scope.path;
  }
}

export function scopeShortLabel(scope: ScopeRef): string {
  switch (scope.kind) {
    case 'hub':
      return 'Hub';
    case 'repo':
      return scope.id;
    case 'worktree':
      return scope.id;
    case 'agent_workspace':
      return scope.id;
    case 'filesystem':
      return scope.path.split(/[\\/]/).filter(Boolean).at(-1) ?? scope.path;
  }
}

export type ScopeBreadcrumb = { label: string; href: string | null };

export function scopeBreadcrumbs(scope: ScopeRef): ScopeBreadcrumb[] {
  const crumbs: ScopeBreadcrumb[] = [];
  const ancestors = scopeAncestors(scope);
  for (const ancestor of ancestors) {
    const route = scopeRoute(ancestor);
    const label = scopeShortLabel(ancestor);
    crumbs.push({ label, href: route });
  }
  const last = crumbs.at(-1);
  if (last) last.href = null;
  return crumbs;
}

export function scopeAncestors(scope: ScopeRef): ScopeRef[] {
  const chain: ScopeRef[] = [];
  let current: ScopeRef | null = scope;
  while (current) {
    chain.push(current);
    current = parentScope(current);
  }
  return chain;
}

export function scopeRoute(scope: ScopeRef): string | null {
  switch (scope.kind) {
    case 'hub':
      return '/chats';
    case 'repo':
      return `/repos/${encodeURIComponent(scope.id)}`;
    case 'worktree':
      return `/worktrees/${encodeURIComponent(scope.id)}`;
    case 'agent_workspace':
      return null;
    case 'filesystem':
      return null;
  }
}

export function scopeTicketRoute(scope: ScopeRef): string | null {
  switch (scope.kind) {
    case 'repo':
      return `/repos/${encodeURIComponent(scope.id)}/tickets`;
    case 'worktree':
      return `/worktrees/${encodeURIComponent(scope.id)}/tickets`;
    default:
      return null;
  }
}

export function scopeMemoryRoute(scope: ScopeRef): string | null {
  switch (scope.kind) {
    case 'repo':
      return `/contextspace/${encodeURIComponent(scope.id)}`;
    case 'worktree':
      return `/contextspace/${encodeURIComponent(scope.id)}`;
    default:
      return null;
  }
}

export function scopeFromApiPayload(raw: Record<string, unknown>): ScopeRef {
  const urn = stringField(raw, 'scope_urn', 'urn');
  if (urn) return parseScopeUrn(urn);

  const kind = stringField(raw, 'kind', 'resource_kind', 'scope_kind');
  if (kind) {
    const id = stringField(raw, 'id', 'resource_id', 'scope_id');
    const parentRepoId = stringField(raw, 'parent_repo_id', 'base_repo_id');
    const path = stringField(raw, 'path', 'workspace_root');
    return buildScopeRef(kind, id, parentRepoId, path);
  }

  const repoId = stringField(raw, 'repo_id');
  if (repoId) {
    const worktreeId = stringField(raw, 'worktree_id', 'worktree_repo_id');
    if (worktreeId) return { kind: 'worktree', id: worktreeId, parentRepoId: repoId };
    return { kind: 'repo', id: repoId };
  }

  const workspaceRoot = stringField(raw, 'workspace_root');
  if (workspaceRoot) return { kind: 'filesystem', path: workspaceRoot };

  return { kind: 'hub' };
}

export function scopeFromTicket(raw: Record<string, unknown>): ScopeRef {
  const workspaceKind = stringField(raw, 'workspace_kind', 'resource_kind');
  const workspaceId = stringField(raw, 'workspace_id', 'resource_id');
  if (workspaceKind === 'repo' && workspaceId) return { kind: 'repo', id: workspaceId };
  if (workspaceKind === 'worktree' && workspaceId) {
    const frontmatter = asRecord(raw.frontmatter);
    const repoId =
      stringField(raw, 'repo_id', 'base_repo_id') ??
      stringField(frontmatter, 'repo_id', 'base_repo_id');
    if (repoId) return { kind: 'worktree', id: workspaceId, parentRepoId: repoId };
    return { kind: 'hub' };
  }

  const worktreeId = stringField(raw, 'worktree_id', 'worktree_repo_id');
  if (worktreeId) {
    const frontmatter = asRecord(raw.frontmatter);
    const repoId =
      stringField(raw, 'repo_id', 'base_repo_id') ??
      stringField(frontmatter, 'repo_id', 'base_repo_id');
    if (repoId) return { kind: 'worktree', id: worktreeId, parentRepoId: repoId };
    return { kind: 'hub' };
  }

  const repoId = stringField(raw, 'repo_id', 'base_repo_id');
  if (repoId) return { kind: 'repo', id: repoId };

  return { kind: 'hub' };
}

export function scopeMatchesResource(scope: ScopeRef, kind: string, id: string): boolean {
  if (scope.kind === 'hub' || scope.kind === 'filesystem') return false;
  if (scope.kind === 'repo' && kind === 'repo') return scope.id === id;
  if (scope.kind === 'worktree' && kind === 'worktree') return scope.id === id;
  if (scope.kind === 'worktree' && kind === 'repo') return scope.parentRepoId === id;
  if (scope.kind === 'agent_workspace' && kind === 'agent_workspace') return scope.id === id;
  return false;
}

export function scopeEquals(left: ScopeRef, right: ScopeRef): boolean {
  return formatScopeUrn(left) === formatScopeUrn(right);
}

export class ScopeUrnParseError extends Error {
  readonly urn: string;
  readonly reason: string;

  constructor(urn: string, reason = '') {
    const msg = `Invalid scope URN: ${urn}${reason ? ` (${reason})` : ''}`;
    super(msg);
    this.name = 'ScopeUrnParseError';
    this.urn = urn;
    this.reason = reason;
  }
}

export class ScopeUrnKindError extends Error {
  readonly urn: string;
  readonly kind: string;

  constructor(urn: string, kind: string) {
    super(`Unknown scope kind '${kind}' in URN: ${urn}`);
    this.name = 'ScopeUrnKindError';
    this.urn = urn;
    this.kind = kind;
  }
}

function buildScopeRef(kind: string, id: string | null, parentRepoId: string | null, path: string | null): ScopeRef {
  if (kind === 'hub') return { kind: 'hub' };
  if (kind === 'repo' && id) return { kind: 'repo', id };
  if (kind === 'worktree' && id && parentRepoId) return { kind: 'worktree', id, parentRepoId };
  if (kind === 'agent_workspace' && id) return { kind: 'agent_workspace', id };
  if (kind === 'filesystem' && path) return { kind: 'filesystem', path };
  if (kind === 'repo' && !id && path) return { kind: 'filesystem', path };
  return { kind: 'hub' };
}

function stringField(raw: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = raw[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

function validatePercentEscapes(value: string, urn: string): void {
  let percentPos = value.indexOf('%');
  while (percentPos >= 0) {
    const escape = value.slice(percentPos + 1, percentPos + 3);
    if (escape.length !== 2 || !/^[0-9a-fA-F]{2}$/.test(escape)) {
      throw new ScopeUrnParseError(urn, 'filesystem path has invalid escape');
    }
    percentPos = value.indexOf('%', percentPos + 1);
  }
}

function decodePercentEncoded(value: string): string {
  const decoder = new TextDecoder('utf-8');
  let decoded = '';
  for (let index = 0; index < value.length; ) {
    if (value[index] !== '%') {
      decoded += value[index];
      index += 1;
      continue;
    }

    const bytes: number[] = [];
    while (value[index] === '%') {
      bytes.push(Number.parseInt(value.slice(index + 1, index + 3), 16));
      index += 3;
    }
    decoded += decoder.decode(new Uint8Array(bytes));
  }
  return decoded;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function repr(value: unknown): string {
  if (typeof value === 'string') return value;
  return String(value);
}
