import { describe, expect, it } from 'vitest';
import { overflowMenuPosition } from './OverflowMenu';

describe('overflowMenuPosition', () => {
  it('opens below the trigger when the panel fits', () => {
    expect(
      overflowMenuPosition({
        triggerRect: { top: 100, bottom: 130, right: 420 },
        panelWidth: 180,
        panelHeight: 96,
        viewportWidth: 800,
        viewportHeight: 600
      })
    ).toEqual({ top: 134, left: 240 });
  });

  it('flips above the trigger near the viewport bottom', () => {
    expect(
      overflowMenuPosition({
        triggerRect: { top: 540, bottom: 570, right: 760 },
        panelWidth: 180,
        panelHeight: 120,
        viewportWidth: 800,
        viewportHeight: 600
      })
    ).toEqual({ top: 416, left: 580 });
  });

  it('clamps horizontally for triggers near the viewport left edge', () => {
    expect(
      overflowMenuPosition({
        triggerRect: { top: 100, bottom: 130, right: 40 },
        panelWidth: 180,
        panelHeight: 96,
        viewportWidth: 800,
        viewportHeight: 600
      }).left
    ).toBe(8);
  });
});
