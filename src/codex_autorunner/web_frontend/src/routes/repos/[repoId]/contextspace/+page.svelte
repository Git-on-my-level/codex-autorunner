<script lang="ts">
  import { page } from '$app/state';
  import ScopedContextspacePage from '$lib/components/ScopedContextspacePage.svelte';

  const scope = $derived({ kind: 'repo' as const, id: page.params.repoId ?? 'unknown-repo' });
</script>

<ScopedContextspacePage {scope} />
