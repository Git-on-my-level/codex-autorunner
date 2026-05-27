<script lang="ts">
  import { onMount, type Snippet } from 'svelte';
  import type { FilterChip } from './FilterRow';

  interface Props {
    items: FilterChip[];
    ariaLabel?: string;
    label?: string;
    rootClass?: string;
    role?: 'tablist';
    itemRole?: 'tab';
    trailing?: Snippet;
    maxRows?: number;
    dropdownPlaceholder?: string;
  }

  const {
    items,
    ariaLabel,
    label,
    rootClass = '',
    role,
    itemRole,
    trailing,
    maxRows = 2,
    dropdownPlaceholder = 'Filters'
  }: Props = $props();

  let containerEl = $state<HTMLDivElement | undefined>();
  let measureEl = $state<HTMLDivElement | undefined>();
  let dropdownEl = $state<HTMLElement | undefined>();
  let collapsed = $state(false);
  let dropdownOpen = $state(false);

  const activeItem = $derived(items.find((item) => item.active) ?? null);
  const rowClass = $derived(['filter-row', rootClass].filter(Boolean).join(' '));

  function recompute() {
    if (!measureEl) return;
    const chips = Array.from(measureEl.querySelectorAll<HTMLElement>('[data-filter-chip]'));
    if (chips.length === 0) {
      collapsed = false;
      dropdownOpen = false;
      return;
    }
    const tops = new Set<number>();
    for (const chip of chips) tops.add(chip.offsetTop);
    collapsed = tops.size > maxRows;
    if (!collapsed) dropdownOpen = false;
  }

  onMount(() => {
    recompute();
    if (!containerEl) return;
    const ro = new ResizeObserver(() => recompute());
    ro.observe(containerEl);
    return () => ro.disconnect();
  });

  $effect(() => {
    // Re-measure when visible chip content changes.
    items.map((i) => `${i.key}:${i.label}:${i.count ?? ''}`).join('|');
    queueMicrotask(recompute);
  });

  function handleClickOutside(event: MouseEvent) {
    if (!dropdownEl || !dropdownOpen) return;
    if (event.target instanceof Node && dropdownEl.contains(event.target)) return;
    dropdownOpen = false;
  }

  $effect(() => {
    if (!collapsed) return;
    window.addEventListener('mousedown', handleClickOutside);
    return () => window.removeEventListener('mousedown', handleClickOutside);
  });

  function selectAndClose(item: FilterChip) {
    item.onSelect();
    dropdownOpen = false;
  }

  function toggleDropdown(): void {
    dropdownOpen = !dropdownOpen;
  }
</script>

<div class="filter-row-container" bind:this={containerEl}>
  <div class="{rowClass} filter-row-measure" bind:this={measureEl} aria-hidden="true">
    {#if label}
      <span class="filter-row-label">{label}</span>
    {/if}
    {#each items as item (item.key)}
      <button class="chip" type="button" tabindex="-1" data-filter-chip>
        {item.label}
        {#if item.count !== undefined && item.count !== null}
          <span>{item.count}</span>
        {/if}
      </button>
    {/each}
  </div>

  {#if collapsed}
    <div class={rowClass} aria-label={ariaLabel}>
      {#if label}
        <span class="filter-row-label">{label}</span>
      {/if}
      <!-- Keep the collapsed menu button-driven; native details/summary can swallow option clicks
        when this absolute menu is layered over virtualized scroll content. -->
      <div class="filter-dropdown" class:open={dropdownOpen} bind:this={dropdownEl}>
        <button
          class="chip filter-dropdown-trigger"
          class:active={activeItem !== null}
          type="button"
          aria-haspopup="menu"
          aria-expanded={dropdownOpen}
          onclick={toggleDropdown}
        >
          <span class="filter-dropdown-label">{activeItem?.label ?? dropdownPlaceholder}</span>
          {#if activeItem && activeItem.count !== undefined && activeItem.count !== null}
            <span>{activeItem.count}</span>
          {/if}
          <span class="filter-dropdown-chevron" aria-hidden="true">▾</span>
        </button>
        {#if dropdownOpen}
          <div class="filter-dropdown-menu" role="menu" aria-label={ariaLabel}>
            {#each items as item (item.key)}
              <button
                class:active={item.active}
                class={`chip ${item.className ?? ''}`.trim()}
                type="button"
                role="menuitemradio"
                aria-checked={item.ariaSelected ?? item.active ?? false}
                title={item.title}
                onclick={() => selectAndClose(item)}
              >
                {item.label}
                {#if item.count !== undefined && item.count !== null}
                  <span>{item.count}</span>
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      </div>
      {#if trailing}{@render trailing()}{/if}
    </div>
  {:else}
    <div class={rowClass} {role} aria-label={ariaLabel}>
      {#if label}
        <span class="filter-row-label">{label}</span>
      {/if}
      {#each items as item (item.key)}
        <button
          class:active={item.active}
          class={`chip ${item.className ?? ''}`.trim()}
          type="button"
          role={itemRole}
          aria-selected={item.ariaSelected}
          title={item.title}
          onclick={item.onSelect}
        >
          {item.label}
          {#if item.count !== undefined && item.count !== null}
            <span>{item.count}</span>
          {/if}
        </button>
      {/each}
      {#if trailing}{@render trailing()}{/if}
    </div>
  {/if}
</div>

<style>
  .filter-row-container {
    position: relative;
    min-width: 0;
  }

  .filter-row-measure {
    position: absolute;
    inset: 0 0 auto 0;
    visibility: hidden;
    pointer-events: none;
    margin: 0;
  }

  .filter-dropdown {
    position: relative;
  }

  .filter-dropdown-chevron {
    font-size: 10px;
    opacity: 0.6;
    transition: transform 120ms ease;
  }

  .filter-dropdown.open .filter-dropdown-chevron {
    transform: rotate(180deg);
  }

  .filter-dropdown-menu {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 6px;
    min-width: 200px;
    max-width: 280px;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    box-shadow: var(--shadow-2);
  }

  .filter-dropdown-menu :global(.chip) {
    justify-content: space-between;
    width: 100%;
  }

  .filter-row-label {
    display: inline-flex;
    align-items: center;
    min-height: 26px;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
    white-space: nowrap;
  }
</style>
