<script lang="ts">
  import type { PaletteStore } from '$lib/palette/store';

  let { store }: { store: PaletteStore } = $props();
  let searchInput: HTMLInputElement | null = $state(null);

  $effect(() => {
    if (store.open) {
      queueMicrotask(() => searchInput?.focus());
    }
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (store.handleKeydown(event)) return;
  }

  function handleInputKeydown(event: KeyboardEvent): void {
    if (store.handleKeydown(event)) return;
  }
</script>

{#if store.open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="palette-backdrop" onclick={() => store.closePalette()} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="palette-panel"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      tabindex="-1"
      onclick={(event) => event.stopPropagation()}
      onkeydown={() => {}}
    >
      <div class="palette-search">
        <svg class="palette-search-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <circle cx="6.5" cy="6.5" r="5" fill="none" stroke="currentColor" stroke-width="1.4" />
          <line x1="10.5" y1="10.5" x2="14.5" y2="14.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
        </svg>
        <input
          bind:this={searchInput}
          class="palette-input"
          type="text"
          placeholder="Search commands, threads, scopes, tickets..."
          value={store.query}
          oninput={(event) => store.setQuery((event.target as HTMLInputElement).value)}
          onkeydown={handleInputKeydown}
          aria-label="Search palette items"
          autocomplete="off"
          spellcheck="false"
        />
      </div>
      <ul class="palette-list" role="listbox" aria-label="Palette results">
        {#each store.items as item, index (item.id)}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <li
            class={`palette-item ${index === store.activeIndex ? 'active' : ''}`}
            role="option"
            aria-selected={index === store.activeIndex}
            onclick={() => store.selectItem(item)}
            onpointerenter={() => { store.moveActive(index - store.activeIndex); }}
          >
            <span class="palette-item-label">{item.label}</span>
            <span class="palette-item-group">{item.group}</span>
          </li>
        {/each}
        {#if store.items.length === 0}
          <li class="palette-empty" role="presentation">
            <span>No results for "{store.query}"</span>
          </li>
        {/if}
      </ul>
      <div class="palette-footer" aria-hidden="true">
        <span class="palette-hint"><kbd>&uarr;</kbd><kbd>&darr;</kbd> navigate</span>
        <span class="palette-hint"><kbd>Enter</kbd> select</span>
        <span class="palette-hint"><kbd>Esc</kbd> close</span>
      </div>
    </div>
  </div>
{/if}

<style>
  .palette-backdrop {
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: var(--color-scrim);
    display: flex;
    justify-content: center;
    padding-top: 12vh;
  }

  .palette-panel {
    width: min(560px, calc(100vw - var(--space-8)));
    max-height: 60vh;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-3);
    box-shadow: var(--shadow-2);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .palette-search {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--color-border-subtle);
  }

  .palette-search-icon {
    flex-shrink: 0;
    color: var(--color-ink-faint);
  }

  .palette-input {
    flex: 1;
    border: none;
    background: transparent;
    font-size: var(--font-size-2);
    color: var(--color-ink);
    outline: none;
    font-family: inherit;
  }

  .palette-input::placeholder {
    color: var(--color-ink-faint);
  }

  .palette-list {
    list-style: none;
    margin: 0;
    padding: var(--space-1) 0;
    overflow-y: auto;
    flex: 1;
  }

  .palette-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-4);
    cursor: pointer;
    font-size: var(--font-size-1);
    color: var(--color-ink-soft);
    transition: background var(--transition-fast);
  }

  .palette-item.active {
    background: var(--color-accent-soft);
    color: var(--color-ink);
  }

  .palette-item:hover {
    background: var(--color-surface-muted);
  }

  .palette-item.active:hover {
    background: var(--color-accent-soft);
  }

  .palette-item-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }

  .palette-item-group {
    font-size: var(--font-size-0);
    color: var(--color-ink-faint);
    flex-shrink: 0;
    padding: 1px 6px;
    background: var(--color-surface-muted);
    border-radius: var(--radius-1);
  }

  .palette-empty {
    padding: var(--space-5) var(--space-4);
    text-align: center;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  .palette-footer {
    display: flex;
    gap: var(--space-4);
    padding: var(--space-2) var(--space-4);
    border-top: 1px solid var(--color-border-subtle);
    font-size: var(--font-size-0);
    color: var(--color-ink-faint);
  }

  .palette-hint {
    display: flex;
    align-items: center;
    gap: 3px;
  }

  .palette-hint kbd {
    display: inline-block;
    padding: 0 4px;
    border: 1px solid var(--color-border);
    border-radius: 3px;
    font-family: inherit;
    font-size: var(--font-size-0);
    line-height: 1.5;
    background: var(--color-surface-muted);
  }
</style>
