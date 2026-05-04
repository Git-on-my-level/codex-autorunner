import type { ContextspaceDocument, RepoSummary, WorktreeSummary } from './domain';

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
  workspaceKind: 'repo' | 'worktree' | 'workspace';
  openWorkspaceHref: string;
  openWorkspaceLabel: string;
  askPmaHref: string;
  docs: ContextspaceDocumentTab[];
  presentCount: number;
};

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
  const workspaceKind = worktree ? 'worktree' : repo ? 'repo' : 'workspace';
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
  const askPrompt = `Please review and update the contextspace docs for workspace ${workspaceId}: active_context.md, spec.md, and decisions.md.`;

  return {
    workspaceId,
    title: `${title} contextspace`,
    eyebrow: workspaceKind === 'repo' ? 'Repo memory' : workspaceKind === 'worktree' ? 'Worktree memory' : 'Workspace memory',
    workspaceKind,
    openWorkspaceHref:
      workspaceKind === 'worktree'
        ? `/worktrees/${encodeURIComponent(workspaceId)}`
        : workspaceKind === 'repo'
          ? `/repos/${encodeURIComponent(workspaceId)}`
          : '/repos',
    openWorkspaceLabel:
      workspaceKind === 'worktree' ? 'Open worktree' : workspaceKind === 'repo' ? 'Open repo' : 'Open workspaces',
    askPmaHref: `/pma?draft=${encodeURIComponent(askPrompt)}`,
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

export function renderMarkdownToHtml(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html: string[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let codeLines: string[] | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInline(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<ul>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join('')}</ul>`);
    listItems = [];
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (codeLines) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = null;
      } else {
        flushParagraph();
        flushList();
        codeLines = [];
      }
      continue;
    }
    if (codeLines) {
      codeLines.push(line);
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = /^[-*]\s+(.+)$/.exec(trimmed);
    if (bullet) {
      flushParagraph();
      listItems.push(bullet[1]);
      continue;
    }

    flushList();
    paragraph.push(trimmed);
  }

  if (codeLines) html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  flushParagraph();
  flushList();
  return html.join('');
}

function renderInline(value: string): string {
  return escapeHtml(value).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
