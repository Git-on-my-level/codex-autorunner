import { describe, expect, it } from 'vitest';
import type { StreamSubscription, TranscriptStreamOptions } from '$lib/api/streaming';
import {
  CurrentTicketChatPreviewProjection,
  type CurrentTicketChatPreviewState
} from '$lib/application/currentTicketChatPreviewProjection';

describe('CurrentTicketChatPreviewProjection', () => {
  it('connects and exposes render-ready preview state from transcript events', () => {
    const stream = streamFixture();
    const states: CurrentTicketChatPreviewState[] = [];
    const projection = new CurrentTicketChatPreviewProjection({
      openStream: stream.open,
      onStateChange: (state) => states.push(state)
    });

    projection.activate('chat-1');
    stream.opened[0].options.onEvent({
      kind: 'transcript_snapshot',
      lastEventId: '1',
      payload: {
        rows: [
          { kind: 'message', message: { role: 'user', text: 'older question' } },
          { kind: 'message', message: { role: 'assistant', text: 'latest answer' } }
        ]
      }
    });

    expect(stream.opened.map((item) => item.chatId)).toEqual(['chat-1']);
    expect(states[0]).toMatchObject({ targetChatId: 'chat-1', streamState: 'connecting' });
    expect(projection.snapshot()).toEqual({
      targetChatId: 'chat-1',
      latestText: 'latest answer',
      latestRole: 'assistant',
      streamState: 'connected'
    });
  });

  it('replaces and tears down the active stream when the chat id changes', () => {
    const stream = streamFixture();
    const projection = new CurrentTicketChatPreviewProjection({ openStream: stream.open });

    projection.activate('chat-1');
    projection.activate('chat-2');

    expect(stream.opened.map((item) => item.chatId)).toEqual(['chat-1', 'chat-2']);
    expect(stream.opened[0].closeCalls).toBe(1);
    expect(projection.snapshot()).toMatchObject({ targetChatId: 'chat-2', streamState: 'connecting' });
  });

  it('does not update from stale events after replacement', () => {
    const stream = streamFixture();
    const projection = new CurrentTicketChatPreviewProjection({ openStream: stream.open });

    projection.activate('chat-old');
    const oldOptions = stream.opened[0].options;
    projection.activate('chat-new');
    oldOptions.onEvent({
      kind: 'transcript_append',
      lastEventId: 'old',
      payload: { rows: [{ kind: 'message', message: { role: 'assistant', text: 'stale text' } }] }
    });

    expect(projection.snapshot()).toEqual({
      targetChatId: 'chat-new',
      latestText: '',
      latestRole: null,
      streamState: 'connecting'
    });
  });

  it('updates from append events and ignores patch events without preview rows', () => {
    const stream = streamFixture();
    const projection = new CurrentTicketChatPreviewProjection({ openStream: stream.open });

    projection.activate('chat-1');
    stream.opened[0].options.onEvent({
      kind: 'transcript_append',
      lastEventId: 'append-1',
      payload: { rows: [{ kind: 'message', message: { role: 'user', text: 'latest append' } }] }
    });
    expect(projection.snapshot()).toMatchObject({
      latestText: 'latest append',
      latestRole: 'user',
      streamState: 'connected'
    });

    stream.opened[0].options.onEvent({
      kind: 'transcript_patch',
      lastEventId: 'patch-1',
      payload: { status: { status: 'running' } }
    });
    expect(projection.snapshot()).toMatchObject({
      latestText: 'latest append',
      latestRole: 'user',
      streamState: 'connected'
    });
  });

  it('closes deterministically when deactivated or destroyed', () => {
    const stream = streamFixture();
    const projection = new CurrentTicketChatPreviewProjection({ openStream: stream.open });

    projection.activate('chat-1');
    projection.activate(null);
    projection.activate('chat-2');
    projection.destroy();

    expect(stream.opened[0].closeCalls).toBe(1);
    expect(stream.opened[1].closeCalls).toBe(1);
    expect(projection.snapshot()).toEqual({
      targetChatId: null,
      latestText: '',
      latestRole: null,
      streamState: 'idle'
    });
  });

  it('surfaces interrupted state from stream status and errors', () => {
    const stream = streamFixture();
    const projection = new CurrentTicketChatPreviewProjection({ openStream: stream.open });

    projection.activate('chat-1');
    stream.opened[0].options.onStatus?.('connected');
    expect(projection.snapshot().streamState).toBe('connected');

    stream.opened[0].options.onError?.(new Event('error'));
    expect(projection.snapshot().streamState).toBe('interrupted');
  });
});

function streamFixture(): {
  opened: { chatId: string; options: TranscriptStreamOptions; closeCalls: number }[];
  open: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
} {
  const fixture: {
    opened: { chatId: string; options: TranscriptStreamOptions; closeCalls: number }[];
    open: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
  } = {
    opened: [],
    open(chatId, options) {
      const opened = { chatId, options, closeCalls: 0 };
      fixture.opened.push(opened);
      return {
        close: () => {
          opened.closeCalls += 1;
        }
      };
    }
  };
  return fixture;
}
