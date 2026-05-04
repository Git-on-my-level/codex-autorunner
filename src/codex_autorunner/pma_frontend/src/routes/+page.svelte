<script lang="ts">
  import { goto } from '$app/navigation';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { onMount } from 'svelte';

  onMount(() => {
    void goto(href('/pma'), { replaceState: true });
  });
</script>

<section class="page-panel">
  <p>Opening PMA...</p>
</section>
