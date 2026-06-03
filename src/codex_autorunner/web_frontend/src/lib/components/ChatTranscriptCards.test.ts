import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ChatTranscriptCards from './ChatTranscriptCards.svelte';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';

const baseTrace = {
  turnId: 'turn-1',
  orderKey: '00000001|trace',
  timestamp: '2026-05-10T00:00:00.000Z',
  eventIds: [],
  progressSourceIds: [] as string[]
};

const goldenHermesCodexMarkdown = [
  '**Current State**',
  '',
  '- **active_context.md**: Empty',
  '- **AGENTS.md**: Current',
  '- Workspace path: `/Users/dazheng/car-workspace/codex-autorunner`',
  '',
  '| Section | Count |',
  '|---|---:|',
  '| PMA file inbox | 5 |'
].join('\n');

function assistantMessageCard(id: string, text: string): ChatTranscriptCard {
  return {
    kind: 'message',
    id,
    turnId: 't1',
    orderKey: '00000002|a1',
    timestamp: '2026-05-10T12:05:00.000Z',
    message: {
      id,
      chatId: 'c1',
      role: 'assistant',
      text,
      createdAt: '2026-05-10T12:05:00.000Z',
      status: 'running',
      artifacts: [],
      raw: {}
    }
  };
}

function intermediateCard(id: string, title: string, text: string): ChatTranscriptCard {
  return {
    ...baseTrace,
    kind: 'intermediate',
    id,
    title,
    text,
    detail: null
  };
}

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

  it('renders context compaction as an expandable retained-context card', () => {
    const cards: ChatTranscriptCard[] = [
      {
        kind: 'context_compaction',
        id: 'compact-1',
        title: 'Context compacted by CAR',
        text: 'Earlier conversation was summarized.',
        detail: '{"action_type":"managed_thread_compact"}',
        turnId: null,
        orderKey: '00000002|compact',
        timestamp: '2026-05-10T00:01:00.000Z',
        compaction: {
          source: 'car',
          provider: null,
          summary: 'Keep the current goal.',
          preview: 'Keep the current goal.',
          scope: 'managed_thread',
          startedFreshSession: true,
          storedByCar: true
        }
      }
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).toContain('class="tool-call-bar context-compaction-card"');
    expect(body).toContain('<span>CAR</span>');
    expect(body).toContain('Keep the current goal.');
    expect(body).toContain('Retained context');
    expect(body).toContain('<dt>Fresh session</dt>');
    expect(body).toContain('<dd>yes</dd>');
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

  it('renders markdown for the assistant message currently marked as streaming', () => {
    const cards: ChatTranscriptCard[] = [assistantMessageCard('a-streaming', goldenHermesCodexMarkdown)];

    const { body } = render(ChatTranscriptCards, {
      props: { cards, streamingMessageId: 'a-streaming' }
    });

    expect(body).toContain('class="message-markdown markdown-body streaming"');
    expect(body).toContain('<strong>Current State</strong>');
    expect(body).toContain('<strong>AGENTS.md</strong>');
    expect(body).toContain('<code>/Users/dazheng/car-workspace/codex-autorunner</code>');
    expect(body).toContain('<table>');
    expect(body).not.toContain('**Current State**');
    expect(body).not.toContain('AG ENTS');
    expect(body).not.toContain('car-work space');
    expect(body).not.toContain('cod ex-aut orunner');
  });

  it('does not add live regions to virtualized transcript rows', () => {
    const cards: ChatTranscriptCard[] = [
      assistantMessageCard('a-complete', 'Completed assistant response'),
      intermediateCard('trace-1', 'thinking', 'Completed thinking trace')
    ];

    const { body } = render(ChatTranscriptCards, { props: { cards } });

    expect(body).not.toContain('aria-live="polite"');
    expect(body).not.toContain('aria-atomic="true"');
  });

  it('renders the Hermes/Codex golden markdown transcript identically when streaming and completed', () => {
    const streaming = render(ChatTranscriptCards, {
      props: {
        cards: [assistantMessageCard('a-golden', goldenHermesCodexMarkdown)],
        streamingMessageId: 'a-golden'
      }
    }).body;
    const completed = render(ChatTranscriptCards, {
      props: { cards: [assistantMessageCard('a-golden', goldenHermesCodexMarkdown)] }
    }).body;

    for (const body of [streaming, completed]) {
      expect(body).toContain('<strong>Current State</strong>');
      expect(body).toContain('<strong>active_context.md</strong>');
      expect(body).toContain('<strong>AGENTS.md</strong>');
      expect(body).toContain('<code>/Users/dazheng/car-workspace/codex-autorunner</code>');
      expect(body).toContain('<table>');
      expect(body).not.toContain('**AGENTS.md**');
      expect(body).not.toContain('active_context.md **');
      expect(body).not.toContain('AG ENTS');
      expect(body).not.toContain('car-work space');
      expect(body).not.toContain('cod ex-aut orunner');
    }
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
          visibleText: 'Please fix the archive button.',
          modelContextText: 'CAR managed repo',
          rawModelPrompt: '<injected context>\nCAR managed repo\n</injected context>\n\nPlease fix the archive button.',
          createdAt: '2026-05-10T12:00:00.000Z',
          status: null,
          artifacts: [],
          raw: {}
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

  function toolGroupCard(states: Array<'started' | 'completed' | 'failed'>): ChatTranscriptCard {
    return {
      kind: 'tool_group',
      id: 'tools-1',
      turnId: 'turn-1',
      orderKey: '00000003|tools',
      timestamp: '2026-05-10T00:00:00.000Z',
      tools: states.map((state, index) => ({
        id: `tool-${index}`,
        title: 'bash',
        summary: 'tool: bash',
        detail: null,
        state,
        eventIds: []
      }))
    };
  }

  it('strips the redundant "tool:" prefix and same-tool count in the headline', () => {
    const { body } = render(ChatTranscriptCards, {
      props: { cards: [toolGroupCard(['completed', 'completed', 'completed'])] }
    });
    expect(body).toContain('bash ×3');
    expect(body).not.toContain('tool: bash');
    expect(body).not.toContain('· +2 more');
  });

  it('stops spinning tool calls once the run is no longer active', () => {
    const { body } = render(ChatTranscriptCards, {
      props: { cards: [toolGroupCard(['completed', 'started'])], runActive: false }
    });
    // The stale "started" tool downgrades to an indeterminate marker, not a spinner.
    expect(body).not.toContain('tool-status-started');
    expect(body).toContain('tool-status-unknown');
  });

  it('keeps the spinner while the run is genuinely active', () => {
    const { body } = render(ChatTranscriptCards, {
      props: { cards: [toolGroupCard(['completed', 'started'])], runActive: true }
    });
    expect(body).toContain('tool-status-started');
  });

  it('renders thinking summaries as plain text, not raw markdown', () => {
    const { body } = render(ChatTranscriptCards, {
      props: {
        cards: [
          intermediateCard(
            'thinking-md',
            'thinking',
            'No. This branch has **zero commits** ahead of `origin/main`.'
          )
        ]
      }
    });
    expect(body).toContain('zero commits ahead of origin/main');
    // Summary teaser must not leak markdown syntax.
    const summary = body.slice(0, body.indexOf('thinking-trace-body'));
    expect(summary).not.toContain('**zero commits**');
    expect(summary).not.toContain('`origin/main`');
  });

  it('renders assistant shared delivery files as downloadable pills', () => {
    const { body } = render(ChatTranscriptCards, {
      props: {
        cards: [],
        assistantLabel: 'Codex',
        sharedFiles: [
          {
            deliveryId: 'delivery:abc',
            artifactId: 'sha256:abc',
            filename: 'spec.md',
            state: 'sent',
            targetSurface: 'discord',
            targetConversation: 'channel:1',
            workspaceScope: null,
            attempts: 1,
            size: 1024,
            mimeType: 'text/markdown',
            downloadUrl: '/hub/filebox/repo/artifacts/deliveries/delivery%3Aabc/download',
            createdAt: '2026-05-21T00:00:00Z',
            updatedAt: '2026-05-21T00:01:00Z',
            sentAt: '2026-05-21T00:01:00Z',
            failedAt: null,
            lastError: null,
            raw: {}
          }
        ]
      }
    });

    expect(body).toContain('class="message assistant shared-files-message"');
    expect(body).toContain('Codex');
    expect(body).toContain('spec.md');
    expect(body).toContain('delivery-sent');
    expect(body).toContain('/hub/filebox/repo/artifacts/deliveries/delivery%3Aabc/download');
  });
});
