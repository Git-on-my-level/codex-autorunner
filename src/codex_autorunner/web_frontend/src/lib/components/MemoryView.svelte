<script lang="ts">
  import MarkdownDocViewer from '$lib/components/MarkdownDocViewer.svelte';
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

  const editableDoc = (doc: { filename: string }) => doc.filename !== 'context_log.md';
</script>

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading memory...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load memory. {errorMessage}</div>
  </section>
{:else if vm}
  <MarkdownDocViewer
    title={vm.title}
    description={vm.description}
    docs={vm.docs}
    presentCount={vm.presentCount}
    ariaLabel="PMA memory documents"
    emptyMessage="PMA has not written content to this memory document yet."
    closeLabel="Close memory panel"
    {editableDoc}
    {onSaveDoc}
    {onClose}
  />
{/if}
