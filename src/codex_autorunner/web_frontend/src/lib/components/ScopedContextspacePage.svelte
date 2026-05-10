<script lang="ts">
  import { onMount } from 'svelte';
  import ContextspaceView from '$lib/components/ContextspaceView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { buildContextspaceViewModel, type ContextspaceViewModel } from '$lib/viewModels/contextspace';
  import type { RepoSummary, WorktreeSummary } from '$lib/viewModels/domain';
  import type { ScopeRef } from '$lib/viewModels/scope';

  let {
    scope,
    workspaceId
  }: {
    scope?: ScopeRef;
    workspaceId?: string;
  } = $props();

  let vm = $state<ContextspaceViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let repoList = $state<RepoSummary[]>([]);
  let worktreeList = $state<WorktreeSummary[]>([]);

  onMount(() => {
    void loadContextspace();
  });

  async function resolveWorkspaceId(): Promise<string> {
    if (scope?.kind === 'repo' || scope?.kind === 'worktree') return scope.id;
    if (workspaceId) return workspaceId;
    return 'local';
  }

  async function loadInventory(): Promise<void> {
    const [repos, worktrees] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);
    repoList = repos.ok ? repos.data : [];
    worktreeList = worktrees.ok ? worktrees.data : [];
  }

  async function loadContextspace(): Promise<void> {
    loading = true;
    error = null;
    await loadInventory();
    const id = await resolveWorkspaceId();
    const docs = await pmaApi.contextspace.listDocuments(id);
    if (!docs.ok) {
      error = docs.error;
      vm = null;
      loading = false;
      return;
    }
    vm = buildContextspaceViewModel(id, docs.data, repoList, worktreeList);
    loading = false;
  }

  async function saveDoc(docId: string, content: string): Promise<boolean> {
    if (!vm) return false;
    const result = await pmaApi.contextspace.updateDocument(vm.workspaceId, docId, content);
    if (!result.ok) {
      error = result.error;
      return false;
    }
    vm = buildContextspaceViewModel(vm.workspaceId, result.data, repoList, worktreeList);
    return true;
  }
</script>

<ContextspaceView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {vm}
  errorMessage={error?.message ?? null}
  onSaveDoc={saveDoc}
/>
