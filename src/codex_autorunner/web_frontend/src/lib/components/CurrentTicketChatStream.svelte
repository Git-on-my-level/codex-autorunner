<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    createCurrentTicketChatPreviewProjection,
    type CurrentTicketChatPreviewState
  } from '$lib/application/currentTicketChatPreviewProjection';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';

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
    statusSignal?: 'running' | 'waiting' | 'blocked' | 'failed' | 'invalid' | 'idle' | 'done';
  } = $props();

  let preview = $state<CurrentTicketChatPreviewState>({
    targetChatId: null,
    latestText: '',
    latestRole: null,
    streamState: 'idle'
  });
  const projection = createCurrentTicketChatPreviewProjection({
    onStateChange: (state) => {
      preview = state;
    }
  });

  $effect(() => {
    projection.activate(chatId);
  });

  onDestroy(() => projection.destroy());

  const dotClass = $derived(`stream-dot signal-${statusSignal} stream-${preview.streamState}`);
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
  <div class="cs-body" title={preview.latestText}>
    {#if preview.latestText}
      {#if preview.latestRole === 'user'}
        <span class="cs-role">you:</span>
      {:else if preview.latestRole === 'intermediate'}
        <span class="cs-role">…</span>
      {/if}
      <span class="cs-text">{preview.latestText}</span>
    {:else}
      <span class="cs-text muted">
        {preview.streamState === 'connecting' ? 'Connecting to live output…' : 'No output yet.'}
      </span>
    {/if}
  </div>
  <a class="cs-open ghost-button" href={chatHref} aria-label="Open chat thread">Open chat</a>
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
  .stream-dot.signal-running { background: var(--color-success); box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-success) 50%, transparent); animation: cs-pulse 1.6s ease-out infinite; }
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
  .cs-status.signal-running { color: var(--color-success); }
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
    flex-shrink: 0;
  }

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
