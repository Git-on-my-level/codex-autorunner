<script lang="ts">
  import type { PmaMemoryViewModel } from '$lib/viewModels/pmaMemory';

  let {
    state: viewState,
    vm = null,
    errorMessage = null
  }: {
    state: 'loading' | 'error' | 'ready';
    vm?: PmaMemoryViewModel | null;
    errorMessage?: string | null;
  } = $props();

  let activeDocId = $state<string>('AGENTS.md');
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
    <div class="state-panel">Loading PMA memory...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load PMA memory. {errorMessage}</div>
  </section>
{:else if vm && activeDoc}
  <section class="page-stack contextspace-page">
    <div class="section-heading detail-heading">
      <div>
        <p class="eyebrow">{vm.eyebrow}</p>
        <h1>{vm.title}</h1>
        <p>{vm.description} {vm.presentCount} of {vm.docs.length} docs have content.</p>
      </div>
    </div>

    <div class="contextspace-layout">
      <aside class="page-panel contextspace-doc-list" aria-label="PMA memory documents">
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
            <strong>{activeDoc.label} has no content</strong>
            <p>PMA has not written content to this memory document yet.</p>
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
