<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, untrack } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndRetireWorktree } from '$lib/actions/repoWorktreeActions';
  import { webApi } from '$lib/api/client';
  import {
    createRepoWorktreeDetailSession,
    type RepoWorktreeDetailSessionState
  } from '$lib/application/repoWorktreeDetailSession';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';

  let { data = { worktreeId: '', result: { status: 'cold' as const, tags: [] } } } = $props();
  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  const session = createRepoWorktreeDetailSession({
    ownerKind: 'worktree',
    ownerId: untrack(() => worktreeId),
    loaderResult: untrack(() => data.result),
    dependencies: {
      syncRepoMain: webApi.hub.syncRepoMain,
      retireWorktree: confirmAndRetireWorktree,
      archiveState: confirmAndArchiveState,
      currentPath: () => stripRuntimeBasePath(page.url.pathname),
      redirect: (path) => goto(href(path), { replaceState: true })
    }
  });
  let sessionState = $state<RepoWorktreeDetailSessionState>(session.state);

  $effect(() => {
    session.setOwner('worktree', worktreeId, data.result);
    sessionState = session.state;
  });

  onMount(() => {
    void runSessionCommand(() => session.hydrate());
  });

  async function runSessionCommand(command: () => Promise<void>): Promise<void> {
    await command();
    sessionState = session.state;
  }
</script>

<AutoDismissNotice message={sessionState.notice?.message ?? null} tone={sessionState.notice?.tone ?? 'neutral'} />
<RepoWorktreeViews
  state={sessionState.loading ? 'loading' : sessionState.error ? 'error' : 'ready'}
  mode="detail"
  detail={sessionState.detail}
  sectionIssues={sessionState.sectionIssues}
  onRetry={() => runSessionCommand(() => session.load())}
  onRetireWorktree={(target) => runSessionCommand(() => session.retireWorktree(target))}
  onArchiveState={(target) => runSessionCommand(() => session.archiveState(target))}
  onSyncRepo={() => runSessionCommand(() => session.syncRepo())}
  syncRepoBusy={sessionState.syncRepoBusy}
  errorMessage={sessionState.error?.message ?? null}
/>
