<script lang="ts" generics="T">
  import { onMount, tick, type Snippet } from 'svelte';

  let {
    items,
    children,
    key,
    estimatedItemSize = 72,
    overscan = 8,
    initialCount = 40,
    ariaLabel = 'Virtualized list',
    class: className = '',
    itemClass = ''
  }: {
    items: T[];
    children: Snippet<[T, number]>;
    key?: (item: T, index: number) => string;
    estimatedItemSize?: number;
    overscan?: number;
    initialCount?: number;
    ariaLabel?: string;
    class?: string;
    itemClass?: string;
  } = $props();

  let viewport: HTMLDivElement | null = $state(null);
  let mounted = $state(false);
  let scrollTop = $state(0);
  let viewportHeight = $state(0);

  const safeItemSize = $derived(Math.max(1, estimatedItemSize));
  const startIndex = $derived(
    mounted ? Math.max(0, Math.floor(scrollTop / safeItemSize) - overscan) : 0
  );
  const visibleCapacity = $derived(
    mounted ? Math.ceil(viewportHeight / safeItemSize) + overscan * 2 : initialCount
  );
  const endIndex = $derived(Math.min(items.length, startIndex + Math.max(1, visibleCapacity)));
  const visibleItems = $derived(items.slice(startIndex, endIndex));
  const totalHeight = $derived(items.length * safeItemSize);
  const offsetY = $derived(startIndex * safeItemSize);
  const rangeLabel = $derived(
    items.length === 0
      ? '0 items'
      : `${startIndex + 1}-${endIndex} of ${items.length} items`
  );

  function updateMeasurements(): void {
    if (!viewport) return;
    scrollTop = viewport.scrollTop;
    viewportHeight = viewport.clientHeight;
  }

  function handleScroll(): void {
    updateMeasurements();
  }

  onMount(() => {
    mounted = true;
    void tick().then(updateMeasurements);
    const observer = new ResizeObserver(updateMeasurements);
    if (viewport) observer.observe(viewport);
    return () => observer.disconnect();
  });
</script>

<div
  bind:this={viewport}
  class={`virtual-list ${className}`}
  role="list"
  aria-label={`${ariaLabel}, ${items.length} total`}
  aria-describedby={items.length > initialCount ? `virtual-list-count-${ariaLabel.replace(/\W+/g, '-').toLowerCase()}` : undefined}
  onscroll={handleScroll}
>
  {#if items.length > initialCount}
    <span id={`virtual-list-count-${ariaLabel.replace(/\W+/g, '-').toLowerCase()}`} class="sr-only">
      Showing {rangeLabel}; more rows load as you scroll.
    </span>
  {/if}
  <div class="virtual-list-spacer" style:height={`${totalHeight}px`}>
    <div class="virtual-list-window" style:transform={`translateY(${offsetY}px)`}>
      {#each visibleItems as item, localIndex (key ? key(item, startIndex + localIndex) : startIndex + localIndex)}
        <div class={`virtual-list-item ${itemClass}`} role="presentation">
          {@render children(item, startIndex + localIndex)}
        </div>
      {/each}
    </div>
  </div>
</div>

<style>
  .virtual-list {
    flex: 1 1 auto;
    min-height: 0;
    min-width: 0;
    overflow: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }

  .virtual-list-spacer {
    position: relative;
    min-width: 100%;
  }

  .virtual-list-window {
    position: absolute;
    inset: 0 0 auto 0;
    display: grid;
    gap: var(--virtual-list-gap, var(--space-2));
    will-change: transform;
  }

  .virtual-list-item {
    min-width: 0;
    min-height: var(--virtual-list-item-min-height, 0);
  }
</style>
