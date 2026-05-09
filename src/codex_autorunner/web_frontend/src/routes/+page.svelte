<script lang="ts">
  import { goto } from '$app/navigation';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { onMount } from 'svelte';

  onMount(() => {
    void goto(href('/chats'), { replaceState: true });
  });
</script>

<section class="page-panel">
  <p>Opening Chats...</p>
</section>
