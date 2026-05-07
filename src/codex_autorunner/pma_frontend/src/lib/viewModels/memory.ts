import type { ContextspaceDocument, RepoSummary, WorktreeSummary } from './domain';
import { renderMarkdownToHtml } from './markdown';
import type { ScopeRef } from './scope';
import { scopeLabel, scopeMemoryRoute, scopeRoute, scopeShortLabel } from './scope';

export type MemoryDocumentTab = {
  id: string;
  filename: string;
  content: string;
  html: string;
  isMissing: boolean;
  updatedAt: string | null;
};

export type MemoryViewModel = {
  scope: ScopeRef;
  title: string;
  eyebrow: string;
  description: string;
  workspaceHref: string | null;
  memoryHref: string | null;
  docs: MemoryDocumentTab[];
  presentCount: number;
};

const DOC_ORDER = ['AGENTS.md', 'active_context.md', 'context_log.md'];
const DOC_SET = new Set(DOC_ORDER);

export function buildMemoryViewModel(
  scope: ScopeRef,
  docs: ContextspaceDocument[],
  _repos: RepoSummary[] = [],
  _worktrees: WorktreeSummary[] = []
): MemoryViewModel {
  const orderedDocs = docs
    .filter((doc) => DOC_SET.has(doc.name))
    .sort((left, right) => {
      const leftIndex = DOC_ORDER.indexOf(left.name);
      const rightIndex = DOC_ORDER.indexOf(right.name);
      const normalizedLeft = leftIndex === -1 ? Number.MAX_SAFE_INTEGER : leftIndex;
      const normalizedRight = rightIndex === -1 ? Number.MAX_SAFE_INTEGER : rightIndex;
      if (normalizedLeft !== normalizedRight) return normalizedLeft - normalizedRight;
      return left.name.localeCompare(right.name);
    });

  const tabs = orderedDocs.map((doc) => {
    const content = doc.content ?? '';
    return {
      id: doc.id || doc.name,
      filename: doc.name,
      content,
      html: renderMarkdownToHtml(content),
      isMissing: !content.trim(),
      updatedAt: doc.updatedAt
    };
  });

  const label = scopeLabel(scope);
  const shortLabel = scopeShortLabel(scope);
  const workspaceHref = scopeRoute(scope);
  const memoryHref = scopeMemoryRoute(scope);

  let description: string;
  if (scope.kind === 'repo') {
    description = `Repo memory is read from this repo's .codex-autorunner/pma/docs directory and provides durable PMA guidance and working context.`;
  } else if (scope.kind === 'worktree') {
    description = `Worktree memory is read from this worktree's .codex-autorunner/pma/docs directory and provides durable PMA guidance and working context.`;
  } else {
    description = `PMA memory docs for ${label}.`;
  }

  return {
    scope,
    title: `Memory: ${shortLabel}`,
    eyebrow: scope.kind === 'repo' ? 'Repo memory' : scope.kind === 'worktree' ? 'Worktree memory' : `${label} memory`,
    description,
    workspaceHref,
    memoryHref,
    docs: tabs,
    presentCount: tabs.filter((doc) => !doc.isMissing).length
  };
}

export { DOC_ORDER, DOC_SET };
