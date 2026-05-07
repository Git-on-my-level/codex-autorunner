<script lang="ts">
  import { page } from '$app/state';
  import ScopedMemoryPage from '$lib/components/ScopedMemoryPage.svelte';

  const scope = $derived({
    kind: 'worktree' as const,
    id: page.params.worktreeId ?? 'unknown-worktree',
    parentRepoId: page.params.repoId ?? ''
  });
</script>

<ScopedMemoryPage {scope} />
