import { describe, expect, it } from 'vitest';
import { markdownPreview, markdownToPlainText } from './plainText';

describe('markdownToPlainText', () => {
  it('returns an empty string for nullish input', () => {
    expect(markdownToPlainText(null)).toBe('');
    expect(markdownToPlainText(undefined)).toBe('');
    expect(markdownToPlainText('')).toBe('');
  });

  it('strips headings, emphasis, and inline code', () => {
    expect(markdownToPlainText('## Goal\n\nUsed by `scripts/smoke.py`. **Ready**.')).toBe(
      'Goal Used by scripts/smoke.py. Ready.'
    );
  });

  it('keeps link text and drops the target', () => {
    expect(markdownToPlainText('See [the spec](https://example.com/spec) first.')).toBe(
      'See the spec first.'
    );
  });

  it('drops fenced code blocks and images entirely', () => {
    expect(markdownToPlainText('Before\n\n```py\nprint(1)\n```\n\n![shot](a.png)\n\nAfter')).toBe(
      'Before After'
    );
  });

  it('flattens list markers and block quotes', () => {
    expect(markdownToPlainText('- one\n- two\n\n> quoted')).toBe('one two quoted');
  });

  it('collapses all whitespace to single spaces', () => {
    expect(markdownToPlainText('a\n\n\tb   c')).toBe('a b c');
  });
});

describe('markdownPreview', () => {
  it('leaves short text untouched', () => {
    expect(markdownPreview('# Short title', 40)).toBe('Short title');
  });

  it('truncates with a single-character ellipsis at the limit', () => {
    const preview = markdownPreview('x'.repeat(200), 20);
    expect(preview).toBe(`${'x'.repeat(20)}…`);
    expect(preview).toHaveLength(21);
  });

  it('trims trailing whitespace before the ellipsis', () => {
    expect(markdownPreview('abcde fghij klmno', 12)).toBe('abcde fghij…');
  });

  it('returns an empty string when nothing survives stripping', () => {
    expect(markdownPreview('```\ncode only\n```')).toBe('');
  });
});
