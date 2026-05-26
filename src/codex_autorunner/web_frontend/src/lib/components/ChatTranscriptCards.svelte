<script lang="ts">
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import {
    compactChatTranscriptCards,
    formatCompactMessageDateTime,
    type ChatTranscriptCard,
    type ChatToolCallCard
  } from '$lib/viewModels/pmaChat';
  import type { PmaMessageCapsuleRef } from '$lib/viewModels/domain';
  import type { ArtifactDelivery, SurfaceArtifact } from '$lib/viewModels/domain';

  let {
    cards,
    assistantLabel = 'Assistant',
    streamingMessageId = null,
    sharedFiles = []
  }: {
    cards: ChatTranscriptCard[];
    assistantLabel?: string;
    streamingMessageId?: string | null;
    sharedFiles?: ArtifactDelivery[];
  } = $props();

  const MAX_RENDERED_TOOL_GROUP_ITEMS = 80;
  const MAX_RENDERED_TURN_SUMMARY_CARDS = 80;
  const displayCardCache = new WeakMap<ChatTranscriptCard[], ChatTranscriptCard[]>();

  const userToggled = $state<Record<string, boolean>>({});

  function attachmentKindLabel(kind: SurfaceArtifact['kind']): string {
    if (kind === 'image' || kind === 'screenshot') return 'image';
    if (kind === 'link' || kind === 'preview_url') return 'link';
    return 'file';
  }

  function attachmentSizeLabel(artifact: SurfaceArtifact): string | null {
    const raw = artifact.raw as Record<string, unknown> | null | undefined;
    if (!raw) return null;
    const size = raw.size_label ?? raw.sizeLabel ?? raw.size;
    if (typeof size === 'string' && size.trim()) return size;
    return null;
  }

  function deliveryStateLabel(state: string): string {
    const normalized = state.trim().toLowerCase();
    if (normalized === 'sent') return 'sent';
    if (normalized === 'failed') return 'failed';
    if (normalized === 'cancelled') return 'cancelled';
    if (normalized === 'sending' || normalized === 'claimed') return 'sending';
    return 'pending';
  }

  function deliveryMeta(delivery: ArtifactDelivery): string | null {
    const parts = [
      delivery.targetSurface ? `to ${delivery.targetSurface}` : null,
      delivery.size !== null ? `${Math.round(delivery.size / 1024)} KB` : null
    ].filter((part): part is string => Boolean(part));
    return parts.length ? parts.join(' · ') : null;
  }

  function isThinkingTrace(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): boolean {
    return card.title.trim().toLowerCase() === 'thinking';
  }

  function isCommentaryTrace(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): boolean {
    return card.title.trim().toLowerCase() === 'commentary';
  }

  function traceKindLabel(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): string {
    const title = card.title.trim();
    if (!title) return 'Update';
    return title.charAt(0).toUpperCase() + title.slice(1);
  }

  function looksLikeJson(value: string): boolean {
    const trimmed = value.trim();
    return trimmed.startsWith('{') || trimmed.startsWith('[');
  }

  function traceSummaryLabel(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): string {
    const detail = card.detail?.split('·', 1)[0]?.trim();
    if (detail && !looksLikeJson(detail)) return detail;
    const text = card.text.trim().replace(/\s+/g, ' ');
    if (text) return text.length > 80 ? `${text.slice(0, 80).trimEnd()}…` : text;
    return traceKindLabel(card);
  }

  function thinkingTraceLabel(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): string {
    const detail = card.detail;
    if (detail) {
      const trimmed = detail.trim();
      if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(trimmed);
          const stack: unknown[] = [parsed];
          while (stack.length) {
            const node = stack.pop();
            if (!node || typeof node !== 'object') continue;
            const obj = node as Record<string, unknown>;
            const ms = obj.duration_ms ?? obj.elapsed_ms;
            if (typeof ms === 'number' && Number.isFinite(ms)) {
              return `Thought for ${(ms / 1000).toFixed(1)}s`;
            }
            const secs = obj.duration_seconds;
            if (typeof secs === 'number' && Number.isFinite(secs)) {
              return `Thought for ${secs.toFixed(1)}s`;
            }
            for (const v of Object.values(obj)) {
              if (v && typeof v === 'object') stack.push(v);
            }
          }
        } catch {
          // fall through
        }
      }
    }
    const count = card.detail?.split('·', 1)[0]?.trim();
    if (count && !looksLikeJson(count)) return count;
    const text = card.text?.trim();
    if (text) {
      const oneLine = text.replace(/\s+/g, ' ');
      return oneLine.length > 80 ? `${oneLine.slice(0, 80).trimEnd()}…` : oneLine;
    }
    return 'Reasoning trace';
  }

  function isToolGroupRunning(tools: ChatToolCallCard[]): boolean {
    return tools.length > 0 && tools[0].state === 'started';
  }

  function toolGroupOpen(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>): boolean {
    const userPref = userToggled[card.id];
    if (typeof userPref === 'boolean') return userPref;
    return isToolGroupRunning(card.tools);
  }

  function handleToolToggle(cardId: string, event: Event) {
    const el = event.currentTarget as HTMLDetailsElement;
    userToggled[cardId] = el.open;
  }

  function toolHeadlineText(tool: ChatToolCallCard | undefined): string {
    if (!tool) return 'Tool call';
    const summary = tool.summary?.trim();
    if (summary) return summary;
    return tool.title;
  }

  function visibleToolGroupItems(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>): ChatToolCallCard[] {
    return card.tools.slice(0, MAX_RENDERED_TOOL_GROUP_ITEMS);
  }

  function omittedToolGroupItemCount(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>): number {
    return Math.max(0, card.tools.length - MAX_RENDERED_TOOL_GROUP_ITEMS);
  }

  function visibleTurnSummaryCards(card: Extract<ChatTranscriptCard, { kind: 'turn_summary' }>): ChatTranscriptCard[] {
    return card.cards.slice(0, MAX_RENDERED_TURN_SUMMARY_CARDS);
  }

  function omittedTurnSummaryCardCount(card: Extract<ChatTranscriptCard, { kind: 'turn_summary' }>): number {
    return Math.max(0, card.cards.length - MAX_RENDERED_TURN_SUMMARY_CARDS);
  }

  function modelOnlyCapsuleRefs(card: Extract<ChatTranscriptCard, { kind: 'message' }>): PmaMessageCapsuleRef[] {
    if (card.message.role !== 'user') return [];
    const structuredRefs = card.message.modelContextRefs ?? [];
    if (structuredRefs.length > 0) return structuredRefs;
    return (card.message.capsuleRefs ?? []).filter((ref) => ref.visibility === 'model_only');
  }

  function capsuleRefLabel(ref: PmaMessageCapsuleRef): string {
    return `${ref.capsuleId} v${ref.capsuleVersion} · ${ref.scope}`;
  }

  function compactionSourceLabel(card: Extract<ChatTranscriptCard, { kind: 'context_compaction' }>): string {
    if (card.compaction.source === 'car') return 'CAR';
    if (card.compaction.provider) return card.compaction.provider;
    if (card.compaction.source === 'provider') return 'Provider';
    return 'Runtime';
  }

  function compactionScopeLabel(value: string | null): string {
    if (value === 'managed_thread') return 'Managed thread';
    if (value === 'provider_session') return 'Provider session';
    return value || 'Context';
  }

  function compactionPreview(card: Extract<ChatTranscriptCard, { kind: 'context_compaction' }>): string {
    return card.compaction.preview || card.compaction.summary || card.text || 'No retained summary was exposed.';
  }

  function displayCardsFor(input: ChatTranscriptCard[]): ChatTranscriptCard[] {
    const cached = displayCardCache.get(input);
    if (cached) return cached;
    const compacted = compactChatTranscriptCards(input);
    displayCardCache.set(input, compacted);
    return compacted;
  }

  const displayCards = $derived(displayCardsFor(cards));
</script>

{#each displayCards as card (card.id)}
  {#if card.kind === 'message'}
    {@const isStreaming = card.message.role === 'assistant' && card.id === streamingMessageId}
    {@const modelContextRefs = modelOnlyCapsuleRefs(card)}
    {@const visibleText = card.message.visibleText ?? card.message.text}
    {@const modelContextText = card.message.role === 'user' ? card.message.modelContextText : null}
    {#if modelContextRefs.length > 0 || modelContextText}
      <details class="injected-prompt-card">
        <summary>
          <span>{modelContextText ? 'Injected prompt' : 'Model-only context'}</span>
        </summary>
        <div class="injected-prompt-body markdown-body">
          {#if modelContextText}
            {@html renderMarkdownToHtml(modelContextText, { openLinksInNewTab: true })}
          {/if}
          {#if modelContextRefs.length > 0}
            <ul>
              {#each modelContextRefs as ref (`${ref.capsuleId}:${ref.capsuleVersion}:${ref.sourceDigest}`)}
                <li>
                  <strong>{capsuleRefLabel(ref)}</strong>
                  {#if ref.reason}
                    <small>{ref.reason}</small>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      </details>
    {/if}
    {@const isOptimisticUser = card.message.role === 'user' && card.id.startsWith('optimistic:user:')}
    <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}${isOptimisticUser ? ' is-sending' : ''}`}>
      <span>{card.message.role === 'user' ? 'You' : assistantLabel}</span>
      {#if isStreaming}
        <div class="message-markdown markdown-body streaming">
          {@html renderMarkdownToHtml(visibleText, { openLinksInNewTab: true })}
        </div>
      {:else}
        <div class="message-markdown markdown-body">
          {@html renderMarkdownToHtml(visibleText, { openLinksInNewTab: true })}
        </div>
      {/if}
      {#if card.message.role === 'user' && card.message.artifacts.length > 0}
        <ul class="message-attachments" aria-label="Attachments">
          {#each card.message.artifacts as artifact (artifact.id)}
            {@const kindLabel = attachmentKindLabel(artifact.kind)}
            {@const sizeLabel = attachmentSizeLabel(artifact)}
            {@const url = artifact.url ?? null}
            <li class={`message-attachment-pill kind-${kindLabel}`}>
              <span class="attachment-kind">{kindLabel}</span>
              {#if url}
                <a href={href(url)} target="_blank" rel="noopener" title={artifact.title}><strong>{artifact.title}</strong></a>
              {:else}
                <strong title={artifact.title}>{artifact.title}</strong>
              {/if}
              {#if sizeLabel}
                <em>{sizeLabel}</em>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
      {#if isOptimisticUser}
        <span class="message-delivery sending" aria-live="polite">
          <span class="message-delivery-dot" aria-hidden="true"></span>
          Sending…
        </span>
      {:else if card.message.role === 'user' || card.message.role === 'assistant'}
        {@const sentLabel = formatCompactMessageDateTime(card.message.createdAt)}
        {#if sentLabel}
          <time class="message-timestamp" datetime={card.message.createdAt ?? undefined} title={card.message.createdAt ?? undefined}>
            {#if card.message.role === 'user'}
              <span class="message-delivery-tick" aria-label="Sent" title="Sent">✓</span>
            {/if}
            {sentLabel}
          </time>
        {/if}
      {/if}
    </article>
  {:else if card.kind === 'intermediate'}
    {#if isThinkingTrace(card)}
      <details class="tool-call-bar thinking-trace">
        <summary>
          <span>Thinking</span>
          <strong>{thinkingTraceLabel(card)}</strong>
        </summary>
        <div class="thinking-trace-body markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </details>
    {:else if isCommentaryTrace(card)}
      <article class="message commentary">
        <span class="commentary-kind">{card.title}</span>
        <div class="message-markdown markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </article>
    {:else}
      <details class="tool-call-bar trace-update">
        <summary>
          <span>{traceKindLabel(card)}</span>
          <strong>{traceSummaryLabel(card)}</strong>
        </summary>
        <div class="thinking-trace-body markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </details>
    {/if}
  {:else if card.kind === 'tool_group'}
    {@const headlineTool = card.tools[0]}
    {@const isOpen = toolGroupOpen(card)}
    <details class="tool-call-bar" open={isOpen} ontoggle={(e) => handleToolToggle(card.id, e)}>
      <summary>
        <strong>
          {toolHeadlineText(headlineTool)}{card.tools.length > 1 ? ` · +${card.tools.length - 1} more` : ''}
        </strong>
      </summary>
      <ol>
        {#each visibleToolGroupItems(card) as tool (tool.id)}
          <li class={tool.state}>
            <span>{tool.state}</span>
            <strong>{tool.title}</strong>
            {#if tool.summary && tool.summary !== tool.title}
              <small>{tool.summary}</small>
            {/if}
            {#if tool.detail}
              <pre class="timeline-detail">{tool.detail}</pre>
            {/if}
          </li>
        {/each}
      </ol>
      {#if omittedToolGroupItemCount(card) > 0}
        <p class="trace-omitted">{omittedToolGroupItemCount(card)} additional tool calls omitted</p>
      {/if}
    </details>
  {:else if card.kind === 'turn_summary'}
    <details class="tool-call-bar turn-summary-card">
      <summary>
        <strong>{card.title}</strong>
      </summary>
      <div class="turn-summary-trace">
        {#each visibleTurnSummaryCards(card) as traceCard (traceCard.id)}
          {#if traceCard.kind === 'intermediate'}
            {#if isThinkingTrace(traceCard)}
              <details class="tool-call-bar thinking-trace nested-trace">
                <summary>
                  <span>Thinking</span>
                  <strong>{thinkingTraceLabel(traceCard)}</strong>
                </summary>
                <div class="thinking-trace-body markdown-body">
                  {@html renderMarkdownToHtml(traceCard.text, { openLinksInNewTab: true })}
                </div>
              </details>
            {:else if isCommentaryTrace(traceCard)}
              <article class="message commentary nested-commentary">
                <span class="commentary-kind">{traceCard.title}</span>
                <div class="message-markdown markdown-body">
                  {@html renderMarkdownToHtml(traceCard.text, { openLinksInNewTab: true })}
                </div>
              </article>
            {:else}
              <details class="tool-call-bar trace-update nested-trace">
                <summary>
                  <span>{traceKindLabel(traceCard)}</span>
                  <strong>{traceSummaryLabel(traceCard)}</strong>
                </summary>
                <div class="thinking-trace-body markdown-body">
                  {@html renderMarkdownToHtml(traceCard.text, { openLinksInNewTab: true })}
                </div>
              </details>
            {/if}
          {:else if traceCard.kind === 'tool_group'}
            {@const traceHeadlineTool = traceCard.tools[0]}
            {@const isNestedOpen = toolGroupOpen(traceCard)}
            <details class="tool-call-bar nested-trace" open={isNestedOpen} ontoggle={(e) => handleToolToggle(traceCard.id, e)}>
              <summary>
                <strong>
                  {toolHeadlineText(traceHeadlineTool)}{traceCard.tools.length > 1 ? ` · +${traceCard.tools.length - 1} more` : ''}
                </strong>
              </summary>
              <ol>
                {#each visibleToolGroupItems(traceCard) as tool (tool.id)}
                  <li class={tool.state}>
                    <span>{tool.state}</span>
                    <strong>{tool.title}</strong>
                    {#if tool.summary && tool.summary !== tool.title}
                      <small>{tool.summary}</small>
                    {/if}
                    {#if tool.detail}
                      <pre class="timeline-detail">{tool.detail}</pre>
                    {/if}
                  </li>
                {/each}
              </ol>
              {#if omittedToolGroupItemCount(traceCard) > 0}
                <p class="trace-omitted">{omittedToolGroupItemCount(traceCard)} additional tool calls omitted</p>
              {/if}
            </details>
          {:else if traceCard.kind === 'approval'}
            <details class="approval-card nested-trace">
              <summary>
                <span>Approval</span>
                <strong>{traceCard.title}</strong>
              </summary>
              <p>{traceCard.summary}</p>
              {#if traceCard.detail}
                <pre class="timeline-detail">{traceCard.detail}</pre>
              {/if}
            </details>
          {/if}
        {/each}
        {#if omittedTurnSummaryCardCount(card) > 0}
          <p class="trace-omitted">{omittedTurnSummaryCardCount(card)} additional activity updates omitted</p>
        {/if}
      </div>
    </details>
  {:else if card.kind === 'approval'}
    <details class="approval-card">
      <summary>
        <span class="artifact-type">Approval</span>
        <strong>{card.title}</strong>
      </summary>
      <p>{card.summary}</p>
      {#if card.detail}
        <pre class="timeline-detail">{card.detail}</pre>
      {/if}
    </details>
  {:else if card.kind === 'lifecycle'}
    <article class="timeline-divider-card">
      <div></div>
      <section>
        <strong>{card.title}</strong>
        <div class="message-markdown markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </section>
      <div></div>
    </article>
  {:else if card.kind === 'context_compaction'}
    <details class="tool-call-bar context-compaction-card">
      <summary>
        <span>{compactionSourceLabel(card)}</span>
        <strong>{compactionPreview(card)}</strong>
      </summary>
      <div class="thinking-trace-body markdown-body">
        <p>{card.text}</p>
        {#if card.compaction.summary}
          <h4>Retained context</h4>
          {@html renderMarkdownToHtml(card.compaction.summary, { openLinksInNewTab: true })}
        {:else}
          <p>No retained summary was exposed.</p>
        {/if}
        <dl class="compaction-meta">
          <div>
            <dt>Source</dt>
            <dd>{compactionSourceLabel(card)}</dd>
          </div>
          <div>
            <dt>Scope</dt>
            <dd>{compactionScopeLabel(card.compaction.scope)}</dd>
          </div>
          <div>
            <dt>Fresh session</dt>
            <dd>{card.compaction.startedFreshSession ? 'yes' : 'no'}</dd>
          </div>
          <div>
            <dt>Stored by CAR</dt>
            <dd>{card.compaction.storedByCar ? 'yes' : 'no'}</dd>
          </div>
        </dl>
        {#if card.detail}
          <details class="compaction-raw-detail">
            <summary>Raw details</summary>
            <pre class="timeline-detail">{card.detail}</pre>
          </details>
        {/if}
      </div>
    </details>
  {:else if card.kind === 'ticket'}
    <article class="artifact-card ticket-card">
      <span class="artifact-type">Ticket</span>
      <strong>{card.title}</strong>
      <p>{card.summary ?? 'PMA created or is managing this ticket.'}</p>
    </article>
  {:else}
    <SurfaceArtifactCard artifact={card.artifact} />
  {/if}
{/each}

{#if sharedFiles.length > 0}
  <article class="message assistant shared-files-message">
    <span>{assistantLabel}</span>
    <ul class="message-attachments assistant-attachments" aria-label="Files shared by assistant">
      {#each sharedFiles as file (file.deliveryId)}
        {@const stateLabel = deliveryStateLabel(file.state)}
        {@const meta = deliveryMeta(file)}
        <li class={`message-attachment-pill delivery-${stateLabel}`}>
          <span class="attachment-kind">file</span>
          {#if file.downloadUrl}
            <a href={href(file.downloadUrl)} target="_blank" rel="noopener" title={file.filename}><strong>{file.filename}</strong></a>
          {:else}
            <strong title={file.filename}>{file.filename}</strong>
          {/if}
          <em>{stateLabel}</em>
          {#if meta}
            <em>{meta}</em>
          {/if}
        </li>
      {/each}
    </ul>
  </article>
{/if}
