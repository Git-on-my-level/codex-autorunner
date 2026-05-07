<script lang="ts">
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { MemoryViewModel } from '$lib/viewModels/memory';

  let {
    state: viewState,
    vm = null,
    errorMessage = null,
    onSaveDoc,
    onClose
  }: {
    state: 'loading' | 'error' | 'ready';
    vm?: MemoryViewModel | null;
    errorMessage?: string | null;
    onSaveDoc?: (docId: string, content: string) => Promise<boolean> | boolean;
    onClose?: () => void;
  } = $props();

  let activeDocId = $state<string>('');
  let copyState = $state('Copy');

  const activeDoc = $derived(vm?.docs.find((doc) => doc.id === activeDocId) ?? vm?.docs[0] ?? null);

  $effect(() => {
    if (vm && vm.docs.length > 0 && !activeDocId) {
      activeDocId = vm.docs[0].id;
    }
  });

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

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading memory...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load memory. {errorMessage}</div>
  </section>
{:else if vm && activeDoc}
  <section class="page-stack memory-view-page">
    <header class="memory-hero">
      <div class="memory-hero-copy">
        <h1>{vm.title}</h1>
        <p class="memory-hero-sub">{vm.description}</p>
      </div>
      <dl class="memory-hero-stats" aria-label="Memory summary">
        <div class={vm.presentCount === vm.docs.length ? 'is-complete' : 'is-partial'}>
          <dt>Docs</dt>
          <dd><strong>{vm.presentCount}</strong><span>of {vm.docs.length}</span></dd>
        </div>
      </dl>
    </header>

    <nav class="memory-tabs memory-tabs-v2" aria-label="Memory documents">
      {#each vm.docs as doc}
        <button
          class:active={doc.id === activeDoc.id}
          class:missing={doc.isMissing}
          type="button"
          onclick={() => (activeDocId = doc.id)}
        >
          <span class="memory-tab-dot" aria-hidden="true"></span>
          <span class="memory-tab-label">{doc.filename}</span>
        </button>
      {/each}
    </nav>

    <article class="page-panel memory-reader memory-reader-v2">
      <header class="memory-reader-head">
        <div class="memory-reader-title">
          <span class="memory-reader-filename">{activeDoc.filename}</span>
          <span class={`memory-reader-status ${activeDoc.isMissing ? 'is-missing' : 'is-present'}`}>
            <span class="memory-reader-status-dot" aria-hidden="true"></span>
            {activeDoc.isMissing ? 'Empty' : 'Present'}
          </span>
        </div>
        <div class="memory-reader-actions">
          {#if vm.askPmaHref}
            <a class="memory-action-link" href={href(vm.askPmaHref)}>Ask PMA</a>
          {/if}
          <button class="memory-copy-button" type="button" onclick={copyActiveDoc} disabled={activeDoc.isMissing}>
            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
              <rect x="4" y="4" width="9" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4" />
              <path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            <span>{copyState}</span>
          </button>
          {#if onClose}
            <button class="memory-close-button" type="button" onclick={onClose} aria-label="Close memory panel">
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
        emptyMessage={`Ask PMA to refresh this memory before starting work that depends on shared context.`}
        emptyActionHref={vm.askPmaHref ? href(vm.askPmaHref) : undefined}
        emptyActionLabel="Ask PMA to update"
        editable={activeDoc.filename !== 'context_log.md'}
        onSave={onSaveDoc}
      />
    </article>
  </section>
{/if}

<style>
  .memory-view-page {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    width: 100%;
    gap: var(--space-2);
    overflow: hidden;
  }

  .memory-hero {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-4);
    padding: 0 2px;
    flex: 0 0 auto;
  }

  .memory-hero-copy {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .memory-hero h1 {
    margin: 0;
    font-size: var(--font-size-3);
    font-weight: 650;
    letter-spacing: -0.018em;
    line-height: 1.2;
  }

  .memory-hero-sub {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.4;
    max-width: 64ch;
  }

  .memory-hero-stats {
    display: flex;
    align-items: stretch;
    gap: 0;
    margin: 0;
    padding: 4px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
  }

  .memory-hero-stats > div {
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 1px var(--space-2);
  }

  .memory-hero-stats dt {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 11px;
    font-weight: 500;
  }

  .memory-hero-stats dd {
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

  .memory-hero-stats dd span {
    color: var(--color-ink-faint);
    font-weight: 500;
    font-size: var(--font-size-0);
  }

  .memory-hero-stats > div.is-complete dd strong { color: var(--color-success); }
  .memory-hero-stats > div.is-partial dd strong { color: var(--color-warning); }

  .memory-tabs-v2 {
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

  .memory-tabs-v2 button {
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
    font-size: var(--font-size-1);
    font-weight: 500;
    line-height: 1.2;
    transition: background-color var(--transition-fast), color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .memory-tabs-v2 button:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .memory-tabs-v2 button.active {
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px var(--color-border-subtle);
  }

  .memory-tab-dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--color-success);
    flex: 0 0 auto;
  }

  .memory-tabs-v2 button.missing .memory-tab-dot {
    background: var(--color-border-strong);
  }

  .memory-tabs-v2 button.active .memory-tab-dot {
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 14%, transparent);
  }

  .memory-tab-label {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    font-weight: 500;
    color: var(--color-ink-muted);
  }

  .memory-tabs-v2 button.active .memory-tab-label {
    color: var(--color-ink);
  }

  .memory-reader-v2 {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 0;
    overflow: hidden;
  }

  .memory-reader-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex: 0 0 auto;
    padding: var(--space-2) var(--space-4);
    border-bottom: 1px solid var(--color-border-subtle);
    background: linear-gradient(180deg, var(--color-surface), var(--color-surface-sunken));
  }

  .memory-reader-title {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
  }

  .memory-reader-filename {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    font-weight: 600;
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .memory-reader-status {
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

  .memory-reader-status.is-present {
    background: var(--color-success-soft);
    color: var(--color-success);
  }

  .memory-reader-status.is-missing {
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
  }

  .memory-reader-status-dot {
    width: 5px;
    height: 5px;
    border-radius: 999px;
    background: currentColor;
  }

  .memory-reader-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 0 0 auto;
  }

  .memory-action-link {
    display: inline-flex;
    align-items: center;
    height: 26px;
    padding: 0 10px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: var(--font-size-0);
    font-weight: 500;
    text-decoration: none;
    transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
  }

  .memory-action-link:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .memory-copy-button {
    display: inline-flex;
    align-items: center;
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

  .memory-copy-button:hover:not(:disabled) {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .memory-copy-button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .memory-close-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
  }

  .memory-close-button:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .memory-reader-v2 :global(.markdown-body),
  .memory-reader-v2 :global(.markdown-editor-shell) {
    margin: 0;
    width: 100%;
    padding: var(--space-5) var(--space-6) var(--space-6);
    max-width: none;
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
  }

  .memory-reader-v2 :global(.markdown-body) {
    margin-top: 0;
  }

  @media (max-width: 760px) {
    .memory-hero {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
    }

    .memory-hero-stats {
      align-self: flex-start;
    }

    .memory-reader-head {
      padding: var(--space-3) var(--space-4);
    }

    .memory-reader-v2 :global(.markdown-body),
    .memory-reader-v2 :global(.markdown-editor-shell) {
      padding: var(--space-4);
    }
  }
</style>
