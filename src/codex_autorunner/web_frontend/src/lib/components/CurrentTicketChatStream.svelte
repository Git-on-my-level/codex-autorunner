<script lang="ts">
  import { onDestroy } from 'svelte';
  import { openPmaTailEventSource, type StreamSubscription } from '$lib/api/streaming';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { PmaTimelineItem } from '$lib/viewModels/domain';

  let {
    chatId,
    ticketLabel = null,
    ticketHref = null,
    statusLabel: statusText = null,
    statusSignal = 'idle'
  }: {
    chatId: string;
    ticketLabel?: string | null;
    ticketHref?: string | null;
    statusLabel?: string | null;
    statusSignal?: 'active' | 'waiting' | 'blocked' | 'failed' | 'invalid' | 'idle' | 'done';
  } = $props();

  let latestText = $state<string>('');
  let latestRole = $state<'user' | 'assistant' | 'intermediate' | null>(null);
  let streamState = $state<'idle' | 'connecting' | 'connected' | 'interrupted'>('idle');
  let subscription: StreamSubscription | null = null;
  let activeChatId: string | null = null;

  $effect(() => {
    const id = chatId;
    if (!id) {
      teardown();
      return;
    }
    if (id === activeChatId) return;
    teardown();
    activeChatId = id;
    streamState = 'connecting';
    connect(id);
  });

  onDestroy(() => teardown());

  function connect(id: string): void {
    subscription = openPmaTailEventSource(id, {
      onEvent: (event) => {
        if (activeChatId !== id) return;
        streamState = 'connected';
        if (event.kind === 'timeline') {
          const item = event.payload as unknown as PmaTimelineItem & { kind?: string; payload?: { text?: string } };
          const kind = String(item.kind ?? '');
          const text = String((item as { payload?: { text?: unknown } }).payload?.text ?? '').trim();
          if (!text) return;
          if (kind === 'assistant_message') {
            latestText = text;
            latestRole = 'assistant';
          } else if (kind === 'user_message') {
            latestText = text;
            latestRole = 'user';
          } else if (kind === 'intermediate' || kind === 'status') {
            latestText = text;
            latestRole = 'intermediate';
          }
        }
      },
      onError: () => {
        if (activeChatId !== id) return;
        streamState = 'interrupted';
      }
    });
  }

  function teardown(): void {
    subscription?.close();
    subscription = null;
    activeChatId = null;
    latestText = '';
    latestRole = null;
    streamState = 'idle';
  }

  const dotClass = $derived(`stream-dot signal-${statusSignal} stream-${streamState}`);
  const linkHref = $derived(ticketHref ? href(ticketHref) : null);
  const chatHref = $derived(href(`/chats/${encodeURIComponent(chatId)}`));
</script>

<aside class="current-chat-stream" aria-label="Current ticket chat output">
  <span class={dotClass} aria-hidden="true"></span>
  <div class="cs-headline">
    {#if linkHref && ticketLabel}
      <a class="cs-ticket" href={linkHref}>{ticketLabel}</a>
    {:else if ticketLabel}
      <span class="cs-ticket">{ticketLabel}</span>
    {/if}
    {#if statusText}
      <span class="cs-status signal-{statusSignal}">{statusText}</span>
    {/if}
  </div>
  <div class="cs-body" title={latestText}>
    {#if latestText}
      {#if latestRole === 'user'}
        <span class="cs-role">you:</span>
      {:else if latestRole === 'intermediate'}
        <span class="cs-role">…</span>
      {/if}
      <span class="cs-text">{latestText}</span>
    {:else}
      <span class="cs-text muted">
        {streamState === 'connecting' ? 'Connecting to live output…' : 'No output yet.'}
      </span>
    {/if}
  </div>
  <a class="cs-open" href={chatHref} aria-label="Open chat thread">Open →</a>
</aside>

<style>
  .current-chat-stream {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    padding: 6px 12px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
    font-size: var(--font-size-0);
    line-height: 1.4;
    min-width: 0;
  }

  .stream-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-ink-faint);
    flex-shrink: 0;
  }
  .stream-dot.signal-active { background: var(--color-success); box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-success) 50%, transparent); animation: cs-pulse 1.6s ease-out infinite; }
  .stream-dot.signal-waiting { background: var(--color-warning); }
  .stream-dot.signal-blocked,
  .stream-dot.signal-failed,
  .stream-dot.signal-invalid { background: var(--color-danger); }
  .stream-dot.signal-done { background: var(--color-success); opacity: 0.7; }

  @keyframes cs-pulse {
    0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-success) 55%, transparent); }
    70%  { box-shadow: 0 0 0 6px color-mix(in srgb, var(--color-success) 0%, transparent); }
    100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-success) 0%, transparent); }
  }

  .cs-headline {
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-2);
    flex-shrink: 0;
    min-width: 0;
  }

  .cs-ticket {
    color: var(--color-ink);
    font-weight: 600;
    text-decoration: none;
    white-space: nowrap;
  }
  a.cs-ticket:hover { color: var(--color-accent); }

  .cs-status {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    color: var(--color-ink-muted);
  }
  .cs-status.signal-active { color: var(--color-success); }
  .cs-status.signal-waiting { color: var(--color-warning); }
  .cs-status.signal-blocked,
  .cs-status.signal-failed,
  .cs-status.signal-invalid { color: var(--color-danger); }

  .cs-body {
    min-width: 0;
    color: var(--color-ink-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
  }
  .cs-role {
    color: var(--color-ink-faint);
    margin-right: 4px;
  }
  .cs-text {
    color: var(--color-ink-soft);
  }
  .cs-text.muted { color: var(--color-ink-faint); font-style: italic; font-family: inherit; }

  .cs-open {
    color: var(--color-ink-muted);
    text-decoration: none;
    font-weight: 500;
    flex-shrink: 0;
  }
  .cs-open:hover { color: var(--color-ink); }

  @media (max-width: 760px) {
    .current-chat-stream {
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: var(--space-2);
    }
    .cs-headline {
      grid-column: 2 / 3;
    }
    .cs-body {
      grid-column: 1 / -1;
    }
  }
</style>
