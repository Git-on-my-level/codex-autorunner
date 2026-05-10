import { describe, expect, it } from 'vitest';
import { noticeTone } from './noticeTone';

describe('noticeTone', () => {
  it('classifies common PMA status strings', () => {
    expect(noticeTone(null)).toBe('neutral');
    expect(noticeTone('')).toBe('neutral');
    expect(noticeTone('Ticket saved.')).toBe('success');
    expect(noticeTone('Save failed')).toBe('danger');
    expect(noticeTone('This ticket cannot be edited until it has a numeric TICKET index.')).toBe('warning');
    expect(noticeTone('Continuing worktree ticket flow...')).toBe('neutral');
  });
});
