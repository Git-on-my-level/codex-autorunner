import type { ContextspaceDocument, RepoSummary, WorktreeSummary } from './domain';
import { renderMarkdownToHtml } from './markdown';
import type { ScopeRef } from './scope';
import { scopeLabel, scopeRoute, scopeShortLabel } from './scope';

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
  docs: MemoryDocumentTab[];
  presentCount: number;
};

const PMA_DOC_ORDER = ['AGENTS.md', 'active_context.md', 'context_log.md'];

export function buildMemoryViewModel(
  scope: ScopeRef,
  docs: ContextspaceDocument[],
  _repos: RepoSummary[] = [],
  _worktrees: WorktreeSummary[] = []
): MemoryViewModel {
  const docMap = new Map(docs.map((doc) => [doc.name, doc]));

  const tabs = PMA_DOC_ORDER.map((filename) => {
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

  const title = `Memory: ${shortLabel}`;
  const eyebrow = `${label} memory`;
  const description = `PMA memory is read from .codex-autorunner/pma/docs and provides durable PMA guidance and working context.`;

  return {
    scope,
    title,
    eyebrow,
    description,
    workspaceHref,
    docs: tabs,
    presentCount: tabs.filter((doc) => !doc.isMissing).length
  };
}

export { PMA_DOC_ORDER };
