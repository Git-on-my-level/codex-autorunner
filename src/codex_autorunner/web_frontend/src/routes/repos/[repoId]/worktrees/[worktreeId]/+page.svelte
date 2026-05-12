<script lang="ts">
  import WorktreeDetailPage from '../../../../worktrees/[worktreeId]/+page.svelte';
  let { data } = $props();
</script>

<WorktreeDetailPage {data} />
