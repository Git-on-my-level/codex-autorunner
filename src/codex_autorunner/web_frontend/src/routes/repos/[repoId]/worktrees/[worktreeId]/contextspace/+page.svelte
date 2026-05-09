<script lang="ts">
  import { page } from '$app/state';
  import ScopedContextspacePage from '$lib/components/ScopedContextspacePage.svelte';

  const scope = $derived({
    kind: 'worktree' as const,
    id: page.params.worktreeId ?? 'unknown-worktree',
    parentRepoId: page.params.repoId ?? ''
  });
</script>

<ScopedContextspacePage {scope} />
