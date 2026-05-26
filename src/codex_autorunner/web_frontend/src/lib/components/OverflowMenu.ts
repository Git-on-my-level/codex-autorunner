export type OverflowMenuItem = {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  title?: string;
};
