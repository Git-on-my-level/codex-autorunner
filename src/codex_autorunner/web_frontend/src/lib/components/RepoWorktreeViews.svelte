<script lang="ts">
  import type { RepoWorktreeDetailViewModel, RepoWorktreeIndexViewModel } from '$lib/viewModels/repoWorktree';
  import type { PartialPageIssue } from '$lib/api/client';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import RepoWorktreeDetail from './RepoWorktreeDetail.svelte';
  import RepoWorktreeIndex from './RepoWorktreeIndex.svelte';

  let {
    state: viewState,
    mode,
    index = null,
    detail = null,
    errorMessage = null,
    sectionIssues = [],
    onRetry = undefined,
    onArchiveWorktree = undefined,
    onRetireWorktree = undefined,
    onRetireState = undefined,
    onRepoPin = undefined,
    onSyncRepo = undefined,
    syncRepoBusy = false,
    onCreateRepo = undefined,
    onCreateWorktree = undefined,
    onOpenRepoSettings = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'index' | 'detail';
    index?: RepoWorktreeIndexViewModel | null;
    detail?: RepoWorktreeDetailViewModel | null;
    errorMessage?: string | null;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
    onArchiveWorktree?: ((worktree: { id: string; label: string; archived: boolean }) => void | Promise<void>) | undefined;
    onRetireWorktree?: ((worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }) => void | Promise<void>) | undefined;
    onRetireState?: ((target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }) => void | Promise<void>) | undefined;
    onRepoPin?: ((target: { id: string; pinned: boolean }) => void | Promise<void>) | undefined;
    onSyncRepo?: (() => void | Promise<void>) | undefined;
    syncRepoBusy?: boolean;
    onCreateRepo?: (() => void) | undefined;
    onCreateWorktree?: ((target: { id: string; label: string }) => void) | undefined;
    onOpenRepoSettings?: ((target: { id: string; label: string; worktreeSetupCommands: string[] }) => void) | undefined;
  } = $props();
</script>

{#if viewState === 'loading'}
  <ContentSkeleton variant="index" rows={4} />
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load workspace state. {errorMessage}</div>
  </section>
{:else if mode === 'index' && index}
  <RepoWorktreeIndex
    {index}
    {sectionIssues}
    {onRetry}
    {onArchiveWorktree}
    {onRetireWorktree}
    {onRetireState}
    {onRepoPin}
    {onCreateRepo}
    {onCreateWorktree}
    {onOpenRepoSettings}
  />
{:else if mode === 'detail' && detail}
  <RepoWorktreeDetail
    {detail}
    {sectionIssues}
    {onRetry}
    {onRetireWorktree}
    {onRetireState}
    {onSyncRepo}
    {syncRepoBusy}
  />
{/if}
