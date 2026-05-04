<script lang="ts">
  import type { ContextspaceViewModel, ContextspaceDocKind } from '$lib/viewModels/contextspace';

  let {
    state: viewState,
    vm = null,
    errorMessage = null
  }: {
    state: 'loading' | 'error' | 'ready';
    vm?: ContextspaceViewModel | null;
    errorMessage?: string | null;
  } = $props();

  let activeDocId = $state<ContextspaceDocKind>('active_context');
  let copyState = $state('Copy');

  const activeDoc = $derived(vm?.docs.find((doc) => doc.id === activeDocId) ?? vm?.docs[0] ?? null);

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
    <div class="state-panel">Loading contextspace docs...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load contextspace. {errorMessage}</div>
  </section>
{:else if vm && activeDoc}
  <section class="page-stack contextspace-page">
    <div class="section-heading detail-heading">
      <div>
        <p class="eyebrow">{vm.eyebrow}</p>
        <h1>{vm.title}</h1>
        <p>{vm.presentCount} of {vm.docs.length} standard docs have content.</p>
      </div>
      <div class="detail-actions">
        <a href={vm.openWorkspaceHref}>{vm.openWorkspaceLabel}</a>
        <a href={vm.askPmaHref}>Ask PMA to update</a>
      </div>
    </div>

    <div class="contextspace-layout">
      <aside class="page-panel contextspace-doc-list" aria-label="Contextspace documents">
        <h2>Documents</h2>
        <div class="doc-tab-list">
          {#each vm.docs as doc}
            <button
              class:active={doc.id === activeDoc.id}
              class:missing={doc.isMissing}
              type="button"
              onclick={() => (activeDocId = doc.id)}
            >
              <span>{doc.label}</span>
              <small>{doc.filename}{doc.isMissing ? ' · missing' : ''}</small>
            </button>
          {/each}
        </div>
      </aside>

      <article class="page-panel contextspace-reader">
        <div class="panel-heading-row">
          <div>
            <h2>{activeDoc.filename}</h2>
            <p>{activeDoc.isMissing ? 'No content has been written yet.' : 'Readable markdown preview.'}</p>
          </div>
          <button class="secondary-button" type="button" onclick={copyActiveDoc} disabled={activeDoc.isMissing}>
            {copyState}
          </button>
        </div>

        {#if activeDoc.isMissing}
          <div class="state-panel contextspace-empty">
            <strong>{activeDoc.label} is empty.</strong>
            <p>Ask PMA to update contextspace when the workspace needs refreshed shared memory.</p>
            <a class="inline-link" href={vm.askPmaHref}>Ask PMA to update</a>
          </div>
        {:else}
          <div class="markdown-body">
            {@html activeDoc.html}
          </div>
        {/if}
      </article>
    </div>
  </section>
{/if}
