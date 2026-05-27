import { primaryNav } from './navigation';

export type Breadcrumb = { label: string; href: string | null };

/**
 * Hub breadcrumbs are derived only from the pathname so unlisted routes
 * (worktrees, contextspace, etc.) never inherit a misleading “active nav” label.
 *
 * Adding new pages:
 * 1. **Top-level screen** — add a `primaryNav` entry in `navigation.ts`. If the
 *    route is exactly that `href`, you get a single crumb automatically.
 * 2. **Nested route** — append a `{ description, pattern, toCrumbs }` entry to `STRUCTURED_ROUTES`
 *    in `breadcrumbs.ts`. Keep **most specific paths first** (e.g. `.../tickets/:id` before `.../tickets`).
 * 3. Run `pnpm test` (see `breadcrumbs.test.ts`).
 */
type StructuredRoute = {
  /** Single-line description for maintainers (optional but recommended). */
  description: string;
  pattern: RegExp;
  toCrumbs: (match: RegExpExecArray) => Breadcrumb[];
};

function normalizePath(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith('/')) return pathname.slice(0, -1);
  return pathname;
}

/** Longest / most specific patterns first. */
const STRUCTURED_ROUTES: StructuredRoute[] = [
  {
    description: 'Repo-owned worktree new ticket',
    pattern: /^\/repos\/([^/]+)\/worktrees\/([^/]+)\/tickets\/new$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const worktreeId = decodeURIComponent(m[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: worktreeId, href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}/tickets` },
        { label: 'New', href: null }
      ];
    }
  },
  {
    description: 'Repo-scoped new ticket',
    pattern: /^\/repos\/([^/]+)\/tickets\/new$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Tickets', href: `/repos/${encodeURIComponent(repoId)}/tickets` },
        { label: 'New', href: null }
      ];
    }
  },
  {
    description: 'Repo-owned worktree ticket detail',
    pattern: /^\/repos\/([^/]+)\/worktrees\/([^/]+)\/tickets\/([^/]+)$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const worktreeId = decodeURIComponent(m[2]);
      const ticketId = decodeURIComponent(m[3]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: worktreeId, href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}/tickets` },
        { label: ticketId.match(/^\d+$/) ? `#${ticketId}` : ticketId, href: null }
      ];
    }
  },
  {
    description: 'Repo-owned worktree ticket queue',
    pattern: /^\/repos\/([^/]+)\/worktrees\/([^/]+)\/tickets$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const worktreeId = decodeURIComponent(m[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: worktreeId, href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: null }
      ];
    }
  },
  {
    description: 'Repo-owned worktree contextspace',
    pattern: /^\/repos\/([^/]+)\/worktrees\/([^/]+)\/contextspace$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const worktreeId = decodeURIComponent(m[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: worktreeId, href: `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Contextspace', href: null }
      ];
    }
  },
  {
    description: 'Repo-owned worktree overview',
    pattern: /^\/repos\/([^/]+)\/worktrees\/([^/]+)$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const worktreeId = decodeURIComponent(m[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: worktreeId, href: null }
      ];
    }
  },
  {
    description: 'Repo contextspace',
    pattern: /^\/repos\/([^/]+)\/contextspace$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Contextspace', href: null }
      ];
    }
  },
  {
    description: 'Repo-scoped ticket detail',
    pattern: /^\/repos\/([^/]+)\/tickets\/([^/]+)$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      const ticketId = decodeURIComponent(m[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Tickets', href: `/repos/${encodeURIComponent(repoId)}/tickets` },
        { label: ticketId.match(/^\d+$/) ? `#${ticketId}` : ticketId, href: null }
      ];
    }
  },
  {
    description: 'Repo ticket queue',
    pattern: /^\/repos\/([^/]+)\/tickets$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Tickets', href: null }
      ];
    }
  },
  {
    description: 'Repo workspace overview',
    pattern: /^\/repos\/([^/]+)$/,
    toCrumbs: (m) => {
      const repoId = decodeURIComponent(m[1]);
      return [{ label: 'Repos', href: '/repos' }, { label: repoId, href: null }];
    }
  },
  {
    description: 'Repo index',
    pattern: /^\/repos$/,
    toCrumbs: () => [{ label: 'Repos', href: null }]
  },
  {
    description: 'Worktree-scoped ticket detail',
    pattern: /^\/worktrees\/([^/]+)\/tickets\/([^/]+)$/,
    toCrumbs: (m) => {
      const worktreeId = decodeURIComponent(m[1]);
      const ticketId = decodeURIComponent(m[2]);
      return [
        { label: 'Worktrees', href: '/worktrees' },
        { label: worktreeId, href: `/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: `/worktrees/${encodeURIComponent(worktreeId)}/tickets` },
        { label: ticketId.match(/^\d+$/) ? `#${ticketId}` : ticketId, href: null }
      ];
    }
  },
  {
    description: 'Worktree ticket queue',
    pattern: /^\/worktrees\/([^/]+)\/tickets$/,
    toCrumbs: (m) => {
      const worktreeId = decodeURIComponent(m[1]);
      return [
        { label: 'Worktrees', href: '/worktrees' },
        { label: worktreeId, href: `/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: null }
      ];
    }
  },
  {
    description: 'Worktree overview',
    pattern: /^\/worktrees\/([^/]+)$/,
    toCrumbs: (m) => {
      const worktreeId = decodeURIComponent(m[1]);
      return [{ label: 'Worktrees', href: '/worktrees' }, { label: worktreeId, href: null }];
    }
  },
  {
    description: 'Worktree index',
    pattern: /^\/worktrees$/,
    toCrumbs: () => [{ label: 'Worktrees', href: null }]
  },
  {
    description: 'Contextspace docs for a workspace',
    pattern: /^\/contextspace\/([^/]+)$/,
    toCrumbs: (m) => {
      const workspaceId = decodeURIComponent(m[1]);
      return [{ label: 'Contextspace', href: null }, { label: workspaceId, href: null }];
    }
  },
  {
    description: 'Hub compatibility route',
    pattern: /^\/hub$/,
    toCrumbs: () => [{ label: 'Hub', href: null }]
  },
  {
    description: 'Automation detail',
    pattern: /^\/automations\/([^/]+)$/,
    toCrumbs: (m) => {
      const ruleId = decodeURIComponent(m[1]);
      // Drop the `user:automation:` / `builtin:pma:...` namespace and any trailing
      // hex hash so the crumb reads as a slug, not a raw rule id.
      const tail = (ruleId.split(':').pop() ?? ruleId).replace(/-[0-9a-f]{6,}$/i, '');
      const short = tail.length > 22 ? `${tail.slice(0, 22)}…` : tail || ruleId;
      return [
        { label: 'Automations', href: '/automations' },
        { label: short, href: null }
      ];
    }
  },
  {
    description: 'Settings section detail',
    pattern: /^\/settings\/([^/]+)$/,
    toCrumbs: (m) => {
      const sectionId = decodeURIComponent(m[1]);
      const labels: Record<string, string> = {
        memory: 'PMA memory',
        general: 'General',
        integrations: 'Integrations',
        agents: 'Agents & Runner'
      };
      const label = labels[sectionId] ?? sectionId;
      return [
        { label: 'Settings', href: '/settings' },
        { label, href: null }
      ];
    }
  },
  {
    description: 'Chat detail',
    pattern: /^\/chats\/([^/]+)$/,
    toCrumbs: (m) => {
      const chatId = decodeURIComponent(m[1]);
      const short = chatId.length > 6 ? `#${chatId.slice(0, 6)}` : `#${chatId}`;
      return [
        { label: 'Chats', href: '/chats' },
        { label: short, href: null }
      ];
    }
  }
];

function matchStructured(normalizedPath: string): Breadcrumb[] | null {
  for (const route of STRUCTURED_ROUTES) {
    const match = route.pattern.exec(normalizedPath);
    if (match) return route.toCrumbs(match);
  }
  return null;
}

function matchPrimaryNavExact(normalizedPath: string): Breadcrumb[] | null {
  const item = primaryNav.find((nav) => nav.href === normalizedPath);
  if (!item) return null;
  return [{ label: item.label, href: null }];
}

function fallbackCrumbs(normalizedPath: string): Breadcrumb[] {
  const segments = normalizedPath.split('/').filter(Boolean);
  const tail = segments.length ? decodeURIComponent(segments[segments.length - 1] ?? '') : normalizedPath;
  return [
    { label: 'Chats', href: '/chats' },
    { label: tail || 'Page', href: null }
  ];
}

/**
 * Build top-bar breadcrumbs for a hub pathname (no base path prefix).
 */
export function breadcrumbsForPath(pathname: string): Breadcrumb[] {
  const path = normalizePath(pathname);

  if (path === '/') {
    return [{ label: 'Chats', href: null }];
  }

  const structured = matchStructured(path);
  if (structured) return structured;

  const navExact = matchPrimaryNavExact(path);
  if (navExact) return navExact;

  return fallbackCrumbs(path);
}
