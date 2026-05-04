import { describe, expect, it } from 'vitest';
import { normalizePmaTailStreamEvent, parseJsonSseFrame, parseSseFrame } from './streaming';

describe('SSE helpers', () => {
  it('parses named SSE frames with ids and retry hints', () => {
    const parsed = parseSseFrame('id: 42\nevent: tail\nretry: 5000\ndata: {"summary":"Edited"}\n\n');

    expect(parsed).toEqual({
      id: '42',
      event: 'tail',
      retry: 5000,
      data: '{"summary":"Edited"}'
    });
  });

  it('parses JSON data and normalizes PMA tail events', () => {
    const parsed = parseJsonSseFrame('id: 9\nevent: progress\ndata: {"phase":"testing"}\n\n');
    expect(parsed).not.toBeNull();
    const normalized = normalizePmaTailStreamEvent(parsed!);

    expect(normalized).toEqual({
      kind: 'progress',
      lastEventId: '9',
      payload: { phase: 'testing' }
    });
  });
});
