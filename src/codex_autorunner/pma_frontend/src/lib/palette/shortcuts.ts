import type { ShortcutWhen } from './types';

export type ParsedCombo = {
  ctrl: boolean;
  meta: boolean;
  shift: boolean;
  alt: boolean;
  key: string;
};

export type KeyEventLike = {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  code?: string;
  target?: ElementLike | null;
};

export type ElementLike = {
  tagName: string;
  getAttribute: (name: string) => string | null;
};

export function parseCombo(spec: string): ParsedCombo {
  const parts = spec.toLowerCase().split('+').map((s) => s.trim());
  return {
    ctrl: parts.includes('ctrl') || parts.includes('control'),
    meta: parts.includes('meta') || parts.includes('cmd') || parts.includes('command'),
    shift: parts.includes('shift'),
    alt: parts.includes('alt') || parts.includes('option'),
    key: parts.find((p) => !['ctrl', 'control', 'meta', 'cmd', 'command', 'shift', 'alt', 'option'].includes(p)) ?? ''
  };
}

export function comboMatchesEvent(combo: ParsedCombo, event: KeyEventLike): boolean {
  const key = combo.key.toLowerCase();
  const eventKey = event.key.toLowerCase();

  if (key !== eventKey && key !== (event.code ?? '').toLowerCase()) return false;
  if (combo.ctrl !== (event.ctrlKey ?? false)) return false;
  if (combo.meta !== (event.metaKey ?? false)) return false;
  if (combo.shift !== (event.shiftKey ?? false)) return false;
  if (combo.alt !== (event.altKey ?? false)) return false;
  return true;
}

export function shortcutFiresWhen(when: ShortcutWhen, target: ElementLike | null | undefined): boolean {
  const isInput = isInputElement(target);
  if (when === 'always') return true;
  if (when === 'not-input') return !isInput;
  if (when === 'input-only') return isInput;
  return true;
}

export function isInputElement(element: ElementLike | null | undefined): boolean {
  if (!element) return false;
  const tag = element.tagName.toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (element.getAttribute('contenteditable') === 'true') return true;
  return false;
}

export function isModifierKey(event: KeyEventLike): boolean {
  return event.key === 'Control' || event.key === 'Meta' || event.key === 'Shift' || event.key === 'Alt';
}

export function elementFromDom(el: Element | null | undefined): ElementLike | null {
  if (!el) return null;
  return {
    tagName: el.tagName,
    getAttribute: (name: string) => el.getAttribute(name)
  };
}

export function eventFromDom(event: KeyboardEvent): KeyEventLike {
  return {
    key: event.key,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
    altKey: event.altKey,
    code: event.code,
    target: elementFromDom(event.target instanceof Element ? event.target : null)
  };
}
