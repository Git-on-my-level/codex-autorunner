<script lang="ts">
  import { page } from '$app/state';
  import { onMount, untrack } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndRetireState, confirmAndRetireWorktree } from '$lib/actions/repoWorktreeActions';
  import { webApi } from '$lib/api/client';
  import {
    createRepoWorktreeDetailSession,
    type RepoWorktreeDetailSessionState
  } from '$lib/application/repoWorktreeDetailSession';

  let { data = { repoId: '', result: { status: 'cold' as const, tags: [] } } } = $props();
  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const session = createRepoWorktreeDetailSession({
    ownerKind: 'repo',
    ownerId: untrack(() => repoId),
    loaderResult: untrack(() => data.result),
    dependencies: {
      syncRepoMain: webApi.hub.syncRepoMain,
      retireWorktree: confirmAndRetireWorktree,
      retireState: confirmAndRetireState
    }
  });
  let sessionState = $state<RepoWorktreeDetailSessionState>(session.state);

  $effect(() => {
    session.setOwner('repo', repoId, data.result);
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
  onRetireState={(target) => runSessionCommand(() => session.retireState(target))}
  onSyncRepo={() => runSessionCommand(() => session.syncRepo())}
  syncRepoBusy={sessionState.syncRepoBusy}
  errorMessage={sessionState.error?.message ?? null}
/>
