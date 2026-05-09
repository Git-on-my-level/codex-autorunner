/** Hub default palettes (also used when `system` tracks the OS). */
export const HUB_BASE_THEME_IDS = ['light', 'dark'] as const;
export type HubBaseThemeId = (typeof HUB_BASE_THEME_IDS)[number];

/** IDE- / terminal-style named palettes (`<html data-theme="…">`). */
export const THEME_PRESET_IDS = [
  'solarized-light',
  'solarized-dark',
  'dracula',
  'nord',
  'one-dark',
  'github-light',
  'github-dark'
] as const;
export type ThemePresetId = (typeof THEME_PRESET_IDS)[number];

export type ThemePreference = HubBaseThemeId | 'system' | ThemePresetId;

export const THEME_STORAGE_KEY = 'car.pma.theme';

const PREFERENCE_SET = new Set<string>(['light', 'dark', 'system', ...THEME_PRESET_IDS]);

export function isThemePreference(value: string): value is ThemePreference {
  return PREFERENCE_SET.has(value);
}

/** Normalize raw localStorage. Unknown values fall back to `system`. */
export function normalizeThemePreference(raw: string | null | undefined): ThemePreference {
  if (raw != null && isThemePreference(raw)) return raw;
  return 'system';
}

/** Value for `<html data-theme>` (never `system`). */
export type DataTheme = HubBaseThemeId | ThemePresetId;

export function resolveDataTheme(preference: ThemePreference, prefersDark: boolean): DataTheme {
  if (preference === 'system') return prefersDark ? 'dark' : 'light';
  return preference;
}

function readPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function readStoredThemePreference(): ThemePreference {
  if (typeof localStorage === 'undefined') return 'system';
  try {
    return normalizeThemePreference(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

/** Persist preference and apply `data-theme` on `document.documentElement`. */
export function applyThemePreference(preference: ThemePreference): void {
  if (typeof document === 'undefined') return;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    /* ignore quota / private mode */
  }
  const dataTheme = resolveDataTheme(preference, readPrefersDark());
  document.documentElement.setAttribute('data-theme', dataTheme);
}

let mediaQuery: MediaQueryList | null = null;
let onSchemeChange: (() => void) | null = null;

/** Re-resolve theme when OS appearance changes while preference is `system`. */
export function attachThemeSchemeListener(): void {
  if (typeof window === 'undefined' || !window.matchMedia) return;
  if (mediaQuery) return;
  mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
  onSchemeChange = () => {
    if (readStoredThemePreference() === 'system') {
      applyThemePreference('system');
    }
  };
  mediaQuery.addEventListener('change', onSchemeChange);
}

export function detachThemeSchemeListener(): void {
  if (mediaQuery && onSchemeChange) {
    mediaQuery.removeEventListener('change', onSchemeChange);
  }
  mediaQuery = null;
  onSchemeChange = null;
}
