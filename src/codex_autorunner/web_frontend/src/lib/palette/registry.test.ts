import { describe, expect, it, beforeEach } from 'vitest';
import { createShortcutRegistry, createBinding, buildStandardShortcuts } from './registry';
import type { KeyEventLike } from './shortcuts';

describe('createShortcutRegistry', () => {
  let registry: ReturnType<typeof createShortcutRegistry>;
  let fired: string[];

  beforeEach(() => {
    registry = createShortcutRegistry();
    fired = [];
  });

  it('fires registered shortcut on matching keydown', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('test') }
    ));
    const event: KeyEventLike = { key: 'k', ctrlKey: true };
    const handled = registry.handleKeydown(event);
    expect(handled).toBe(true);
    expect(fired).toEqual(['test']);
  });

  it('does not fire when modifier is missing', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('test') }
    ));
    const event: KeyEventLike = { key: 'k' };
    const handled = registry.handleKeydown(event);
    expect(handled).toBe(false);
    expect(fired).toEqual([]);
  });

  it('does not fire when input is focused and when is not-input', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: '/' },
      { kind: 'command', handler: () => fired.push('test') },
      { when: 'not-input' }
    ));
    const event: KeyEventLike = {
      key: '/',
      target: { tagName: 'INPUT', getAttribute: () => null }
    };
    const handled = registry.handleKeydown(event);
    expect(handled).toBe(false);
    expect(fired).toEqual([]);
  });

  it('fires always shortcuts even when input is focused', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('test') },
      { when: 'always' }
    ));
    const event: KeyEventLike = {
      key: 'k',
      ctrlKey: true,
      target: { tagName: 'INPUT', getAttribute: () => null }
    };
    const handled = registry.handleKeydown(event);
    expect(handled).toBe(true);
    expect(fired).toEqual(['test']);
  });

  it('replaces binding with same id', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Old', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('old') }
    ));
    registry.register(createBinding(
      { id: 'test', label: 'New', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('new') }
    ));
    expect(registry.bindings()).toHaveLength(1);
    const event: KeyEventLike = { key: 'k', ctrlKey: true };
    registry.handleKeydown(event);
    expect(fired).toEqual(['new']);
  });

  it('unregister removes binding', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: 'ctrl+k' },
      { kind: 'command', handler: () => fired.push('test') }
    ));
    registry.unregister('test');
    expect(registry.bindings()).toHaveLength(0);
    const event: KeyEventLike = { key: 'k', ctrlKey: true };
    registry.handleKeydown(event);
    expect(fired).toEqual([]);
  });

  it('destroy clears all bindings', () => {
    registry.register(createBinding(
      { id: 'test', label: 'Test', keys: 'ctrl+k' },
      { kind: 'command', handler: () => {} }
    ));
    registry.destroy();
    expect(registry.bindings()).toHaveLength(0);
  });
});

describe('buildStandardShortcuts', () => {
  it('returns six standard shortcuts', () => {
    const shortcuts = buildStandardShortcuts({
      togglePalette: () => {},
      newChat: () => {},
      toggleSidebar: () => {},
      toggleMemory: () => {},
      focusComposer: () => {},
      goBack: () => {}
    });
    expect(shortcuts).toHaveLength(6);
    expect(shortcuts.map((s) => s.id)).toEqual([
      'palette', 'new-chat', 'toggle-sidebar', 'toggle-memory', 'focus-composer', 'go-back'
    ]);
  });

  it('palette shortcut fires always', () => {
    const shortcuts = buildStandardShortcuts({
      togglePalette: () => {},
      newChat: () => {},
      toggleSidebar: () => {},
      toggleMemory: () => {},
      focusComposer: () => {},
      goBack: () => {}
    });
    const palette = shortcuts.find((s) => s.id === 'palette');
    expect(palette?.when).toBe('always');
  });
});
