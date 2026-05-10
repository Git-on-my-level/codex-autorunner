import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ChatTranscriptCards from './ChatTranscriptCards.svelte';
import type { PmaCard } from '$lib/viewModels/pmaChat';

const baseTrace = {
  turnId: 'turn-1',
  orderKey: '00000001|trace',
  timestamp: '2026-05-10T00:00:00.000Z',
  eventIds: []
};

describe('ChatTranscriptCards', () => {
  it('collapses thinking traces while keeping commentary visible', () => {
    const cards: PmaCard[] = [
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'thinking-1',
        title: 'thinking',
        text: 'Private chain-of-thought style text',
        detail: '1 thinking update'
      },
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'commentary-1',
        title: 'commentary',
        text: 'Visible progress update.',
        detail: null
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="tool-call-bar thinking-trace"');
    expect(body).toContain('<span>Thinking</span>');
    expect(body).toContain('<strong>1 thinking update</strong>');
    expect(body).toContain('Private chain-of-thought style text');
    expect(body).toContain('class="message commentary"');
    expect(body).toContain('Visible progress update.');
  });

  it('collapses thinking traces inside worked summaries', () => {
    const cards: PmaCard[] = [
      {
        kind: 'turn_summary',
        id: 'summary-1',
        title: 'Worked for 12s',
        turnId: 'turn-1',
        orderKey: '00000002|summary',
        timestamp: '2026-05-10T00:00:12.000Z',
        cards: [
          {
            ...baseTrace,
            kind: 'intermediate',
            id: 'thinking-1',
            title: 'thinking',
            text: 'Nested private trace',
            detail: '2 thinking updates'
          },
          {
            ...baseTrace,
            kind: 'intermediate',
            id: 'commentary-1',
            title: 'commentary',
            text: 'Nested visible update.',
            detail: null
          }
        ]
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="tool-call-bar thinking-trace nested-trace"');
    expect(body).toContain('<strong>2 thinking updates</strong>');
    expect(body).toContain('Nested private trace');
    expect(body).toContain('class="message commentary nested-commentary"');
    expect(body).toContain('Nested visible update.');
  });
});
