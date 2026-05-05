<script lang="ts">
  import { onMount } from 'svelte';
  import PmaMemoryView from '$lib/components/PmaMemoryView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { buildPmaMemoryViewModel, type PmaMemoryViewModel } from '$lib/viewModels/pmaMemory';

  let vm = $state<PmaMemoryViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadPmaMemory();
  });

  async function loadPmaMemory(): Promise<void> {
    loading = true;
    error = null;
    const docs = await pmaApi.pma.listDocsWithContent();
    if (!docs.ok) {
      error = docs.error;
      loading = false;
      return;
    }
    vm = buildPmaMemoryViewModel(docs.data);
    loading = false;
  }
</script>

<PmaMemoryView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {vm}
  errorMessage={error?.message ?? null}
/>
