<script lang="ts">
  import { page } from '$app/state';
  import ScopedNewTicketPage from '$lib/components/ScopedNewTicketPage.svelte';
  import type { ScopedTicketQueueConfig } from '$lib/viewModels/scopedTicketQueue';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'worktree',
    resourceId: worktreeId,
    apiBasePath: `/repos/${encodeURIComponent(worktreeId)}/api/flows`,
    displayLabel: 'worktree',
    parentRepoId: repoId
  });
  const listHref = $derived(
    `/repos/${encodeURIComponent(repoId)}/worktrees/${encodeURIComponent(worktreeId)}/tickets`
  );
</script>

<ScopedNewTicketPage {queueConfig} {listHref} />
