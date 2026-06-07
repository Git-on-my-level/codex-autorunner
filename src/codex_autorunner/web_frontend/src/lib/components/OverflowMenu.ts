export type OverflowMenuItem = {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  title?: string;
};

type OverflowMenuRect = Pick<DOMRect, 'top' | 'bottom' | 'right'>;

export type OverflowMenuPositionInput = {
  triggerRect: OverflowMenuRect;
  panelWidth: number;
  panelHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  margin?: number;
  gap?: number;
};

export type OverflowMenuPosition = {
  top: number;
  left: number;
};

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

export function overflowMenuPosition({
  triggerRect,
  panelWidth,
  panelHeight,
  viewportWidth,
  viewportHeight,
  margin = 8,
  gap = 4
}: OverflowMenuPositionInput): OverflowMenuPosition {
  const maxLeft = viewportWidth - panelWidth - margin;
  const left = clamp(triggerRect.right - panelWidth, margin, maxLeft);
  const belowTop = triggerRect.bottom + gap;
  const aboveTop = triggerRect.top - panelHeight - gap;
  const top =
    belowTop + panelHeight <= viewportHeight - margin
      ? belowTop
      : aboveTop >= margin
        ? aboveTop
        : clamp(belowTop, margin, viewportHeight - panelHeight - margin);
  return { top, left };
}
