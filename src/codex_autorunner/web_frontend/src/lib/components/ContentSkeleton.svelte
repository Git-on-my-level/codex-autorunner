<script lang="ts">
  let {
    variant = 'index',
    rows = 5
  }: {
    variant?: 'index' | 'chat-list' | 'detail' | 'content';
    rows?: number;
  } = $props();

  const rowRange = $derived(Array.from({ length: rows }, (_, i) => i));
</script>

{#if variant === 'index'}
  <section class="page-stack skeleton-page" aria-busy="true" aria-label="Loading content">
    <div class="skeleton-bar skeleton-bar--search"></div>
    <div class="skeleton-filter-row">
      {#each [0, 1, 2] as _}
        <div class="skeleton-bar skeleton-bar--chip"></div>
      {/each}
    </div>
    {#each rowRange as i}
      <div class="skeleton-card" style={`--delay: ${i * 60}ms`}>
        <div class="skeleton-avatar"></div>
        <div class="skeleton-card-body">
          <div class="skeleton-bar skeleton-bar--title"></div>
          <div class="skeleton-bar skeleton-bar--meta"></div>
        </div>
        <div class="skeleton-card-counts">
          <div class="skeleton-bar skeleton-bar--count"></div>
        </div>
      </div>
    {/each}
  </section>
{:else if variant === 'chat-list'}
  <div class="skeleton-chat-list" aria-busy="true" aria-label="Loading chats">
    {#each rowRange as i}
      <div class="skeleton-chat-row" style={`--delay: ${i * 50}ms`}>
        <div class="skeleton-avatar skeleton-avatar--sm"></div>
        <div class="skeleton-chat-body">
          <div class="skeleton-bar skeleton-bar--title"></div>
          <div class="skeleton-bar skeleton-bar--meta-sm"></div>
        </div>
      </div>
    {/each}
  </div>
{:else if variant === 'detail'}
  <section class="page-stack skeleton-page" aria-busy="true" aria-label="Loading content">
    <div class="skeleton-detail-hero">
      <div class="skeleton-bar skeleton-bar--heading"></div>
      <div class="skeleton-bar skeleton-bar--subtitle"></div>
    </div>
    <div class="skeleton-detail-body">
      {#each [0, 1, 2, 3, 4] as i}
        <div class="skeleton-bar skeleton-bar--line" style={`--delay: ${i * 40}ms; width: ${90 - i * 8}%`}></div>
      {/each}
      <div class="skeleton-bar skeleton-bar--line" style="width: 60%"></div>
    </div>
  </section>
{:else}
  <section class="page-stack skeleton-page" aria-busy="true" aria-label="Loading content">
    <div class="skeleton-bar skeleton-bar--heading"></div>
    <div class="skeleton-bar skeleton-bar--line" style="width: 85%"></div>
    <div class="skeleton-bar skeleton-bar--line" style="width: 65%"></div>
    <div class="skeleton-card">
      <div class="skeleton-card-body">
        <div class="skeleton-bar skeleton-bar--title"></div>
        <div class="skeleton-bar skeleton-bar--meta"></div>
      </div>
    </div>
    <div class="skeleton-card">
      <div class="skeleton-card-body">
        <div class="skeleton-bar skeleton-bar--title"></div>
        <div class="skeleton-bar skeleton-bar--meta"></div>
      </div>
    </div>
  </section>
{/if}

<style>
  .skeleton-page {
    gap: var(--space-3);
  }

  .skeleton-bar {
    height: 14px;
    border-radius: 6px;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      color-mix(in srgb, var(--color-surface-muted) 60%, var(--color-bg)),
      var(--color-surface-muted)
    );
    background-size: 220% 100%;
    animation: skeleton-shimmer 1.4s ease-in-out infinite;
    animation-delay: var(--delay, 0ms);
  }

  .skeleton-bar--search {
    width: min(100%, 320px);
    height: 34px;
    border-radius: 8px;
  }

  .skeleton-bar--chip {
    width: 56px;
    height: 28px;
    border-radius: 999px;
  }

  .skeleton-bar--title {
    width: 55%;
    height: 15px;
  }

  .skeleton-bar--meta {
    width: 38%;
    height: 12px;
    margin-top: var(--space-1);
  }

  .skeleton-bar--meta-sm {
    width: 45%;
    height: 11px;
    margin-top: 3px;
  }

  .skeleton-bar--count {
    width: 48px;
    height: 24px;
    border-radius: 999px;
  }

  .skeleton-bar--heading {
    width: 35%;
    height: 22px;
  }

  .skeleton-bar--subtitle {
    width: 50%;
    height: 13px;
    margin-top: 3px;
  }

  .skeleton-bar--line {
    height: 13px;
  }

  .skeleton-filter-row {
    display: flex;
    gap: var(--space-2);
  }

  .skeleton-card {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-4) var(--space-5);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
    animation: skeleton-shimmer 1.4s ease-in-out infinite;
    animation-delay: var(--delay, 0ms);
  }

  .skeleton-card-body {
    flex: 1 1 auto;
    min-width: 0;
    display: grid;
    gap: 0;
  }

  .skeleton-card-counts {
    flex-shrink: 0;
  }

  .skeleton-avatar {
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: var(--color-surface-muted);
    flex-shrink: 0;
  }

  .skeleton-avatar--sm {
    width: 28px;
    height: 28px;
    border-radius: 8px;
  }

  .skeleton-chat-list {
    display: grid;
    gap: var(--space-2);
    padding: var(--space-2);
  }

  .skeleton-chat-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-radius: 8px;
  }

  .skeleton-chat-body {
    flex: 1 1 auto;
    min-width: 0;
    display: grid;
    gap: 0;
  }

  .skeleton-detail-hero {
    display: flex;
    flex-direction: column;
    gap: 3px;
    margin-bottom: var(--space-3);
  }

  .skeleton-detail-body {
    display: grid;
    gap: var(--space-3);
    padding: var(--space-4);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
  }

  .skeleton-card .skeleton-bar,
  .skeleton-detail-body .skeleton-bar {
    background: linear-gradient(
      90deg,
      var(--color-surface-sunken),
      color-mix(in srgb, var(--color-surface-sunken) 55%, var(--color-surface)),
      var(--color-surface-sunken)
    );
    background-size: 220% 100%;
  }

  @keyframes skeleton-shimmer {
    from {
      background-position: 100% 0;
    }
    to {
      background-position: -100% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-bar,
    .skeleton-card {
      animation: none;
    }
  }
</style>
