import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  normalizePmaChatStreamEvent,
  normalizePmaTailStreamEvent,
  openPmaChatEventSource,
  openPmaTailEventSource,
  parseJsonSseFrame,
  parseSseFrame
} from './streaming';

describe('SSE helpers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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

  it('normalizes PMA timeline stream events', () => {
    const parsed = parseJsonSseFrame('id: 7\nevent: timeline\ndata: {"item_id":"turn:1:intermediate:0001","kind":"intermediate"}\n\n');
    expect(parsed).not.toBeNull();

    expect(normalizePmaTailStreamEvent(parsed!)).toEqual({
      kind: 'timeline',
      lastEventId: '7',
      payload: { item_id: 'turn:1:intermediate:0001', kind: 'intermediate' }
    });
  });

  it('normalizes PMA chat snapshot stream events', () => {
    const parsed = parseJsonSseFrame('id: abc\nevent: chat_snapshot\ndata: {"threads":[{"managed_thread_id":"thread-1"}]}\n\n');
    expect(parsed).not.toBeNull();

    expect(normalizePmaChatStreamEvent(parsed!)).toEqual({
      kind: 'chat_snapshot',
      lastEventId: 'abc',
      payload: { threads: [{ managed_thread_id: 'thread-1' }] }
    });
  });

  it('opens PMA tail EventSource under the configured hub base path', () => {
    const close = vi.fn();
    const addEventListener = vi.fn();
    const eventSource = vi.fn(function EventSourceMock() {
      return { addEventListener, close };
    });
    vi.stubGlobal('EventSource', eventSource);

    const subscription = openPmaTailEventSource('thread/1', { onEvent: vi.fn() }, '/car');

    expect(eventSource).toHaveBeenCalledWith('/car/hub/pma/threads/thread%2F1/tail/events', {
      withCredentials: undefined
    });
    expect(addEventListener).toHaveBeenCalledWith('timeline', expect.any(Function));
    subscription.close();
    expect(close).toHaveBeenCalledOnce();
  });

  it('opens PMA chat EventSource under the configured hub base path', () => {
    const close = vi.fn();
    const addEventListener = vi.fn();
    const eventSource = vi.fn(function EventSourceMock() {
      return { addEventListener, close };
    });
    vi.stubGlobal('EventSource', eventSource);

    const subscription = openPmaChatEventSource({ onEvent: vi.fn() }, '/car');

    expect(eventSource).toHaveBeenCalledWith('/car/hub/pma/events', {
      withCredentials: undefined
    });
    expect(addEventListener).toHaveBeenCalledWith('chat_snapshot', expect.any(Function));
    subscription.close();
    expect(close).toHaveBeenCalledOnce();
  });
});
