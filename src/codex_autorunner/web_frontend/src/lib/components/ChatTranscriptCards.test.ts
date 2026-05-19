import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ChatTranscriptCards from './ChatTranscriptCards.svelte';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';

const baseTrace = {
  turnId: 'turn-1',
  orderKey: '00000001|trace',
  timestamp: '2026-05-10T00:00:00.000Z',
  eventIds: [],
  progressSourceIds: [] as string[]
};

describe('ChatTranscriptCards', () => {
  it('collapses thinking traces while keeping commentary visible', () => {
    const cards: ChatTranscriptCard[] = [
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'thinking-1',
        title: 'thinking',
        text: 'Private chain-of-thought style text',
        detail: '1 thinking update · source events turn:one:intermediate:think-1'
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
    expect(body).not.toContain('source events');
    expect(body).not.toContain('turn:one:intermediate:think-1');
    expect(body).toContain('Private chain-of-thought style text');
    expect(body).toContain('class="message commentary"');
    expect(body).toContain('Visible progress update.');
  });

  it('collapses non-commentary progress traces instead of rendering them as chat bubbles', () => {
    const cards: ChatTranscriptCard[] = [
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'progress-1',
        title: 'progress',
        text: 'Managed-thread execution accepted the request.',
        detail: '1 progress update · source events turn:one:intermediate:progress-1'
      },
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'commentary-1',
        title: 'commentary',
        text: 'Visible user-facing note.',
        detail: null
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="tool-call-bar trace-update"');
    expect(body).toContain('<span>Progress</span>');
    expect(body).toContain('<strong>1 progress update</strong>');
    expect(body).not.toContain('class="message commentary"><span class="commentary-kind">progress</span>');
    expect(body).toContain('class="message commentary"');
    expect(body).toContain('Visible user-facing note.');
  });

  it('renders richer progress labels when the model supplies them', () => {
    const cards: ChatTranscriptCard[] = [
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'progress-2',
        title: 'Starting pytest',
        text: 'Managed-thread execution accepted the request.',
        detail: '1 progress update · source events turn:one:intermediate:progress-2'
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('<span>Starting pytest</span>');
  });

  it('collapses thinking traces inside worked summaries', () => {
    const cards: ChatTranscriptCard[] = [
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
            detail: '2 thinking updates · source events turn:one:intermediate:think-1, turn:one:intermediate:think-2'
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
    expect(body).not.toContain('source events');
    expect(body).not.toContain('turn:one:intermediate:think-1');
    expect(body).toContain('Nested private trace');
    expect(body).toContain('class="message commentary nested-commentary"');
    expect(body).toContain('Nested visible update.');
  });

  it('bounds nested activity rendered inside turn summaries', () => {
    const cards: ChatTranscriptCard[] = [
      {
        kind: 'turn_summary',
        id: 'summary-1',
        title: '120 thinking updates',
        turnId: 'turn-1',
        orderKey: '00000002|summary',
        timestamp: '2026-05-10T00:00:12.000Z',
        cards: Array.from({ length: 120 }, (_, index) => ({
          ...baseTrace,
          kind: 'intermediate' as const,
          id: `thinking-${index}`,
          title: 'thinking',
          text: `Nested private trace ${index}`,
          detail: `${index + 1} thinking updates`
        }))
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('Nested private trace 79');
    expect(body).not.toContain('Nested private trace 80');
    expect(body).toContain('40 additional activity updates omitted');
  });

  it('renders compact timestamps on user and assistant message bubbles', () => {
    const cards: ChatTranscriptCard[] = [
      {
        kind: 'message',
        id: 'u1',
        turnId: 't1',
        orderKey: '00000001|u1',
        timestamp: '2026-05-10T12:00:00.000Z',
        message: {
          id: 'u1',
          chatId: 'c1',
          role: 'user',
          text: 'Hello',
          createdAt: '2026-05-10T12:00:00.000Z',
          status: null,
          artifacts: [],
          raw: {}
        }
      },
      {
        kind: 'message',
        id: 'a1',
        turnId: 't1',
        orderKey: '00000002|a1',
        timestamp: '2026-05-10T12:05:00.000Z',
        message: {
          id: 'a1',
          chatId: 'c1',
          role: 'assistant',
          text: 'Hi there',
          createdAt: '2026-05-10T12:05:00.000Z',
          status: 'done',
          artifacts: [],
          raw: {}
        }
      }
    ];
    const { body } = render(ChatTranscriptCards, { props: { cards } });
    expect(body).toContain('class="message-timestamp"');
    expect(body).toContain('datetime="2026-05-10T12:00:00.000Z"');
    expect(body).toContain('datetime="2026-05-10T12:05:00.000Z"');
  });

  it('renders model-only capsule metadata as a separate collapsed card above the user message', () => {
    const cards: ChatTranscriptCard[] = [
      {
        kind: 'message',
        id: 'u1',
        turnId: 't1',
        orderKey: '00000001|u1',
        timestamp: '2026-05-10T12:00:00.000Z',
        message: {
          id: 'u1',
          chatId: 'c1',
          role: 'user',
          text: 'Please fix the archive button.',
          visibility: 'user_visible',
          userVisibleText: 'Please fix the archive button.',
          capsuleRefs: [
            {
              capsuleId: 'car.repo_basics',
              capsuleVersion: '1',
              visibility: 'model_only',
              scope: 'repo',
              sourceDigest: 'sha256:repo',
              payloadDigest: null,
              renderDecision: 'rendered',
              reason: 'repo_context'
            }
          ],
          createdAt: '2026-05-10T12:00:00.000Z',
          status: null,
          artifacts: [],
          raw: {}
        }
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="injected-prompt-card"');
    expect(body).toContain('<span>Model-only context</span>');
    expect(body).toContain('car.repo_basics v1 · repo');
    expect(body).toContain('repo_context');
    expect(body).toContain('Please fix the archive button.');
    expect(body.indexOf('class="injected-prompt-card"')).toBeLessThan(body.indexOf('class="message user"'));
    expect(body).not.toContain('&lt;injected context&gt;');
  });

  it('hides legacy injected prompt text in a collapsed card and leaves only the user text visible', () => {
    const cards: ChatTranscriptCard[] = [
      {
        kind: 'message',
        id: 'u1',
        turnId: 't1',
        orderKey: '00000001|u1',
        timestamp: '2026-05-10T12:00:00.000Z',
        message: {
          id: 'u1',
          chatId: 'c1',
          role: 'user',
          text: 'Please fix the archive button.',
          createdAt: '2026-05-10T12:00:00.000Z',
          status: null,
          artifacts: [],
          raw: {
            payload: {
              raw_model_prompt: '<injected context>\nCAR managed repo\n</injected context>\n\nPlease fix the archive button.'
            }
          }
        }
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="injected-prompt-card"');
    expect(body).toContain('<span>Injected prompt</span>');
    expect(body).toContain('CAR managed repo');
    expect(body).toContain('Please fix the archive button.');
    expect(body).not.toContain('&lt;injected context&gt;');
    expect(body.indexOf('CAR managed repo')).toBeLessThan(body.indexOf('class="message user"'));
  });

  it('never renders raw JSON detail as a trace headline', () => {
    const jsonDetail = '{ "event_id": 1, "event_type": "progress", "lines": ["1"] }';
    const cards: ChatTranscriptCard[] = [
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'progress-json',
        title: 'progress',
        text: 'Working on it',
        detail: jsonDetail
      },
      {
        ...baseTrace,
        kind: 'intermediate',
        id: 'thinking-json',
        title: 'thinking',
        text: 'Reasoning through the request',
        detail: jsonDetail
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).not.toContain('event_id');
    expect(body).not.toContain('event_type');
    expect(body).toContain('Working on it');
    expect(body).toContain('Reasoning through the request');
  });
});
