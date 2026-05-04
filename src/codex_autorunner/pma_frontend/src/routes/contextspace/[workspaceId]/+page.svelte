<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import ContextspaceView from '$lib/components/ContextspaceView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildContextspaceViewModel,
    type ContextspaceViewModel
  } from '$lib/viewModels/contextspace';

  const workspaceId = $derived(page.params.workspaceId ?? 'local');
  let vm = $state<ContextspaceViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadContextspace();
  });

  async function loadContextspace(): Promise<void> {
    loading = true;
    error = null;
    const [docs, repos, worktrees] = await Promise.all([
      pmaApi.contextspace.listDocuments(workspaceId),
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);
    if (!docs.ok) {
      error = docs.error;
      loading = false;
      return;
    }
    vm = buildContextspaceViewModel(
      workspaceId,
      docs.data,
      repos.ok ? repos.data : [],
      worktrees.ok ? worktrees.data : []
    );
    loading = false;
  }
</script>

<ContextspaceView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {vm}
  errorMessage={error?.message ?? null}
/>
