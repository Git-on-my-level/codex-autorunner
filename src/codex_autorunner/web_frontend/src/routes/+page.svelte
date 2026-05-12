<script lang="ts">
  import { goto } from '$app/navigation';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import { onMount } from 'svelte';

  onMount(() => {
    void goto(href('/chats'), { replaceState: true });
  });
</script>

<ContentSkeleton variant="content" rows={3} />
