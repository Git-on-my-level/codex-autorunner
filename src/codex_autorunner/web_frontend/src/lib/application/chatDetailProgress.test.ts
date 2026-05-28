import { describe, expect, it } from 'vitest';
import type { ChatRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import { mergeChatProgressUpdate } from './chatDetailProgress';

const now = '2026-05-16T12:00:00.000Z';

describe('chat detail progress reconciliation', () => {
  it('merges live progress elapsed time and deduplicates event rows', () => {
    const previous = progress('run-1', 10, [artifact('event-1')]);
    const next = progress('run-1', 5, [artifact('event-1'), artifact('event-2')]);

    const merged = mergeChatProgressUpdate(previous, next, Date.parse(now) + 20_000);

    expect(merged.elapsedSeconds).toBe(20);
    expect(merged.events.map((event) => event.id)).toEqual(['event-1', 'event-2']);
  });

  it('replaces live progress events by canonical progress item id', () => {
    const previous = progress('run-1', 10, [
      artifact('event-1', { item_id: 'progress:assistant_update:0001', summary: 'Read' })
    ]);
    const next = progress('run-1', 11, [
      artifact('event-2', { item_id: 'progress:assistant_update:0001', summary: 'Reading files' })
    ]);

    const merged = mergeChatProgressUpdate(previous, next, Date.parse(now) + 11_000);

    expect(merged.events).toHaveLength(1);
    expect(merged.events[0].id).toBe('event-2');
    expect(merged.events[0].raw.progress_item).toMatchObject({
      item_id: 'progress:assistant_update:0001',
      summary: 'Reading files'
    });
  });
});

function artifact(id: string, progressItem: Record<string, unknown> = {}): SurfaceArtifact {
  return {
    id,
    kind: 'progress',
    title: id,
    summary: null,
    url: null,
    createdAt: now,
    raw: Object.keys(progressItem).length ? { progress_item: progressItem } : {}
  };
}

function progress(id: string, elapsedSeconds: number, events: SurfaceArtifact[]): ChatRunProgress {
  return {
    id,
    chatId: 'chat-1',
    status: 'running',
    workStatus: 'running',
    operatorStatus: 'running',
    terminal: false,
    streamShouldClose: false,
    streamCloseReason: null,
    phase: null,
    guidance: null,
    queueDepth: 0,
    elapsedSeconds,
    startedAt: now,
    idleSeconds: null,
    lastEventId: null,
    lastEventAt: null,
    progressPercent: null,
    events,
    raw: {}
  };
}
