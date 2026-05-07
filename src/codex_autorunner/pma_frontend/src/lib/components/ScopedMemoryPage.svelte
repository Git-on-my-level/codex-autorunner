<script lang="ts">
  import { onMount } from 'svelte';
  import MemoryView from '$lib/components/MemoryView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { buildMemoryViewModel, type MemoryViewModel } from '$lib/viewModels/memory';
  import type { ScopeRef } from '$lib/viewModels/scope';

  let {
    scope,
    workspaceId
  }: {
    scope?: ScopeRef;
    workspaceId?: string;
  } = $props();

  let vm = $state<MemoryViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadMemory();
  });

  async function resolveScope(): Promise<ScopeRef> {
    if (scope) return scope;
    if (!workspaceId) return { kind: 'hub' };
    const [repos, worktrees] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);
    const repoList = repos.ok ? repos.data : [];
    const worktreeList = worktrees.ok ? worktrees.data : [];
    const repo = repoList.find((r) => r.id === workspaceId);
    if (repo) return { kind: 'repo', id: repo.id };
    const worktree = worktreeList.find((w) => w.id === workspaceId);
    if (worktree) return { kind: 'worktree', id: worktree.id, parentRepoId: worktree.repoId ?? '' };
    return { kind: 'repo', id: workspaceId };
  }

  async function loadMemory(): Promise<void> {
    loading = true;
    error = null;
    const resolvedScope = await resolveScope();
    const docs = await pmaApi.memory.listDocs(resolvedScope);
    if (!docs.ok) {
      error = docs.error;
      vm = null;
      loading = false;
      return;
    }
    vm = buildMemoryViewModel(resolvedScope, docs.data);
    loading = false;
  }

  async function saveDoc(docId: string, content: string): Promise<boolean> {
    if (!vm) return false;
    const result = await pmaApi.memory.saveDoc(vm.scope, docId, content);
    if (!result.ok) {
      error = result.error;
      return false;
    }
    const docs = await pmaApi.memory.listDocs(vm.scope);
    if (docs.ok) {
      vm = buildMemoryViewModel(vm.scope, docs.data);
    }
    return true;
  }
</script>

<MemoryView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {vm}
  errorMessage={error?.message ?? null}
  onSaveDoc={saveDoc}
/>
