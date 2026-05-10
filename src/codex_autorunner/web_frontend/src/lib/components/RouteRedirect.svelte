<script lang="ts">
  import { goto } from '$app/navigation';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { onMount } from 'svelte';

  let { to, label = 'Opening page...' }: { to: string; label?: string } = $props();

  onMount(() => {
    void goto(href(to), { replaceState: true });
  });
</script>

<section class="page-panel">
  <p>{label}</p>
</section>
