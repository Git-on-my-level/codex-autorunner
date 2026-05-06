import type { ContextspaceDocument } from './domain';
import { renderMarkdownToHtml } from './contextspace';

export type PmaMemoryDocumentTab = {
  id: string;
  label: string;
  filename: string;
  content: string;
  html: string;
  isMissing: boolean;
  updatedAt: string | null;
};

export type PmaMemoryViewModel = {
  title: string;
  eyebrow: string;
  description: string;
  docs: PmaMemoryDocumentTab[];
  presentCount: number;
};

const PMA_DOC_LABELS: Record<string, string> = {
  'AGENTS.md': 'Durable guidance',
  'active_context.md': 'Active context'
};

const PMA_DOC_ORDER = ['AGENTS.md', 'active_context.md'];
const PMA_DOC_SET = new Set(PMA_DOC_ORDER);

export function buildPmaMemoryViewModel(docs: ContextspaceDocument[]): PmaMemoryViewModel {
  const orderedDocs = docs.filter((doc) => PMA_DOC_SET.has(doc.name)).sort((left, right) => {
    const leftIndex = PMA_DOC_ORDER.indexOf(left.name);
    const rightIndex = PMA_DOC_ORDER.indexOf(right.name);
    const normalizedLeft = leftIndex === -1 ? Number.MAX_SAFE_INTEGER : leftIndex;
    const normalizedRight = rightIndex === -1 ? Number.MAX_SAFE_INTEGER : rightIndex;
    if (normalizedLeft !== normalizedRight) return normalizedLeft - normalizedRight;
    return left.name.localeCompare(right.name);
  });
  const tabs = orderedDocs.map((doc) => {
    const content = doc.content ?? '';
    return {
      id: doc.id || doc.name,
      label: PMA_DOC_LABELS[doc.name] ?? readableDocLabel(doc.name),
      filename: doc.name,
      content,
      html: renderMarkdownToHtml(content),
      isMissing: !content.trim(),
      updatedAt: doc.updatedAt
    };
  });
  return {
    title: 'PMA memory',
    eyebrow: 'PMA workspace docs',
    description: 'PMA memory is read from .codex-autorunner/pma/docs and is used by PMA as durable guidance and working context.',
    docs: tabs,
    presentCount: tabs.filter((doc) => !doc.isMissing).length
  };
}

function readableDocLabel(name: string): string {
  return name
    .replace(/\.md$/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase());
}
