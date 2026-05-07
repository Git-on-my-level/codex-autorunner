<script lang="ts">
  import { page } from '$app/state';
  import ScopedMemoryPage from '$lib/components/ScopedMemoryPage.svelte';

  const workspaceId = $derived(page.params.workspaceId ?? 'local');
</script>

<ScopedMemoryPage {workspaceId} />
