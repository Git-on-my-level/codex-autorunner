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
    const [repos, worktrees] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);
    const repoList = repos.ok ? repos.data : [];
    const worktreeList = worktrees.ok ? worktrees.data : [];
    const isKnownWorkspace =
      repoList.some((repo) => repo.id === workspaceId) ||
      worktreeList.some((worktree) => worktree.id === workspaceId);
    if (!isKnownWorkspace) {
      vm = buildContextspaceViewModel(workspaceId, [], repoList, worktreeList);
      loading = false;
      return;
    }
    const docs = await pmaApi.contextspace.listDocuments(workspaceId);
    if (!docs.ok) {
      error = docs.error;
      loading = false;
      return;
    }
    vm = buildContextspaceViewModel(
      workspaceId,
      docs.data,
      repoList,
      worktreeList
    );
    loading = false;
  }
</script>

<ContextspaceView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {vm}
  errorMessage={error?.message ?? null}
/>
