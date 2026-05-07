<script lang="ts">
  import { page } from '$app/state';
  import ScopedMemoryPage from '$lib/components/ScopedMemoryPage.svelte';

  const scope = $derived({ kind: 'repo' as const, id: page.params.repoId ?? 'unknown-repo' });
</script>

<ScopedMemoryPage {scope} />
