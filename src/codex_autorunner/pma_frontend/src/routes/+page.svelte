<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  onMount(() => {
    void goto('/pma', { replaceState: true });
  });
</script>

<section class="page-panel">
  <p>Opening PMA...</p>
</section>
