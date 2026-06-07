import { describe, expect, it } from 'vitest';
import { collapseRepeatedParagraphs } from './traceText';

describe('collapseRepeatedParagraphs', () => {
  const paragraph =
    'The user is accessing through a Tailscale URL and wants to inspect the repository state.';

  it('collapses a paragraph repeated ten times with blank lines', () => {
    const repeated = Array.from({ length: 10 }, () => paragraph).join('\n\n');
    expect(collapseRepeatedParagraphs(repeated)).toBe(paragraph);
  });

  it('collapses a paragraph repeated ten times on single newlines', () => {
    const repeated = Array.from({ length: 10 }, () => paragraph).join('\n');
    expect(collapseRepeatedParagraphs(repeated)).toBe(paragraph);
  });

  it('collapses a glued exact duplicate block repeated ten times', () => {
    const repeated = paragraph.repeat(10);
    expect(collapseRepeatedParagraphs(repeated)).toBe(paragraph);
  });

  it('leaves distinct paragraphs untouched', () => {
    const text = `${paragraph}\n\nSecond distinct paragraph with enough length to avoid accidental whole-string collapse.`;
    expect(collapseRepeatedParagraphs(text)).toBe(text);
  });

  it('leaves non-consecutive duplicate paragraphs untouched', () => {
    const other = 'Second distinct paragraph with enough length to avoid accidental whole-string collapse.';
    const text = `${paragraph}\n\n${other}\n\n${paragraph}`;
    expect(collapseRepeatedParagraphs(text)).toBe(text);
  });

  it('does not collapse short repeated tokens', () => {
    expect(collapseRepeatedParagraphs('yes yes yes')).toBe('yes yes yes');
  });
});
