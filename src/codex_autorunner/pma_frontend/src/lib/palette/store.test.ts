import { describe, expect, it, beforeEach } from 'vitest';
import { createPaletteStore } from './store';
import { clearRecentActions } from './sources';
import type { KeyEventLike } from './shortcuts';

beforeEach(() => {
  clearRecentActions();
});

function makeStore(callbacks?: Record<string, () => void>) {
  return createPaletteStore(
    [],
    {
      togglePalette: callbacks?.togglePalette ?? (() => {}),
      newChat: callbacks?.newChat ?? (() => {}),
      toggleSidebar: callbacks?.toggleSidebar ?? (() => {}),
      toggleMemory: callbacks?.toggleMemory ?? (() => {}),
      focusComposer: callbacks?.focusComposer ?? (() => {}),
      goBack: callbacks?.goBack ?? (() => {})
    },
    () => {}
  );
}

describe('createPaletteStore', () => {
  it('starts closed with empty query', () => {
    const store = makeStore();
    expect(store.open).toBe(false);
    expect(store.query).toBe('');
    expect(store.items).toEqual([]);
    expect(store.activeIndex).toBe(0);
  });

  it('openPalette sets open to true', () => {
    const store = makeStore();
    store.openPalette();
    expect(store.open).toBe(true);
    expect(store.query).toBe('');
  });

  it('closePalette resets state', () => {
    const store = makeStore();
    store.openPalette();
    store.setQuery('test');
    store.closePalette();
    expect(store.open).toBe(false);
    expect(store.query).toBe('');
  });

  it('toggle opens and closes', () => {
    const store = makeStore();
    store.toggle();
    expect(store.open).toBe(true);
    store.toggle();
    expect(store.open).toBe(false);
  });

  it('setQuery updates query and resets activeIndex', () => {
    const store = makeStore();
    store.openPalette();
    store.setQuery('hello');
    expect(store.query).toBe('hello');
    expect(store.activeIndex).toBe(0);
  });

  it('moveActive wraps around', () => {
    const store = makeStore();
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } },
        { id: 'b', label: 'B', group: 'G', keywords: '', action: { kind: 'navigate', href: '/b' } }
      ]
    }]);
    store.openPalette();
    expect(store.activeIndex).toBe(0);
    store.moveActive(1);
    expect(store.activeIndex).toBe(1);
    store.moveActive(1);
    expect(store.activeIndex).toBe(0);
    store.moveActive(-1);
    expect(store.activeIndex).toBe(1);
  });

  it('selectItem closes palette and records action', () => {
    const navigated: string[] = [];
    const store = createPaletteStore([], {}, (href) => navigated.push(href));
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } }
      ]
    }]);
    store.openPalette();
    store.selectItem({
      id: 'a',
      label: 'A',
      group: 'G',
      keywords: '',
      action: { kind: 'navigate', href: '/a' }
    });
    expect(store.open).toBe(false);
    expect(navigated).toEqual(['/a']);
  });

  it('handleKeydown Escape closes palette', () => {
    const store = makeStore();
    store.openPalette();
    const event: KeyEventLike = { key: 'Escape' };
    const handled = store.handleKeydown(event);
    expect(handled).toBe(true);
    expect(store.open).toBe(false);
  });

  it('handleKeydown ArrowDown moves active', () => {
    const store = makeStore();
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } },
        { id: 'b', label: 'B', group: 'G', keywords: '', action: { kind: 'navigate', href: '/b' } }
      ]
    }]);
    store.openPalette();
    const event: KeyEventLike = { key: 'ArrowDown' };
    store.handleKeydown(event);
    expect(store.activeIndex).toBe(1);
  });

  it('handleKeydown ArrowUp moves active', () => {
    const store = makeStore();
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } },
        { id: 'b', label: 'B', group: 'G', keywords: '', action: { kind: 'navigate', href: '/b' } }
      ]
    }]);
    store.openPalette();
    store.moveActive(1);
    const event: KeyEventLike = { key: 'ArrowUp' };
    store.handleKeydown(event);
    expect(store.activeIndex).toBe(0);
  });

  it('handleKeydown Enter selects active item', () => {
    const navigated: string[] = [];
    const store = createPaletteStore([], {}, (href) => navigated.push(href));
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } }
      ]
    }]);
    store.openPalette();
    const event: KeyEventLike = { key: 'Enter' };
    store.handleKeydown(event);
    expect(store.open).toBe(false);
    expect(navigated).toEqual(['/a']);
  });

  it('handleKeydown Ctrl+K opens palette when closed', () => {
    const store = makeStore();
    const event: KeyEventLike = { key: 'k', ctrlKey: true };
    const handled = store.handleKeydown(event);
    expect(handled).toBe(true);
    expect(store.open).toBe(true);
  });

  it('handleKeydown does not intercept unknown keys when palette is closed', () => {
    const store = makeStore();
    const event: KeyEventLike = { key: 'x' };
    const handled = store.handleKeydown(event);
    expect(handled).toBe(false);
  });

  it('updateSources refreshes items', () => {
    const store = makeStore();
    store.updateSources([{
      group: 'Test',
      priority: 0,
      load: () => [
        { id: 'a', label: 'A', group: 'G', keywords: '', action: { kind: 'navigate', href: '/a' } }
      ]
    }]);
    store.openPalette();
    expect(store.items).toHaveLength(1);
  });

  it('destroy cleans up', () => {
    const store = makeStore();
    store.destroy();
    expect(store.registry.bindings()).toHaveLength(0);
  });
});
