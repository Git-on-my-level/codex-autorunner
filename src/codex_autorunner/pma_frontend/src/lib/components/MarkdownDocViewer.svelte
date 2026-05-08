<script lang="ts" module>
  export type MarkdownDocViewerDoc = {
    id: string;
    filename: string;
    content: string;
    html: string;
    isMissing: boolean;
    updatedAt?: string | null;
  };
</script>

<script lang="ts">
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';

  let {
    title,
    description,
    docs,
    presentCount,
    ariaLabel,
    activeDocId = null,
    emptyMessage,
    editableDoc = () => true,
    docHref = null,
    closeLabel = 'Close document viewer',
    onSelectDoc,
    onSaveDoc,
    onClose
  }: {
    title: string;
    description: string;
    docs: MarkdownDocViewerDoc[];
    presentCount: number;
    ariaLabel: string;
    activeDocId?: string | null;
    emptyMessage: string;
    editableDoc?: (doc: MarkdownDocViewerDoc) => boolean;
    docHref?: ((doc: MarkdownDocViewerDoc) => string) | null;
    closeLabel?: string;
    onSelectDoc?: (docId: string) => void;
    onSaveDoc?: (docId: string, content: string) => Promise<boolean> | boolean;
    onClose?: () => void;
  } = $props();

  let internalDocId = $state('');
  let copyState = $state('Copy');

  const selectedDocId = $derived(activeDocId ?? internalDocId);
  const activeDoc = $derived(docs.find((doc) => doc.id === selectedDocId) ?? docs[0] ?? null);

  $effect(() => {
    if (docs.length > 0 && !internalDocId) {
      internalDocId = docs[0].id;
    }
  });

  function selectDoc(docId: string): void {
    internalDocId = docId;
    copyState = 'Copy';
    onSelectDoc?.(docId);
  }

  function handleDocClick(event: MouseEvent, doc: MarkdownDocViewerDoc): void {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    selectDoc(doc.id);
  }

  async function copyActiveDoc(): Promise<void> {
    if (!activeDoc) return;
    try {
      await navigator.clipboard.writeText(activeDoc.content);
      copyState = 'Copied';
      window.setTimeout(() => (copyState = 'Copy'), 1600);
    } catch {
      copyState = 'Copy failed';
      window.setTimeout(() => (copyState = 'Copy'), 1800);
    }
  }
</script>

{#if activeDoc}
  <section class="markdown-doc-viewer">
    <header class="doc-viewer-hero">
      <div class="doc-viewer-hero-copy">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <dl class="doc-viewer-stats" aria-label="Document summary">
        <div class={presentCount === docs.length ? 'is-complete' : 'is-partial'}>
          <dt>Docs</dt>
          <dd><strong>{presentCount}</strong><span>of {docs.length}</span></dd>
        </div>
      </dl>
    </header>

    <nav class="doc-viewer-tabs" aria-label={ariaLabel}>
      {#each docs as doc}
        <a
          class:active={doc.id === activeDoc.id}
          class:missing={doc.isMissing}
          href={docHref?.(doc) ?? `#${encodeURIComponent(doc.id)}`}
          onclick={(event) => handleDocClick(event, doc)}
        >
          <span class="doc-viewer-tab-dot" aria-hidden="true"></span>
          <span class="doc-viewer-tab-label">{doc.filename}</span>
        </a>
      {/each}
    </nav>

    <article class="page-panel doc-viewer-reader">
      <header class="doc-viewer-reader-head">
        <div class="doc-viewer-reader-title">
          <span class="doc-viewer-reader-filename">{activeDoc.filename}</span>
          <span class={`doc-viewer-reader-status ${activeDoc.isMissing ? 'is-missing' : 'is-present'}`}>
            <span class="doc-viewer-reader-status-dot" aria-hidden="true"></span>
            {activeDoc.isMissing ? 'Empty' : 'Present'}
          </span>
        </div>
        <div class="doc-viewer-reader-actions">
          <button class="doc-viewer-copy-button" type="button" onclick={copyActiveDoc} disabled={activeDoc.isMissing}>
            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
              <rect x="4" y="4" width="9" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4" />
              <path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            <span>{copyState}</span>
          </button>
          {#if onClose}
            <button class="doc-viewer-close-button" type="button" onclick={onClose} aria-label={closeLabel}>
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
              </svg>
            </button>
          {/if}
        </div>
      </header>

      <EditableMarkdown
        docId={activeDoc.id}
        content={activeDoc.content}
        html={activeDoc.html}
        isMissing={activeDoc.isMissing}
        emptyTitle={`${activeDoc.filename} has no content`}
        {emptyMessage}
        editable={editableDoc(activeDoc)}
        onSave={onSaveDoc}
      />
    </article>
  </section>
{/if}

<style>
  .markdown-doc-viewer {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    width: 100%;
    gap: var(--space-2);
    overflow: hidden;
  }

  .doc-viewer-hero {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-4);
    padding: 0 2px;
    flex: 0 0 auto;
  }

  .doc-viewer-hero-copy {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .doc-viewer-hero h1 {
    margin: 0;
    font-size: var(--font-size-3);
    font-weight: 650;
    letter-spacing: -0.018em;
    line-height: 1.2;
  }

  .doc-viewer-hero p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.4;
    max-width: 72ch;
  }

  .doc-viewer-stats {
    display: flex;
    align-items: stretch;
    gap: 0;
    margin: 0;
    padding: 4px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
  }

  .doc-viewer-stats > div {
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 1px var(--space-2);
  }

  .doc-viewer-stats dt {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 11px;
    font-weight: 500;
  }

  .doc-viewer-stats dd {
    margin: 0;
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
    color: var(--color-ink);
    font-size: var(--font-size-2);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
    line-height: 1;
  }

  .doc-viewer-stats dd span {
    color: var(--color-ink-faint);
    font-weight: 500;
    font-size: var(--font-size-0);
  }

  .doc-viewer-stats > div.is-complete dd strong { color: var(--color-success); }
  .doc-viewer-stats > div.is-partial dd strong { color: var(--color-warning); }

  .doc-viewer-tabs {
    display: flex;
    align-items: center;
    align-content: flex-start;
    flex-wrap: wrap;
    gap: 2px;
    padding: 3px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
    flex: 0 0 auto;
  }

  .doc-viewer-tabs a {
    display: inline-flex;
    align-items: center;
    flex: 0 0 auto;
    gap: 8px;
    min-height: 26px;
    padding: 4px 9px;
    border: 1px solid transparent;
    border-radius: 7px;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    text-decoration: none;
    font-size: var(--font-size-1);
    font-weight: 500;
    line-height: 1.2;
    transition: background-color var(--transition-fast), color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .doc-viewer-tabs a:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .doc-viewer-tabs a.active {
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px var(--color-border-subtle);
  }

  .doc-viewer-tab-dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--color-success);
    flex: 0 0 auto;
  }

  .doc-viewer-tabs a.missing .doc-viewer-tab-dot {
    background: var(--color-border-strong);
  }

  .doc-viewer-tabs a.active .doc-viewer-tab-dot {
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 14%, transparent);
  }

  .doc-viewer-tab-label {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    font-weight: 500;
    color: var(--color-ink-muted);
  }

  .doc-viewer-tabs a.active .doc-viewer-tab-label {
    color: var(--color-ink);
  }

  .doc-viewer-reader {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 0;
    overflow: hidden;
  }

  .doc-viewer-reader-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex: 0 0 auto;
    padding: var(--space-2) var(--space-4);
    border-bottom: 1px solid var(--color-border-subtle);
    background: linear-gradient(180deg, var(--color-surface), var(--color-surface-sunken));
  }

  .doc-viewer-reader-title {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
  }

  .doc-viewer-reader-filename {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    font-weight: 600;
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .doc-viewer-reader-status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 20px;
    padding: 0 7px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 500;
    line-height: 1;
  }

  .doc-viewer-reader-status.is-present {
    background: var(--color-success-soft);
    color: var(--color-success);
  }

  .doc-viewer-reader-status.is-missing {
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
  }

  .doc-viewer-reader-status-dot {
    width: 5px;
    height: 5px;
    border-radius: 999px;
    background: currentColor;
  }

  .doc-viewer-reader-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .doc-viewer-copy-button,
  .doc-viewer-close-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    height: 26px;
    padding: 0 10px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: var(--font-size-0);
    font-weight: 500;
    transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
  }

  .doc-viewer-close-button {
    width: 26px;
    padding: 0;
  }

  .doc-viewer-copy-button:hover:not(:disabled),
  .doc-viewer-close-button:hover:not(:disabled) {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .doc-viewer-copy-button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .doc-viewer-reader :global(.markdown-body),
  .doc-viewer-reader :global(.markdown-editor-shell) {
    margin: 0;
    width: 100%;
    padding: var(--space-5) var(--space-6) var(--space-6);
    max-width: none;
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
  }

  .doc-viewer-reader :global(.markdown-body) {
    margin-top: 0;
  }

  @media (max-width: 760px) {
    .doc-viewer-hero {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
    }

    .doc-viewer-stats {
      align-self: flex-start;
    }

    .doc-viewer-reader-head {
      padding: var(--space-3) var(--space-4);
    }

    .doc-viewer-reader :global(.markdown-body),
    .doc-viewer-reader :global(.markdown-editor-shell) {
      padding: var(--space-4);
    }
  }
</style>
