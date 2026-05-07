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
  askPmaHref: string | null;
  docs: MemoryDocumentTab[];
  presentCount: number;
};

const PMA_DOC_ORDER = ['AGENTS.md', 'active_context.md', 'context_log.md'];
const CONTEXTSPACE_DOC_ORDER = ['active_context.md', 'spec.md', 'decisions.md'];

function docOrderForScope(scope: ScopeRef): string[] {
  if (scope.kind === 'repo' || scope.kind === 'worktree') {
    return CONTEXTSPACE_DOC_ORDER;
  }
  return PMA_DOC_ORDER;
}

export function buildMemoryViewModel(
  scope: ScopeRef,
  docs: ContextspaceDocument[],
  _repos: RepoSummary[] = [],
  _worktrees: WorktreeSummary[] = []
): MemoryViewModel {
  const docOrder = docOrderForScope(scope);
  const docMap = new Map(docs.map((doc) => [doc.name, doc]));

  const tabs = docOrder.map((filename) => {
    const doc = docMap.get(filename);
    const content = doc?.content ?? '';
    return {
      id: doc?.id || filename,
      filename,
      content,
      html: renderMarkdownToHtml(content),
      isMissing: !content.trim(),
      updatedAt: doc?.updatedAt ?? null
    };
  });

  const label = scopeLabel(scope);
  const shortLabel = scopeShortLabel(scope);
  const workspaceHref = scopeRoute(scope);
  const memoryHref = scopeMemoryRoute(scope);

  let description: string;
  if (scope.kind === 'repo') {
    description = `Repo memory is read from this repo's .codex-autorunner/contextspace directory and provides durable shared context.`;
  } else if (scope.kind === 'worktree') {
    description = `Worktree memory is read from this worktree's .codex-autorunner/contextspace directory and provides durable shared context.`;
  } else {
    description = `PMA memory is read from .codex-autorunner/pma/docs and provides durable PMA guidance and working context.`;
  }

  const scopeKind = scope.kind === 'hub' ? 'PMA' : scope.kind;
  const askPrompt = `Please review and update the ${scopeKind} memory docs for ${shortLabel}.`;
  const askPmaHref = `/chats?draft=${encodeURIComponent(askPrompt)}`;

  return {
    scope,
    title: `Memory: ${shortLabel}`,
    eyebrow: scope.kind === 'repo' ? 'Repo memory' : scope.kind === 'worktree' ? 'Worktree memory' : `${label} memory`,
    description,
    workspaceHref,
    memoryHref,
    askPmaHref,
    docs: tabs,
    presentCount: tabs.filter((doc) => !doc.isMissing).length
  };
}

export { PMA_DOC_ORDER, CONTEXTSPACE_DOC_ORDER };
