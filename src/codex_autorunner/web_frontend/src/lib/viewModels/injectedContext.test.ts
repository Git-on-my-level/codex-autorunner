import { describe, expect, it } from 'vitest';
import { stripInjectedContextBlocks } from './injectedContext';

describe('stripInjectedContextBlocks', () => {
  it('removes a single injected block and trims', () => {
    const text =
      '<injected context>\nYou are operating inside a Codex Autorunner (CAR) managed repo.\n</injected context>\n\nFix the UI freeze';
    expect(stripInjectedContextBlocks(text)).toBe('Fix the UI freeze');
  });

  it('removes multiple blocks', () => {
    const text =
      '<injected context>\nA\n</injected context>\n\nUser line\n\n<injected context>\nB\n</injected context>';
    expect(stripInjectedContextBlocks(text)).toBe('User line');
  });

  it('is case-insensitive on tags', () => {
    const text = '<Injected Context>\nhint\n</Injected Context>\n\nHello';
    expect(stripInjectedContextBlocks(text)).toBe('Hello');
  });

  it('returns empty when only injected content', () => {
    expect(stripInjectedContextBlocks('<injected context>\nx\n</injected context>')).toBe('');
  });

  it('passes through text with no injected block', () => {
    expect(stripInjectedContextBlocks('Plain title')).toBe('Plain title');
  });
});
