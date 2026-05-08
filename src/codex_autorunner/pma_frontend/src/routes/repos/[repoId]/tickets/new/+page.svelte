<script lang="ts">
  import { page } from '$app/state';
  import ScopedNewTicketPage from '$lib/components/ScopedNewTicketPage.svelte';
  import type { ScopedTicketQueueConfig } from '$lib/viewModels/scopedTicketQueue';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'repo',
    resourceId: repoId,
    apiBasePath: `/repos/${encodeURIComponent(repoId)}/api/flows`,
    displayLabel: 'repo'
  });
  const listHref = $derived(`/repos/${encodeURIComponent(repoId)}/tickets`);
</script>

<ScopedNewTicketPage {queueConfig} {listHref} />
