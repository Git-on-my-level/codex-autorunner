<script lang="ts">
  import StatusPill from './StatusPill.svelte';
  import type { RunHistoryEntry } from '$lib/viewModels/runHistory';
  import { rowRelativeTime } from '$lib/viewModels/ticket';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';

  let {
    runs,
    emptyMessage = 'No runs yet.',
    limit = 5
  }: {
    runs: RunHistoryEntry[];
    emptyMessage?: string;
    limit?: number;
  } = $props();

  let expanded = $state(false);
  const visibleRuns = $derived(expanded ? runs : runs.slice(0, Math.max(0, limit)));
</script>

{#if runs.length === 0}
  <div class="state-panel empty-state compact-empty">
    <p>{emptyMessage}</p>
  </div>
{:else}
  <div class="run-history-list">
    {#each visibleRuns as run}
      {@const body = run.summary ?? 'No summary recorded'}
      {@const time = rowRelativeTime({ updatedAt: run.timestamp })}
      {#if run.href}
        <a class="run-history-row" href={href(run.href)} data-sveltekit-preload-data="hover">
          <StatusPill status={run.status} />
          <span class="run-history-body">
            <strong>{run.title}</strong>
            <span>{body}</span>
          </span>
          <span class="run-history-meta">
            <span>{time}</span>
            <span aria-hidden="true">→</span>
          </span>
        </a>
      {:else}
        <div class="run-history-row">
          <StatusPill status={run.status} />
          <span class="run-history-body">
            <strong>{run.title}</strong>
            <span>{body}</span>
          </span>
          <span class="run-history-meta">{time}</span>
        </div>
      {/if}
    {/each}
    {#if runs.length > limit}
      <button type="button" class="ghost-button run-history-more" onclick={() => (expanded = !expanded)}>
        {expanded ? 'Show fewer' : `Show ${runs.length - limit} more`}
      </button>
    {/if}
  </div>
{/if}

<style>
  .run-history-list {
    display: grid;
    gap: var(--space-2);
  }

  .run-history-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--space-3);
    align-items: center;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
    color: inherit;
    text-decoration: none;
  }

  a.run-history-row:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-sunken, var(--color-surface));
  }

  .run-history-body {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .run-history-body strong {
    font-size: var(--font-size-1);
    font-weight: 650;
  }

  .run-history-body span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-history-meta {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-ink-faint);
    font-size: var(--font-size-0);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .run-history-more {
    justify-self: start;
  }

  @media (max-width: 720px) {
    .run-history-row {
      grid-template-columns: auto minmax(0, 1fr);
    }

    .run-history-meta {
      grid-column: 2;
      justify-self: start;
    }
  }
</style>
