<script lang="ts">
  import type { TicketSummary } from '$lib/viewModels/domain';

  type Stats = NonNullable<TicketSummary['diffStats']>;

  let {
    stats,
    extraClass = ''
  }: {
    stats: Stats | null;
    /** Optional wrapper classes (e.g. git-chip). */
    extraClass?: string;
  } = $props();

  const show = $derived(
    stats !== null &&
      (stats.insertions > 0 || stats.deletions > 0 || stats.filesChanged > 0)
  );

  const ariaLabel = $derived(
    stats === null || !show
      ? ''
      : `${stats.insertions} additions, ${stats.deletions} deletions, ${stats.filesChanged} files changed`
  );
</script>

{#if show && stats}
  <span class={`ticket-diff-stats ${extraClass}`.trim()} aria-label={ariaLabel}>
    {#if stats.insertions > 0}<span class="diff-stat-add">+{stats.insertions}</span>{/if}
    {#if stats.deletions > 0}<span class="diff-stat-del">-{stats.deletions}</span>{/if}
    {#if stats.filesChanged > 0}
      <span class="diff-stat-neutral">
        {stats.filesChanged}
        {stats.filesChanged === 1 ? 'file' : 'files'}
      </span>
    {/if}
  </span>
{/if}
