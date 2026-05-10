import { describe, expect, it } from 'vitest';
import { parseCombo, comboMatchesEvent, shortcutFiresWhen, isInputElement, isModifierKey, type KeyEventLike, type ElementLike } from './shortcuts';

function mockElement(tag: string, attrs?: Record<string, string>): ElementLike {
  const attributes = attrs ?? {};
  return {
    tagName: tag,
    getAttribute: (name: string) => attributes[name] ?? null
  };
}

describe('parseCombo', () => {
  it('parses a simple key', () => {
    const combo = parseCombo('k');
    expect(combo.key).toBe('k');
    expect(combo.ctrl).toBe(false);
    expect(combo.meta).toBe(false);
    expect(combo.shift).toBe(false);
    expect(combo.alt).toBe(false);
  });

  it('parses ctrl+k', () => {
    const combo = parseCombo('ctrl+k');
    expect(combo.key).toBe('k');
    expect(combo.ctrl).toBe(true);
    expect(combo.meta).toBe(false);
  });

  it('parses meta+k (treated as meta)', () => {
    const combo = parseCombo('meta+k');
    expect(combo.meta).toBe(true);
  });

  it('parses cmd+k as meta', () => {
    const combo = parseCombo('cmd+k');
    expect(combo.meta).toBe(true);
  });

  it('parses shift+enter', () => {
    const combo = parseCombo('shift+enter');
    expect(combo.key).toBe('enter');
    expect(combo.shift).toBe(true);
  });

  it('parses alt+arrowleft', () => {
    const combo = parseCombo('alt+arrowleft');
    expect(combo.key).toBe('arrowleft');
    expect(combo.alt).toBe(true);
  });

  it('is case-insensitive', () => {
    const combo = parseCombo('Ctrl+K');
    expect(combo.ctrl).toBe(true);
    expect(combo.key).toBe('k');
  });

  it('handles extra whitespace', () => {
    const combo = parseCombo(' ctrl + k ');
    expect(combo.ctrl).toBe(true);
    expect(combo.key).toBe('k');
  });
});

describe('comboMatchesEvent', () => {
  it('matches a simple key press', () => {
    const combo = parseCombo('k');
    const event: KeyEventLike = { key: 'k' };
    expect(comboMatchesEvent(combo, event)).toBe(true);
  });

  it('matches ctrl+k', () => {
    const combo = parseCombo('ctrl+k');
    const event: KeyEventLike = { key: 'k', ctrlKey: true };
    expect(comboMatchesEvent(combo, event)).toBe(true);
  });

  it('rejects when modifier is missing', () => {
    const combo = parseCombo('ctrl+k');
    const event: KeyEventLike = { key: 'k' };
    expect(comboMatchesEvent(combo, event)).toBe(false);
  });

  it('rejects when extra modifier is present', () => {
    const combo = parseCombo('ctrl+k');
    const event: KeyEventLike = { key: 'k', ctrlKey: true, shiftKey: true };
    expect(comboMatchesEvent(combo, event)).toBe(false);
  });

  it('matches meta+k', () => {
    const combo = parseCombo('meta+k');
    const event: KeyEventLike = { key: 'k', metaKey: true };
    expect(comboMatchesEvent(combo, event)).toBe(true);
  });

  it('matches alt+arrowleft', () => {
    const combo = parseCombo('alt+arrowleft');
    const event: KeyEventLike = { key: 'ArrowLeft', altKey: true };
    expect(comboMatchesEvent(combo, event)).toBe(true);
  });
});

describe('shortcutFiresWhen', () => {
  it('fires always regardless of element', () => {
    expect(shortcutFiresWhen('always', null)).toBe(true);
    expect(shortcutFiresWhen('always', mockElement('input'))).toBe(true);
  });

  it('fires not-input only when not in an input', () => {
    expect(shortcutFiresWhen('not-input', null)).toBe(true);
    expect(shortcutFiresWhen('not-input', mockElement('div'))).toBe(true);
    expect(shortcutFiresWhen('not-input', mockElement('input'))).toBe(false);
    expect(shortcutFiresWhen('not-input', mockElement('textarea'))).toBe(false);
  });

  it('fires input-only only when in an input', () => {
    expect(shortcutFiresWhen('input-only', null)).toBe(false);
    expect(shortcutFiresWhen('input-only', mockElement('div'))).toBe(false);
    expect(shortcutFiresWhen('input-only', mockElement('input'))).toBe(true);
  });
});

describe('isInputElement', () => {
  it('returns true for input elements', () => {
    expect(isInputElement(mockElement('input'))).toBe(true);
    expect(isInputElement(mockElement('textarea'))).toBe(true);
    expect(isInputElement(mockElement('select'))).toBe(true);
  });

  it('returns false for non-input elements', () => {
    expect(isInputElement(mockElement('div'))).toBe(false);
    expect(isInputElement(null)).toBe(false);
  });

  it('returns true for contenteditable elements', () => {
    const el = mockElement('div', { contenteditable: 'true' });
    expect(isInputElement(el)).toBe(true);
  });
});

describe('isModifierKey', () => {
  it('recognizes modifier keys', () => {
    expect(isModifierKey({ key: 'Control' })).toBe(true);
    expect(isModifierKey({ key: 'Meta' })).toBe(true);
    expect(isModifierKey({ key: 'Shift' })).toBe(true);
    expect(isModifierKey({ key: 'Alt' })).toBe(true);
  });

  it('does not match regular keys', () => {
    expect(isModifierKey({ key: 'k' })).toBe(false);
    expect(isModifierKey({ key: 'Enter' })).toBe(false);
  });
});
