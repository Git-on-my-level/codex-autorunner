import { describe, expect, it } from 'vitest';
import {
  isThemePreference,
  normalizeThemePreference,
  resolveDataTheme,
  THEME_STORAGE_KEY
} from './theme';

describe('theme', () => {
  it('exports a stable storage key', () => {
    expect(THEME_STORAGE_KEY).toBe('car.pma.theme');
  });

  it('normalizes localStorage values', () => {
    expect(normalizeThemePreference(null)).toBe('system');
    expect(normalizeThemePreference('')).toBe('system');
    expect(normalizeThemePreference('dark')).toBe('dark');
    expect(normalizeThemePreference('light')).toBe('light');
    expect(normalizeThemePreference('system')).toBe('system');
    expect(normalizeThemePreference('dracula')).toBe('dracula');
    expect(normalizeThemePreference('solarized-light')).toBe('solarized-light');
    expect(normalizeThemePreference('nope')).toBe('system');
  });

  it('validates theme preference strings', () => {
    expect(isThemePreference('github-dark')).toBe(true);
    expect(isThemePreference('one-dark')).toBe(true);
    expect(isThemePreference('monokai')).toBe(false);
  });

  it('resolves system preference from prefers-dark', () => {
    expect(resolveDataTheme('system', false)).toBe('light');
    expect(resolveDataTheme('system', true)).toBe('dark');
    expect(resolveDataTheme('light', true)).toBe('light');
    expect(resolveDataTheme('dark', false)).toBe('dark');
    expect(resolveDataTheme('dracula', false)).toBe('dracula');
    expect(resolveDataTheme('dracula', true)).toBe('dracula');
  });
});
