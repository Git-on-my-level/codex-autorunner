import type { ContextspaceDocument, RepoSummary, WorktreeSummary } from './domain';
import { renderMarkdownToHtml } from './markdown';
export { renderMarkdownToHtml } from './markdown';

export type ContextspaceDocKind = 'active_context' | 'spec' | 'decisions';

export type ContextspaceDocumentTab = {
  id: ContextspaceDocKind;
  label: string;
  filename: string;
  content: string;
  html: string;
  isMissing: boolean;
  updatedAt: string | null;
};

export type ContextspaceViewModel = {
  workspaceId: string;
  title: string;
  eyebrow: string;
  workspaceKind: 'repo' | 'worktree' | 'unknown';
  isUnknown: boolean;
  description: string;
  docs: ContextspaceDocumentTab[];
  presentCount: number;
};

export const CONTEXTSPACE_DOC_ORDER = ['active_context.md', 'spec.md', 'decisions.md'] as const;

const DOC_ORDER: Array<{ id: ContextspaceDocKind; label: string; filename: string }> = [
  { id: 'active_context', label: 'Active context', filename: 'active_context.md' },
  { id: 'spec', label: 'Spec', filename: 'spec.md' },
  { id: 'decisions', label: 'Decisions', filename: 'decisions.md' }
];

export function buildContextspaceViewModel(
  workspaceId: string,
  docs: ContextspaceDocument[],
  repos: RepoSummary[] = [],
  worktrees: WorktreeSummary[] = []
): ContextspaceViewModel {
  const repo = repos.find((candidate) => candidate.id === workspaceId) ?? null;
  const worktree = worktrees.find((candidate) => candidate.id === workspaceId) ?? null;
  const workspaceKind = worktree ? 'worktree' : repo ? 'repo' : 'unknown';
  const title = worktree?.name ?? repo?.name ?? workspaceId;
  const docMap = new Map(docs.map((doc) => [normalizeDocKind(doc.kind || doc.id || doc.name), doc]));
  const tabs = DOC_ORDER.map((entry) => {
    const doc = docMap.get(entry.id) ?? null;
    const content = doc?.content ?? '';
    return {
      ...entry,
      content,
      html: renderMarkdownToHtml(content),
      isMissing: !content.trim(),
      updatedAt: doc?.updatedAt ?? null
    };
  });
  return {
    workspaceId,
    title: `Contextspace: ${title}`,
    eyebrow:
      workspaceKind === 'repo'
        ? 'Repo-scoped contextspace'
        : workspaceKind === 'worktree'
          ? 'Worktree-scoped contextspace'
          : 'Unknown workspace contextspace',
    workspaceKind,
    isUnknown: workspaceKind === 'unknown',
    description:
      workspaceKind === 'repo'
        ? 'Repo contextspace is read from this repo workspace.'
        : workspaceKind === 'worktree'
          ? 'Worktree contextspace is read from this worktree workspace.'
          : 'This workspace id was not matched to a known repo or worktree, so scoped contextspace was not loaded.',
    docs: tabs,
    presentCount: tabs.filter((doc) => !doc.isMissing).length
  };
}

export function normalizeDocKind(value: string): ContextspaceDocKind | string {
  const normalized = value.trim().toLowerCase().replace(/\.md$/, '');
  if (normalized === 'active' || normalized === 'active-context') return 'active_context';
  if (normalized === 'active_context' || normalized === 'spec' || normalized === 'decisions') return normalized;
  return normalized;
}
