<script lang="ts">
  import type { Snippet } from 'svelte';

  type MasterDetailMode = 'list' | 'detail';

  let {
    label,
    selected = false,
    mode = 'detail',
    listLabel = 'List',
    detailLabel = 'Detail',
    showSwitch = true,
    onModeChange = undefined,
    rail = undefined,
    list,
    detail
  }: {
    label: string;
    selected?: boolean;
    mode?: MasterDetailMode;
    listLabel?: string;
    detailLabel?: string;
    showSwitch?: boolean;
    onModeChange?: (mode: MasterDetailMode) => void;
    rail?: Snippet;
    list: Snippet;
    detail: Snippet;
  } = $props();

  const hasRail = $derived(Boolean(rail));

  function setMode(nextMode: MasterDetailMode): void {
    onModeChange?.(nextMode);
  }
</script>

<section
  class:has-rail={hasRail}
  class="master-detail"
  data-mode={mode}
  aria-label={label}
>
  {#if showSwitch}
    <div class="master-detail-switch" role="tablist" aria-label={`${label} panels`}>
      <button
        class:active={mode === 'list'}
        type="button"
        role="tab"
        aria-selected={mode === 'list'}
        onclick={() => setMode('list')}
      >
        {listLabel}
      </button>
      <button
        class:active={mode === 'detail'}
        type="button"
        role="tab"
        aria-selected={mode === 'detail'}
        onclick={() => setMode('detail')}
        disabled={!selected}
      >
        {detailLabel}
      </button>
    </div>
  {/if}

  <div class:hidden-mobile-pane={mode !== 'list'} class="master-detail-list">
    {@render list()}
  </div>

  <div class:hidden-mobile-pane={mode !== 'detail'} class="master-detail-main">
    {@render detail()}
  </div>

  {#if rail}
    <div class="master-detail-rail">
      {@render rail()}
    </div>
  {/if}
</section>
