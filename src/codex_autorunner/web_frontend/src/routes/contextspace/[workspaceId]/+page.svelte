<script lang="ts">
  import { page } from '$app/state';
  import ScopedContextspacePage from '$lib/components/ScopedContextspacePage.svelte';

  const workspaceId = $derived(page.params.workspaceId ?? 'local');
</script>

<ScopedContextspacePage {workspaceId} />
