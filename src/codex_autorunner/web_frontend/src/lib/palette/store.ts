import type { PaletteItem, PaletteSource, ShortcutBinding } from './types';
import { loadAllItems, filterItems, recordRecentAction, getRecentActions } from './sources';
import { createShortcutRegistry, buildStandardShortcuts, type ShortcutRegistry } from './registry';
import type { KeyEventLike } from './shortcuts';

export type PaletteStore = {
  open: boolean;
  query: string;
  items: PaletteItem[];
  activeIndex: number;
  sources: PaletteSource[];
  registry: ShortcutRegistry;
  toggle: () => void;
  openPalette: () => void;
  closePalette: () => void;
  setQuery: (query: string) => void;
  moveActive: (delta: number) => void;
  selectActive: () => void;
  selectItem: (item: PaletteItem) => void;
  refresh: () => void;
  updateSources: (sources: PaletteSource[]) => void;
  handleKeydown: (event: KeyboardEvent | KeyEventLike) => boolean;
  subscribe: (listener: () => void) => () => void;
  destroy: () => void;
};

export function createPaletteStore(
  sources: PaletteSource[],
  callbacks: {
    togglePalette?: () => void;
    newChat?: () => void;
    toggleSidebar?: () => void;
    toggleMemory?: () => void;
    focusComposer?: () => void;
    goBack?: () => void;
  },
  onNavigate?: (href: string) => void
): PaletteStore {
  const registry = createShortcutRegistry();
  let currentSources = sources;
  let allItems = loadAllItems(sources);
  let filteredItems = allItems;
  let isOpen = false;
  let query = '';
  let activeIdx = 0;
  let listeners: Array<() => void> = [];

  const standardShortcuts = buildStandardShortcuts({
    togglePalette: () => store.toggle(),
    newChat: () => callbacks.newChat?.(),
    toggleSidebar: () => callbacks.toggleSidebar?.(),
    toggleMemory: () => callbacks.toggleMemory?.(),
    focusComposer: () => callbacks.focusComposer?.(),
    goBack: () => callbacks.goBack?.()
  });
  for (const binding of standardShortcuts) registry.register(binding);

  function recompute(options?: { preserveActiveId?: boolean }): void {
    const activeId = options?.preserveActiveId ? filteredItems[activeIdx]?.id : undefined;
    allItems = loadAllItems(currentSources);
    filteredItems = filterItems(allItems, query);
    if (activeId) {
      const nextIdx = filteredItems.findIndex((item) => item.id === activeId);
      activeIdx = nextIdx >= 0 ? nextIdx : Math.min(activeIdx, Math.max(0, filteredItems.length - 1));
    } else if (activeIdx >= filteredItems.length) {
      activeIdx = Math.max(0, filteredItems.length - 1);
    }
  }

  function notify(): void {
    for (const listener of listeners) listener();
  }

  const store: PaletteStore = {
    get open() { return isOpen; },
    get query() { return query; },
    get items() { return filteredItems; },
    get activeIndex() { return activeIdx; },
    get sources() { return currentSources; },
    get registry() { return registry; },

    toggle() {
      if (isOpen) { store.closePalette(); } else { store.openPalette(); }
    },
    openPalette() {
      isOpen = true;
      query = '';
      activeIdx = 0;
      recompute();
      notify();
    },
    closePalette() {
      isOpen = false;
      query = '';
      notify();
    },
    setQuery(newQuery: string) {
      query = newQuery;
      activeIdx = 0;
      recompute();
      notify();
    },
    moveActive(delta: number) {
      if (filteredItems.length === 0) return;
      activeIdx = (activeIdx + delta + filteredItems.length) % filteredItems.length;
      notify();
    },
    selectActive() {
      if (filteredItems[activeIdx]) store.selectItem(filteredItems[activeIdx]);
    },
    selectItem(item: PaletteItem) {
      recordRecentAction(item);
      if (item.action.kind === 'navigate' && onNavigate) {
        onNavigate(item.action.href);
      } else if (item.action.kind === 'command') {
        item.action.handler();
      }
      store.closePalette();
    },
    refresh() {
      recompute({ preserveActiveId: true });
      notify();
    },
    updateSources(newSources: PaletteSource[]) {
      currentSources = newSources;
      recompute({ preserveActiveId: true });
      notify();
    },
    handleKeydown(event: KeyboardEvent | KeyEventLike) {
      const key = 'key' in event ? event.key : '';
      if (isOpen) {
        if (key === 'Escape') {
          if (typeof (event as KeyboardEvent).preventDefault === 'function') {
            (event as KeyboardEvent).preventDefault();
          }
          store.closePalette();
          return true;
        }
        if (key === 'ArrowDown') {
          if (typeof (event as KeyboardEvent).preventDefault === 'function') {
            (event as KeyboardEvent).preventDefault();
          }
          store.moveActive(1);
          return true;
        }
        if (key === 'ArrowUp') {
          if (typeof (event as KeyboardEvent).preventDefault === 'function') {
            (event as KeyboardEvent).preventDefault();
          }
          store.moveActive(-1);
          return true;
        }
        if (key === 'Enter') {
          if (typeof (event as KeyboardEvent).preventDefault === 'function') {
            (event as KeyboardEvent).preventDefault();
          }
          store.selectActive();
          return true;
        }
        return false;
      }
      return registry.handleKeydown(event);
    },
    subscribe(listener: () => void) {
      listeners.push(listener);
      return () => {
        listeners = listeners.filter((candidate) => candidate !== listener);
      };
    },
    destroy() {
      registry.destroy();
      listeners = [];
    }
  };

  return store;
}
