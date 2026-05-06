<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    title,
    subtitle = null,
    stats = undefined
  }: {
    title: string;
    subtitle?: string | null;
    stats?: Snippet | undefined;
  } = $props();
</script>

<header class="page-hero">
  <div class="page-hero-copy">
    <h1>{title}</h1>
    {#if subtitle}
      <p class="page-hero-sub">{subtitle}</p>
    {/if}
  </div>
  {#if stats}
    {@render stats()}
  {/if}
</header>

<style>
  .page-hero {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-4);
    margin-bottom: var(--space-2);
  }

  .page-hero-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .page-hero h1 {
    margin: 0;
    font-size: var(--font-size-4);
    font-weight: 650;
    letter-spacing: -0.022em;
    line-height: 1.18;
  }

  .page-hero-sub {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  @media (max-width: 760px) {
    .page-hero {
      align-items: flex-start;
    }
  }
</style>
