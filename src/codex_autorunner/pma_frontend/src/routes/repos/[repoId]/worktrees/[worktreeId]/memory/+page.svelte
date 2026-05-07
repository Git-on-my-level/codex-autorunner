<script lang="ts">
  import { page } from '$app/state';
  import ScopedMemoryPage from '$lib/components/ScopedMemoryPage.svelte';

  const workspaceId = $derived(page.params.worktreeId ?? 'unknown-worktree');
</script>

<ScopedMemoryPage {workspaceId} />
