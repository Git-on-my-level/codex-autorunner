import { describe, expect, it, vi } from 'vitest';
import { createDocumentStreamVisibilityPolicy, type VisibilityDocumentAdapter } from './streamVisibilityPolicy';

class FakeDocumentVisibility implements VisibilityDocumentAdapter {
  visibilityState: DocumentVisibilityState = 'visible';
  private listeners = new Set<() => void>();

  addEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.add(listener);
  }

  removeEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.delete(listener);
  }

  setVisibility(visibilityState: DocumentVisibilityState): void {
    this.visibilityState = visibilityState;
    this.listeners.forEach((listener) => listener());
  }
}

describe('stream visibility policy', () => {
  it('adapts document visibility changes without exposing document to stream owners', () => {
    const documentAdapter = new FakeDocumentVisibility();
    const policy = createDocumentStreamVisibilityPolicy(documentAdapter);
    const listener = vi.fn();

    const unsubscribe = policy.subscribe(listener);
    expect(policy.suspendWhenHidden).toBe(true);
    expect(policy.isVisible()).toBe(true);

    documentAdapter.setVisibility('hidden');
    expect(policy.isVisible()).toBe(false);
    expect(listener).toHaveBeenLastCalledWith(false);

    documentAdapter.setVisibility('visible');
    expect(listener).toHaveBeenLastCalledWith(true);

    unsubscribe();
    documentAdapter.setVisibility('hidden');
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
