<script lang="ts">
  /**
   * Compact "more actions" menu. Renders as a 30x30 icon button (three-dot glyph,
   * per DESIGN.md `.icon-button` shape) that opens a small popover with each
   * `item` as a button. Use for clusters of rare / lifecycle / destructive
   * actions that don't earn a permanent inline button on the row.
   *
   * Pattern follows DropdownSelect for outside-click + escape behavior.
   */

  import { tick } from 'svelte';
  import { overflowMenuPosition } from './OverflowMenu';
  import type { OverflowMenuItem } from './OverflowMenu';

  let {
    items = [],
    ariaLabel = 'More actions',
    triggerTitle = 'More actions'
  }: {
    items?: OverflowMenuItem[];
    ariaLabel?: string;
    triggerTitle?: string;
  } = $props();

  let open = $state(false);
  let activeIndex = $state(0);
  let triggerEl = $state<HTMLButtonElement | null>(null);
  let panelEl = $state<HTMLDivElement | null>(null);
  let panelPortalParent: HTMLElement | null = null;
  let panelTop = $state(0);
  let panelLeft = $state(0);

  const selectableItems = $derived(items.filter((item) => !item.disabled));

  const panelStyle = $derived(`top: ${panelTop}px; left: ${panelLeft}px;`);

  function updatePanelPosition(): void {
    if (typeof window === 'undefined' || !triggerEl) return;
    const rect = triggerEl.getBoundingClientRect();
    const panelRect = panelEl?.getBoundingClientRect();
    const position = overflowMenuPosition({
      triggerRect: rect,
      panelWidth: panelRect?.width ?? 180,
      panelHeight: panelRect?.height ?? 0,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight
    });
    panelTop = position.top;
    panelLeft = position.left;
  }

  async function openPanel(): Promise<void> {
    if (selectableItems.length === 0) return;
    open = true;
    const firstEnabled = items.findIndex((item) => !item.disabled);
    activeIndex = firstEnabled >= 0 ? firstEnabled : 0;
    await tick();
    updatePanelPosition();
    panelEl?.focus();
  }

  function closePanel(): void {
    open = false;
  }

  function togglePanel(event: MouseEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (open) closePanel();
    else void openPanel();
  }

  function selectItem(item: OverflowMenuItem): void {
    if (item.disabled) return;
    closePanel();
    triggerEl?.focus();
    item.onSelect();
  }

  function moveActive(delta: number): void {
    if (items.length === 0) return;
    let next = activeIndex;
    for (let i = 0; i < items.length; i += 1) {
      next = (next + delta + items.length) % items.length;
      if (!items[next]?.disabled) break;
    }
    activeIndex = next;
  }

  function handleTriggerKeydown(event: KeyboardEvent): void {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      void openPanel();
    }
  }

  function handlePanelKeydown(event: KeyboardEvent): void {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveActive(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveActive(-1);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      const item = items[activeIndex];
      if (item) selectItem(item);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closePanel();
      triggerEl?.focus();
    }
  }

  function onWindowPointerDown(event: PointerEvent): void {
    if (!open) return;
    const target = event.target as Node | null;
    if (!target) return;
    if (triggerEl?.contains(target) || panelEl?.contains(target)) return;
    closePanel();
  }

  $effect(() => {
    if (!open || typeof window === 'undefined') return;
    updatePanelPosition();
    window.addEventListener('resize', updatePanelPosition);
    window.addEventListener('scroll', updatePanelPosition, true);
    return () => {
      window.removeEventListener('resize', updatePanelPosition);
      window.removeEventListener('scroll', updatePanelPosition, true);
    };
  });

  $effect(() => {
    if (!panelEl || typeof document === 'undefined') return;
    panelPortalParent = document.body;
    document.body.appendChild(panelEl);
    return () => {
      const parent = panelPortalParent;
      if (parent && panelEl && panelEl.parentNode === parent) {
        parent.removeChild(panelEl);
      }
    };
  });
</script>

<svelte:window onpointerdown={onWindowPointerDown} />

<span class="overflow-menu" class:open>
  <button
    bind:this={triggerEl}
    type="button"
    class="icon-button overflow-menu-trigger"
    class:open
    aria-label={ariaLabel}
    aria-haspopup="menu"
    aria-expanded={open}
    title={triggerTitle}
    disabled={items.length === 0}
    onclick={togglePanel}
    onkeydown={handleTriggerKeydown}
  >
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="5" cy="12" r="1.5" />
      <circle cx="12" cy="12" r="1.5" />
      <circle cx="19" cy="12" r="1.5" />
    </svg>
  </button>

  {#if open}
    <div
      bind:this={panelEl}
      class="overflow-menu-panel"
      role="menu"
      aria-label={ariaLabel}
      tabindex="-1"
      style={panelStyle}
      onkeydown={handlePanelKeydown}
    >
      {#each items as item, idx (item.label)}
        <button
          type="button"
          role="menuitem"
          class="overflow-menu-item"
          class:danger={item.danger}
          data-active={idx === activeIndex}
          disabled={item.disabled}
          aria-label={item.ariaLabel ?? item.label}
          title={item.title}
          onmouseenter={() => (activeIndex = idx)}
          onclick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            selectItem(item);
          }}
        >
          {item.label}
        </button>
      {/each}
    </div>
  {/if}
</span>

<style>
  .overflow-menu {
    position: relative;
    display: inline-flex;
  }

  .overflow-menu-trigger {
    width: 30px;
    height: 30px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--color-ink-faint);
    border-radius: 6px;
    cursor: pointer;
    transition:
      background-color var(--transition-fast),
      color var(--transition-fast);
  }

  .overflow-menu-trigger:hover,
  .overflow-menu-trigger.open {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .overflow-menu-trigger:focus-visible {
    outline: none;
    box-shadow: var(--shadow-focus);
  }

  .overflow-menu-trigger:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .overflow-menu-trigger svg {
    width: 16px;
    height: 16px;
    fill: currentColor;
  }

  .overflow-menu-panel {
    position: fixed;
    z-index: 40;
    display: flex;
    flex-direction: column;
    min-width: min(180px, calc(100vw - 16px));
    max-width: calc(100vw - 16px);
    max-height: calc(100vh - 16px);
    overflow-y: auto;
    padding: 4px;
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    box-shadow: var(--shadow-2);
  }

  .overflow-menu-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 6px 10px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--color-ink-soft);
    font: inherit;
    font-size: var(--font-size-1);
    text-align: left;
    cursor: pointer;
    white-space: nowrap;
  }

  .overflow-menu-item[data-active='true'],
  .overflow-menu-item:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .overflow-menu-item.danger {
    color: var(--color-danger);
  }

  .overflow-menu-item.danger[data-active='true'],
  .overflow-menu-item.danger:hover {
    background: var(--color-danger-soft);
    color: var(--color-danger);
  }

  .overflow-menu-item:disabled {
    cursor: not-allowed;
    opacity: 0.45;
    background: transparent;
    color: var(--color-ink-faint);
  }
</style>
