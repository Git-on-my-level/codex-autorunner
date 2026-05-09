<script lang="ts">
  import { onMount } from 'svelte';
  import MemoryView from '$lib/components/MemoryView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { buildMemoryViewModel, type MemoryViewModel } from '$lib/viewModels/memory';
  import type { ScopeRef } from '$lib/viewModels/scope';

  let {
    open = false,
    scope,
    onClose
  }: {
    open?: boolean;
    scope: ScopeRef;
    onClose: () => void;
  } = $props();

  let vm = $state<MemoryViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    if (open) void loadMemory();
  });

  $effect(() => {
    if (open) void loadMemory();
  });

  async function loadMemory(): Promise<void> {
    loading = true;
    error = null;
    const docs = await pmaApi.pma.listDocsWithContent();
    if (!docs.ok) {
      error = docs.error;
      vm = null;
      loading = false;
      return;
    }
    vm = buildMemoryViewModel(scope, docs.data);
    loading = false;
  }

  async function saveDoc(docId: string, content: string): Promise<boolean> {
    const result = await pmaApi.pma.updateDoc(docId, content);
    if (!result.ok) {
      error = result.error;
      return false;
    }
    if (vm) {
      vm = {
        ...vm,
        docs: vm.docs.map((doc) =>
          doc.id === docId
            ? { ...doc, content, html: doc.html, isMissing: !content.trim() }
            : doc
        ),
        presentCount: vm.docs.filter((doc) =>
          doc.id === docId ? !!(content.trim()) : !doc.isMissing
        ).length
      };
    }
    return true;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      onClose();
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div class="memory-rail-backdrop" onclick={onClose} role="presentation">
    <div
      class="memory-rail-panel"
      role="complementary"
      aria-label="Memory panel"
      onclick={(event) => event.stopPropagation()}
      onkeydown={handleKeydown}
    >
      <MemoryView
        state={loading ? 'loading' : error ? 'error' : 'ready'}
        {vm}
        errorMessage={error?.message ?? null}
        onSaveDoc={saveDoc}
        onClose={onClose}
      />
    </div>
  </div>
{/if}

<style>
  .memory-rail-backdrop {
    position: fixed;
    inset: 0;
    z-index: 40;
    display: flex;
    justify-content: flex-end;
    background: color-mix(in srgb, var(--color-surface) 60%, transparent);
  }

  .memory-rail-panel {
    width: min(560px, 90vw);
    height: 100%;
    display: flex;
    flex-direction: column;
    background: var(--color-surface);
    border-left: 1px solid var(--color-border-subtle);
    box-shadow: -4px 0 24px color-mix(in srgb, var(--color-ink) 8%, transparent);
    overflow: hidden;
    padding: var(--space-4);
    padding-right: var(--space-3);
  }

  @media (max-width: 760px) {
    .memory-rail-panel {
      width: 100vw;
    }
  }
</style>
