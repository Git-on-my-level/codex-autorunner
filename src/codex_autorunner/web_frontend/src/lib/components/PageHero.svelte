<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    title,
    subtitle = null,
    stats = undefined,
    actions = undefined
  }: {
    title: string;
    subtitle?: string | null;
    stats?: Snippet | undefined;
    actions?: Snippet | undefined;
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
  {#if actions}
    <div class="page-hero-actions">
      {@render actions()}
    </div>
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

  .page-hero-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
  }

  /* Mobile: the topbar breadcrumb already names the page, so the hero shrinks
     hard. Keep the <h1> for accessibility/semantic hierarchy, but downgrade it
     visually to a tight single line; clamp the subtitle to one line; collapse
     the bottom margin entirely so content cards begin within ~30px of the
     topbar. */
  @media (max-width: 760px) {
    .page-hero {
      gap: var(--space-2);
      margin-bottom: 0;
      align-items: center;
    }

    .page-hero-copy {
      gap: 0;
      flex: 1 1 auto;
    }

    .page-hero h1 {
      font-size: var(--font-size-2);
      letter-spacing: -0.012em;
      line-height: 1.25;
    }

    .page-hero-sub {
      font-size: var(--font-size-0);
      line-height: 1.35;
      overflow: hidden;
      text-overflow: ellipsis;
      display: -webkit-box;
      line-clamp: 1;
      -webkit-line-clamp: 1;
      -webkit-box-orient: vertical;
    }

    .page-hero-actions {
      flex-shrink: 0;
      gap: var(--space-1);
    }

    /* Hero actions on mobile: tight ghost icons rather than full rows.
       The .hero-action class shrinks via global mobile override below. */
  }
</style>
