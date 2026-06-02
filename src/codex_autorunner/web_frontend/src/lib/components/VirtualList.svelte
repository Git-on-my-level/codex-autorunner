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
    itemClass = '',
    scrollable = true,
    onEndReached,
    onScrollState,
    onReady,
    bottomThresholdPx = 32,
    preserveBottomOnResize = false
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
    scrollable?: boolean;
    onEndReached?: () => void;
    onScrollState?: (state: { atBottom: boolean; distanceFromBottom: number }) => void;
    onReady?: (api: { scrollToBottom: (behavior?: ScrollBehavior) => void }) => void;
    bottomThresholdPx?: number;
    preserveBottomOnResize?: boolean;
  } = $props();

  let viewport: HTMLDivElement | null = $state(null);
  let windowNode: HTMLDivElement | null = $state(null);
  let mounted = $state(false);
  let scrollTop = $state(0);
  let viewportHeight = $state(0);
  let measuredHeights = $state<Record<string, number>>({});
  let rowGap = $state(0);
  let lastAtBottom = true;

  const safeItemSize = $derived(Math.max(1, estimatedItemSize));
  const itemKeys = $derived(items.map((item, index) => key ? key(item, index) : String(index)));
  const itemOffsets = $derived.by(() => {
    const offsets = [0];
    for (let index = 0; index < items.length; index += 1) {
      const height = measuredHeights[itemKeys[index]] ?? safeItemSize;
      offsets.push(offsets[index] + height + (index < items.length - 1 ? rowGap : 0));
    }
    return offsets;
  });
  const totalHeight = $derived(itemOffsets.at(-1) ?? 0);
  const overscanPx = $derived(safeItemSize * overscan);
  const startIndex = $derived.by(() => {
    if (!mounted) return 0;
    return Math.max(0, findOffsetIndex(itemOffsets, Math.max(0, scrollTop - overscanPx)));
  });
  const endIndex = $derived.by(() => {
    if (!scrollable) return items.length;
    if (!mounted) return Math.min(items.length, Math.max(1, initialCount));
    const target = scrollTop + viewportHeight + overscanPx;
    return Math.min(items.length, Math.max(startIndex + 1, findOffsetIndex(itemOffsets, target) + 1));
  });
  const canScroll = $derived(scrollable && totalHeight > viewportHeight + 1);
  const visibleItems = $derived(items.slice(startIndex, endIndex));
  const offsetY = $derived(itemOffsets[startIndex] ?? 0);
  const itemUpdateToken = $derived(itemKeys.join('\u0000'));
  const rangeLabel = $derived(
    items.length === 0
      ? '0 items'
      : `${startIndex + 1}-${endIndex} of ${items.length} items`
  );

  function findOffsetIndex(offsets: number[], target: number): number {
    let low = 0;
    let high = Math.max(0, offsets.length - 2);
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      const next = offsets[mid + 1] ?? Number.POSITIVE_INFINITY;
      if (next <= target) low = mid + 1;
      else if ((offsets[mid] ?? 0) > target) high = mid - 1;
      else return mid;
    }
    return Math.max(0, Math.min(offsets.length - 2, low));
  }

  function checkEndReached(): void {
    if (!onEndReached || !viewport || !scrollable) return;
    const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    if (distanceFromBottom <= safeItemSize * 4) onEndReached();
  }

  function distanceFromBottom(): number {
    if (!viewport || !scrollable) return 0;
    return Math.max(
      0,
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
    );
  }

  function reportScrollState(): void {
    if (!viewport) return;
    if (!scrollable) {
      lastAtBottom = true;
      onScrollState?.({ atBottom: true, distanceFromBottom: 0 });
      return;
    }
    const distance = distanceFromBottom();
    lastAtBottom = distance <= bottomThresholdPx;
    onScrollState?.({ atBottom: lastAtBottom, distanceFromBottom: distance });
  }

  function isNearBottom(): boolean {
    return distanceFromBottom() <= bottomThresholdPx;
  }

  function updateMeasurements(): void {
    if (!viewport) return;
    scrollTop = viewport.scrollTop;
    viewportHeight = viewport.clientHeight;
    if (windowNode) {
      const gap = Number.parseFloat(getComputedStyle(windowNode).rowGap);
      rowGap = Number.isFinite(gap) ? gap : 0;
    }
    checkEndReached();
    reportScrollState();
  }

  function scrollToBottom(behavior: ScrollBehavior = 'smooth'): void {
    if (!viewport) return;
    viewport.scrollTo({ top: viewport.scrollHeight, behavior });
  }

  async function preserveBottomIfNeeded(wasAtBottom: boolean): Promise<void> {
    if (!preserveBottomOnResize || !wasAtBottom || !viewport || !scrollable) return;
    await tick();
    if (!viewport) return;
    viewport.scrollTop = viewport.scrollHeight;
    updateMeasurements();
  }

  function handleScroll(): void {
    updateMeasurements();
  }

  function handleViewportResize(): void {
    const wasAtBottom = lastAtBottom;
    if (preserveBottomOnResize && wasAtBottom) {
      void preserveBottomIfNeeded(wasAtBottom);
      return;
    }
    updateMeasurements();
  }

  $effect(() => {
    void items;
    void itemUpdateToken;
    void items.length;
    void totalHeight;
    if (!mounted) return;
    void tick().then(updateMeasurements);
  });

  function measureItem(node: HTMLElement, itemKey: string) {
    let currentKey = itemKey;
    const record = () => {
      const height = node.offsetHeight;
      if (!Number.isFinite(height) || height <= 0 || measuredHeights[currentKey] === height) return;
      const wasAtBottom = lastAtBottom || isNearBottom();
      measuredHeights = { ...measuredHeights, [currentKey]: height };
      void preserveBottomIfNeeded(wasAtBottom);
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(record);
    observer?.observe(node);
    record();
    return {
      update(nextKey: string) {
        currentKey = nextKey;
        record();
      },
      destroy() {
        observer?.disconnect();
      }
    };
  }

  onMount(() => {
    mounted = true;
    void tick().then(updateMeasurements);
    const observer = new ResizeObserver(handleViewportResize);
    if (viewport) observer.observe(viewport);
    onReady?.({ scrollToBottom });
    return () => observer.disconnect();
  });
</script>

<div
  bind:this={viewport}
  class={`virtual-list ${scrollable ? '' : 'non-scrollable'} ${canScroll ? 'can-scroll' : ''} ${className}`}
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
    <div bind:this={windowNode} class="virtual-list-window" style:transform={`translateY(${offsetY}px)`}>
      {#each visibleItems as item, localIndex (key ? key(item, startIndex + localIndex) : startIndex + localIndex)}
        {@const itemKey = itemKeys[startIndex + localIndex]}
        <div use:measureItem={itemKey} class={`virtual-list-item ${itemClass}`} role="presentation">
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
    overscroll-behavior: auto;
    scrollbar-gutter: stable;
  }

  .virtual-list.can-scroll {
    overscroll-behavior: contain;
  }

  .virtual-list.non-scrollable {
    flex: 0 0 auto;
    overflow: visible;
    overscroll-behavior: auto;
    scrollbar-gutter: auto;
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
