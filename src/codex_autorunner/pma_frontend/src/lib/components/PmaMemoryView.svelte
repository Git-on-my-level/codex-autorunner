<script lang="ts">
  import MarkdownDocViewer from '$lib/components/MarkdownDocViewer.svelte';
  import type { PmaMemoryViewModel } from '$lib/viewModels/pmaMemory';

  let {
    state: viewState,
    vm = null,
    errorMessage = null,
    onSaveDoc
  }: {
    state: 'loading' | 'error' | 'ready';
    vm?: PmaMemoryViewModel | null;
    errorMessage?: string | null;
    onSaveDoc?: (docId: string, content: string) => Promise<boolean> | boolean;
  } = $props();

  const editableDoc = (doc: { filename: string }) => doc.filename !== 'context_log.md';
</script>

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading PMA memory...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load PMA memory. {errorMessage}</div>
  </section>
{:else if vm}
  <MarkdownDocViewer
    title={vm.title}
    description={vm.description}
    docs={vm.docs}
    presentCount={vm.presentCount}
    ariaLabel="PMA memory documents"
    emptyMessage="PMA has not written content to this memory document yet."
    {editableDoc}
    {onSaveDoc}
  />
{/if}
