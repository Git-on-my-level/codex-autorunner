import type { ShortcutBinding, ShortcutDef, PaletteAction } from './types';
import { parseCombo, comboMatchesEvent, shortcutFiresWhen, isModifierKey, elementFromDom, eventFromDom, type ParsedCombo, type KeyEventLike } from './shortcuts';

export type ShortcutRegistry = {
  register: (binding: ShortcutBinding) => void;
  unregister: (id: string) => void;
  bindings: () => ShortcutBinding[];
  handleKeydown: (event: KeyboardEvent | KeyEventLike) => boolean;
  destroy: () => void;
};

export function createShortcutRegistry(): ShortcutRegistry {
  let bindings: ShortcutBinding[] = [];

  function register(binding: ShortcutBinding): void {
    bindings = bindings.filter((b) => b.id !== binding.id);
    bindings.push(binding);
  }

  function unregister(id: string): void {
    bindings = bindings.filter((b) => b.id !== id);
  }

  function normalizeKeyEvent(event: KeyboardEvent | KeyEventLike): KeyEventLike {
    if ('bubbles' in event && typeof (event as KeyboardEvent).preventDefault === 'function') {
      return eventFromDom(event as KeyboardEvent);
    }
    return event as KeyEventLike;
  }

  function handleKeydown(event: KeyboardEvent | KeyEventLike): boolean {
    const ke = normalizeKeyEvent(event);
    if (isModifierKey(ke)) return false;
    for (const binding of bindings) {
      const combo = parsePlatformCombo(binding);
      if (!comboMatchesEvent(combo, ke)) continue;
      if (!shortcutFiresWhen(binding.when, ke.target)) continue;
      if (typeof (event as KeyboardEvent).preventDefault === 'function') {
        (event as KeyboardEvent).preventDefault();
      }
      executeAction(binding.action);
      return true;
    }
    return false;
  }

  return {
    register,
    unregister,
    bindings: () => [...bindings],
    handleKeydown,
    destroy: () => {
      bindings = [];
    }
  };
}

export function createBinding(
  def: ShortcutDef,
  action: PaletteAction,
  overrides?: Partial<{ when: ShortcutBinding['when'] }>
): ShortcutBinding {
  return {
    id: def.id,
    label: def.label,
    keys: def.keys,
    macKeys: def.macKeys,
    action,
    when: overrides?.when ?? 'not-input'
  };
}

export function buildStandardShortcuts(
  callbacks: {
    togglePalette: () => void;
    newChat: () => void;
    toggleSidebar: () => void;
    toggleMemory: () => void;
    focusComposer: () => void;
    goBack: () => void;
  }
): ShortcutBinding[] {
  return [
    createBinding(
      { id: 'palette', label: 'Command palette', keys: 'ctrl+k', macKeys: 'meta+k' },
      { kind: 'command', handler: callbacks.togglePalette },
      { when: 'always' }
    ),
    createBinding(
      { id: 'new-chat', label: 'New chat', keys: 'ctrl+n', macKeys: 'meta+n' },
      { kind: 'command', handler: callbacks.newChat }
    ),
    createBinding(
      { id: 'toggle-sidebar', label: 'Toggle sidebar', keys: 'ctrl+\\', macKeys: 'meta+\\' },
      { kind: 'command', handler: callbacks.toggleSidebar },
      { when: 'always' }
    ),
    createBinding(
      { id: 'toggle-memory', label: 'Toggle memory rail', keys: 'ctrl+m', macKeys: 'meta+m' },
      { kind: 'command', handler: callbacks.toggleMemory }
    ),
    createBinding(
      { id: 'focus-composer', label: 'Focus composer', keys: '/' },
      { kind: 'command', handler: callbacks.focusComposer }
    ),
    createBinding(
      { id: 'go-back', label: 'Go back', keys: 'alt+arrowleft' },
      { kind: 'command', handler: callbacks.goBack },
      { when: 'always' }
    )
  ];
}

function parsePlatformCombo(binding: ShortcutBinding): ParsedCombo {
  const isMac = typeof navigator !== 'undefined' && /Mac|iPod|iPhone|iPad/.test(navigator.userAgent);
  const spec = isMac && binding.macKeys ? binding.macKeys : binding.keys;
  return parseCombo(spec);
}

function executeAction(action: PaletteAction): void {
  if (action.kind === 'command') {
    action.handler();
  }
}
