export type PaletteAction =
  | { kind: 'navigate'; href: string }
  | { kind: 'command'; handler: () => void };

export type PaletteItem = {
  id: string;
  label: string;
  group: string;
  keywords: string;
  glyph?: string | null;
  accent?: string | null;
  meta?: string | null;
  chip?: string | null;
  lastActivityAt?: string | null;
  action: PaletteAction;
};

export type PaletteSource = {
  group: string;
  priority: number;
  load: () => PaletteItem[];
};

export type ShortcutBinding = {
  id: string;
  label: string;
  keys: string;
  macKeys?: string;
  action: PaletteAction;
  when: ShortcutWhen;
};

export type ShortcutWhen =
  | 'always'
  | 'not-input'
  | 'input-only';

export type ShortcutDef = {
  id: string;
  label: string;
  keys: string;
  macKeys?: string;
  when?: ShortcutWhen;
};
