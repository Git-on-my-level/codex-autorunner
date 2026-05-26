export type FilterChip = {
  key: string;
  label: string;
  count?: number | null;
  active?: boolean;
  onSelect: () => void;
  title?: string;
  ariaSelected?: boolean;
  className?: string;
};
