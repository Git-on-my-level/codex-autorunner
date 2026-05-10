<script lang="ts">
  import MasterDetail from './MasterDetail.svelte';

  let { viewport, mode }: { viewport: string; mode: 'list' | 'detail' } = $props();
</script>

<div data-viewport={viewport}>
  <MasterDetail label="Harness" selected={true} {mode}>
    {#snippet list()}
      <aside>Queue list region</aside>
    {/snippet}
    {#snippet detail()}
      <main>Conversation detail region</main>
    {/snippet}
    {#snippet rail()}
      <nav>Scope rail region</nav>
    {/snippet}
  </MasterDetail>
</div>
