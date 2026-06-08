<script lang="ts">
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import {
    compactChatTranscriptCards,
    formatCompactMessageDateTime,
    type ChatTranscriptCard,
    type ChatToolCallCard
  } from '$lib/viewModels/chat';
  import { collapseRepeatedParagraphs } from '$lib/viewModels/traceText';
  import type { MessageCapsuleRef } from '$lib/viewModels/domain';
  import type { ArtifactDelivery, SurfaceArtifact } from '$lib/viewModels/domain';

  let {
    cards,
    assistantLabel = 'Assistant',
    streamingMessageId = null,
    runActive = false,
    sharedFiles = []
  }: {
    cards: ChatTranscriptCard[];
    assistantLabel?: string;
    streamingMessageId?: string | null;
    runActive?: boolean;
    sharedFiles?: ArtifactDelivery[];
  } = $props();

  const MAX_RENDERED_TOOL_GROUP_ITEMS = 80;
  const MAX_RENDERED_TURN_SUMMARY_CARDS = 80;
  const displayCardCache = new WeakMap<ChatTranscriptCard[], ChatTranscriptCard[]>();

  const userToggled = $state<Record<string, boolean>>({});

  let copiedMessageId = $state<string | null>(null);
  let copyResetTimer: ReturnType<typeof setTimeout> | undefined;

  async function handleCopyMessage(cardId: string, text: string): Promise<void> {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      copiedMessageId = cardId;
      clearTimeout(copyResetTimer);
      copyResetTimer = setTimeout(() => {
        copiedMessageId = null;
      }, 1600);
    } catch {
      /* clipboard unavailable — no-op */
    }
  }

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

  function formatDeliverySize(size: number | null): string | null {
    if (size === null || size < 0) return null;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  type DeliveryPreviewKind = 'image' | 'video' | 'audio' | 'pdf' | 'other';

  function deliveryPreviewKind(delivery: ArtifactDelivery): DeliveryPreviewKind {
    const mime = (delivery.mimeType ?? '').toLowerCase();
    if (mime.startsWith('image/')) return 'image';
    if (mime.startsWith('video/')) return 'video';
    if (mime.startsWith('audio/')) return 'audio';
    if (mime === 'application/pdf') return 'pdf';
    return 'other';
  }

  function inlineDeliveryUrl(downloadUrl: string): string {
    const separator = downloadUrl.includes('?') ? '&' : '?';
    return href(`${downloadUrl}${separator}disposition=inline`);
  }

  let lightboxFile = $state<ArtifactDelivery | null>(null);
  let lightboxDialog = $state<HTMLDialogElement | null>(null);

  function openLightbox(file: ArtifactDelivery): void {
    lightboxFile = file;
    queueMicrotask(() => {
      if (lightboxDialog && !lightboxDialog.open) {
        try {
          lightboxDialog.showModal();
        } catch {
          /* ignore */
        }
      }
    });
  }

  function closeLightbox(): void {
    if (lightboxDialog?.open) lightboxDialog.close();
    lightboxFile = null;
  }

  function onLightboxBackdropClick(event: MouseEvent): void {
    if (event.target === lightboxDialog) closeLightbox();
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

  // Collapsed disclosure summaries should read as a calm one-line teaser, not a
  // wall of raw markdown (`**bold**`, backticks, headings). Strip the syntax so
  // the rendered body — not the summary — carries the formatting.
  function plainTextPreview(value: string | null | undefined, max = 80): string {
    if (!value) return '';
    const stripped = value
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, '$1')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/^\s*[-*+]\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (!stripped) return '';
    return stripped.length > max ? `${stripped.slice(0, max).trimEnd()}…` : stripped;
  }

  function traceSummaryLabel(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): string {
    const detail = card.detail?.split('·', 1)[0]?.trim();
    if (detail && !looksLikeJson(detail)) return detail;
    const text = plainTextPreview(card.text);
    if (text) return text;
    return traceKindLabel(card);
  }

  // A summary that merely echoes the kind label is noise: a "Foo" trace whose
  // summary is also "Foo" (or the streamed "FooFoo" repetition of a fragment)
  // adds nothing. Detect that case so the row shows a single clean label.
  function isRedundantTraceSummary(summary: string, kindLabel: string): boolean {
    const norm = (value: string) => value.toLowerCase().replace(/\s+/g, '');
    const s = norm(summary);
    const k = norm(kindLabel);
    if (!s || !k) return false;
    if (s === k) return true;
    return s.length % k.length === 0 && k.repeat(s.length / k.length) === s;
  }

  // The secondary summary line for a generic reasoning trace, or null when it
  // would only duplicate the kind label.
  function traceDisplaySummary(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): string | null {
    const summary = traceSummaryLabel(card);
    if (!summary) return null;
    if (isRedundantTraceSummary(summary, traceKindLabel(card))) return null;
    return summary;
  }

  // Defense-in-depth for private reasoning bodies only: collapse obvious
  // exact-duplicate repetition (a backend merge miss). Never applied to
  // commentary or other user-visible trace bodies, where repeated lines may be
  // intentional.
  function thinkingTraceBodyText(text: string): string {
    return collapseRepeatedParagraphs(text);
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
    const text = plainTextPreview(card.text);
    if (text) return text;
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

  function cleanToolLabel(value: string | null | undefined): string {
    if (!value) return '';
    // Backend labels frequently arrive as "tool: <command>"; the "tool:" prefix
    // is noise once the row is already visually marked as a tool call.
    return value.replace(/^\s*tool:\s*/i, '').trim();
  }

  function toolPrimaryLabel(tool: ChatToolCallCard | undefined): string {
    if (!tool) return 'Tool call';
    const title = cleanToolLabel(tool.title);
    if (title) return title;
    return cleanToolLabel(tool.summary) || 'Tool call';
  }

  // Only surface the summary line when it carries information beyond the title;
  // otherwise it is just "tool: <title>" duplicated under the title.
  function toolSecondaryLabel(tool: ChatToolCallCard): string | null {
    const summary = cleanToolLabel(tool.summary);
    if (summary && summary !== toolPrimaryLabel(tool)) return summary;
    return null;
  }

  function toolGroupHeadline(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>): string {
    const tools = card.tools;
    if (!tools.length) return 'Tool call';
    const label = toolPrimaryLabel(tools[0]);
    if (tools.length === 1) return label;
    if (tools.every((tool) => toolPrimaryLabel(tool) === label)) return `${label} ×${tools.length}`;
    return `${label} · +${tools.length - 1} more`;
  }

  // A tool stuck in `started` after the run has ended never received its
  // terminal event; show it as indeterminate rather than spinning forever.
  function effectiveToolState(state: ChatToolCallCard['state']): ChatToolCallCard['state'] {
    if (state === 'started' && !runActive) return 'unknown';
    return state;
  }

  function toolGroupState(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>): ChatToolCallCard['state'] {
    const states = card.tools.map((tool) => effectiveToolState(tool.state));
    if (states.some((state) => state === 'started')) return 'started';
    if (states.some((state) => state === 'failed')) return 'failed';
    if (states.length > 0 && states.every((state) => state === 'completed')) return 'completed';
    return 'unknown';
  }

  function toolStateLabel(state: ChatToolCallCard['state']): string {
    if (state === 'started') return 'Running';
    if (state === 'completed') return 'Completed';
    if (state === 'failed') return 'Failed';
    return 'Finished';
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

  function modelOnlyCapsuleRefs(card: Extract<ChatTranscriptCard, { kind: 'message' }>): MessageCapsuleRef[] {
    if (card.message.role !== 'user') return [];
    const structuredRefs = card.message.modelContextRefs ?? [];
    if (structuredRefs.length > 0) return structuredRefs;
    return (card.message.capsuleRefs ?? []).filter((ref) => ref.visibility === 'model_only');
  }

  function capsuleRefLabel(ref: MessageCapsuleRef): string {
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
    if (input.length <= 1) return input;
    const cached = displayCardCache.get(input);
    if (cached) return cached;
    const compacted = compactChatTranscriptCards(input);
    displayCardCache.set(input, compacted);
    return compacted;
  }

  const displayCards = $derived(displayCardsFor(cards));
</script>

{#snippet sharedFilesGrid(files: ArtifactDelivery[])}
  <ul class="shared-files-grid" aria-label="Files shared by assistant">
    {#each files as file (file.deliveryId)}
      {@const stateLabel = deliveryStateLabel(file.state)}
      {@const previewKind = deliveryPreviewKind(file)}
      {@const sizeLabel = formatDeliverySize(file.size)}
      {@const downloadHref = file.downloadUrl ? href(file.downloadUrl) : null}
      {@const previewHref = file.downloadUrl ? inlineDeliveryUrl(file.downloadUrl) : null}
      {@const canPreview =
        stateLabel === 'sent' &&
        previewHref !== null &&
        (previewKind === 'image' || previewKind === 'video' || previewKind === 'audio')}
      {@const hasMedia = canPreview && (previewKind === 'image' || previewKind === 'video')}
      <li class={`shared-file-card kind-${previewKind} delivery-${stateLabel}`} class:has-media={hasMedia}>
        {#if hasMedia && previewHref}
          <div class="shared-file-media-wrap">
            {#if previewKind === 'image'}
              <button type="button" class="shared-file-media" onclick={() => openLightbox(file)} title={`Open ${file.filename}`}>
                <img src={previewHref} alt={file.filename} loading="lazy" />
              </button>
            {:else}
              <!-- svelte-ignore a11y_media_has_caption -->
              <video class="shared-file-media" controls preload="metadata" src={previewHref}></video>
            {/if}
            <button type="button" class="shared-file-expand" onclick={() => openLightbox(file)} title="Expand preview" aria-label="Expand preview">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M15 3h6v6" />
                <path d="M9 21H3v-6" />
                <path d="M21 3l-7 7" />
                <path d="M3 21l7-7" />
              </svg>
            </button>
          </div>
        {/if}
        {#if canPreview && previewHref && previewKind === 'audio'}
          <audio class="shared-file-audio" controls preload="metadata" src={previewHref}></audio>
        {/if}
        <div class="shared-file-foot">
          <span class="shared-file-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <path d="M13 2v7h7" />
            </svg>
          </span>
          <span class="shared-file-info">
            {#if downloadHref}
              <a class="shared-file-name" href={canPreview ? previewHref : downloadHref} target="_blank" rel="noopener" title={file.filename}>{file.filename}</a>
            {:else}
              <span class="shared-file-name" title={file.filename}>{file.filename}</span>
            {/if}
            <span class="shared-file-meta">
              {#if sizeLabel}<span>{sizeLabel}</span>{/if}
              {#if stateLabel !== 'sent'}<em class={`shared-file-state state-${stateLabel}`}>{stateLabel}</em>{/if}
            </span>
          </span>
          {#if downloadHref}
            <a class="shared-file-download" href={downloadHref} download title={`Download ${file.filename}`} aria-label={`Download ${file.filename}`}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 3v12" />
                <path d="m7 10 5 5 5-5" />
                <path d="M5 21h14" />
              </svg>
            </a>
          {/if}
        </div>
      </li>
    {/each}
  </ul>
{/snippet}

{#snippet toolGroupCard(card: Extract<ChatTranscriptCard, { kind: 'tool_group' }>, nested: boolean)}
  {@const groupState = toolGroupState(card)}
  <details
    class={`tool-call-bar tool-group${nested ? ' nested-trace' : ''}`}
    open={toolGroupOpen(card)}
    ontoggle={(e) => handleToolToggle(card.id, e)}
  >
    <summary>
      <span class={`tool-status tool-status-${groupState}`} aria-hidden="true"></span>
      <strong>{toolGroupHeadline(card)}</strong>
      <span class="sr-only">{toolStateLabel(groupState)}</span>
    </summary>
    <ol class="tool-row-list">
      {#each visibleToolGroupItems(card) as tool (tool.id)}
        {@const secondary = toolSecondaryLabel(tool)}
        {@const rowState = effectiveToolState(tool.state)}
        <li class={`tool-row ${rowState}`}>
          <span class={`tool-status tool-status-${rowState}`} aria-hidden="true"></span>
          <span class="tool-row-body">
            <strong>{toolPrimaryLabel(tool)}</strong>
            {#if secondary}
              <small>{secondary}</small>
            {/if}
            {#if tool.detail}
              <pre class="timeline-detail">{tool.detail}</pre>
            {/if}
          </span>
          <span class="sr-only">{toolStateLabel(rowState)}</span>
        </li>
      {/each}
    </ol>
    {#if omittedToolGroupItemCount(card) > 0}
      <p class="trace-omitted">{omittedToolGroupItemCount(card)} additional tool calls omitted</p>
    {/if}
  </details>
{/snippet}

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
    <article
      class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}${isOptimisticUser ? ' is-sending' : ''}`}
    >
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
      {#if card.message.role === 'assistant' && sharedFiles.length > 0}
        {@render sharedFilesGrid(sharedFiles)}
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
        {@const isAssistant = card.message.role === 'assistant'}
        {#if sentLabel || isAssistant}
          <footer class={`message-footer ${isAssistant ? 'assistant' : 'user'}`}>
            {#if sentLabel}
              <time class="message-timestamp" datetime={card.message.createdAt ?? undefined} title={card.message.createdAt ?? undefined}>
                {#if card.message.role === 'user'}
                  <span class="message-delivery-tick" aria-label="Sent" title="Sent">✓</span>
                {/if}
                {sentLabel}
              </time>
            {/if}
            {#if isAssistant}
              {@const copied = copiedMessageId === card.id}
              <button
                type="button"
                class="message-copy"
                class:is-copied={copied}
                onclick={() => handleCopyMessage(card.id, visibleText)}
                title={copied ? 'Copied' : 'Copy message'}
                aria-label={copied ? 'Copied message' : 'Copy message'}
              >
                {#if copied}
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M20 6 9 17l-5-5" />
                  </svg>
                {:else}
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <rect x="9" y="9" width="11" height="11" rx="2" />
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                  </svg>
                {/if}
              </button>
            {/if}
          </footer>
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
          {@html renderMarkdownToHtml(thinkingTraceBodyText(card.text), { openLinksInNewTab: true })}
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
      {@const traceSummary = traceDisplaySummary(card)}
      <details class="tool-call-bar trace-update">
        <summary>
          {#if traceSummary}
            <span>{traceKindLabel(card)}</span>
            <strong>{traceSummary}</strong>
          {:else}
            <strong>{traceKindLabel(card)}</strong>
          {/if}
        </summary>
        <div class="thinking-trace-body markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </details>
    {/if}
  {:else if card.kind === 'tool_group'}
    {@render toolGroupCard(card, false)}
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
                  {@html renderMarkdownToHtml(thinkingTraceBodyText(traceCard.text), { openLinksInNewTab: true })}
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
              {@const nestedTraceSummary = traceDisplaySummary(traceCard)}
              <details class="tool-call-bar trace-update nested-trace">
                <summary>
                  {#if nestedTraceSummary}
                    <span>{traceKindLabel(traceCard)}</span>
                    <strong>{nestedTraceSummary}</strong>
                  {:else}
                    <strong>{traceKindLabel(traceCard)}</strong>
                  {/if}
                </summary>
                <div class="thinking-trace-body markdown-body">
                  {@html renderMarkdownToHtml(traceCard.text, { openLinksInNewTab: true })}
                </div>
              </details>
            {/if}
          {:else if traceCard.kind === 'tool_group'}
            {@render toolGroupCard(traceCard, true)}
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

{#if sharedFiles.length > 0 && displayCards.length === 0}
  <article class="message assistant shared-files-message">
    <span>{assistantLabel}</span>
    {@render sharedFilesGrid(sharedFiles)}
  </article>
{/if}

<dialog
  bind:this={lightboxDialog}
  class="lightbox"
  onclose={() => (lightboxFile = null)}
  onclick={onLightboxBackdropClick}
>
  {#if lightboxFile}
    {@const kind = deliveryPreviewKind(lightboxFile)}
    {@const inlineUrl = lightboxFile.downloadUrl ? inlineDeliveryUrl(lightboxFile.downloadUrl) : null}
    {@const dlUrl = lightboxFile.downloadUrl ? href(lightboxFile.downloadUrl) : null}
    <div class="lightbox__bar">
      <span class="lightbox__name" title={lightboxFile.filename}>{lightboxFile.filename}</span>
      <div class="lightbox__actions">
        {#if dlUrl}
          <a class="lightbox__btn" href={dlUrl} download title={`Download ${lightboxFile.filename}`} aria-label="Download">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 3v12" />
              <path d="m7 10 5 5 5-5" />
              <path d="M5 21h14" />
            </svg>
          </a>
        {/if}
        <button type="button" class="lightbox__btn" onclick={closeLightbox} title="Close" aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6 6 18" />
            <path d="m6 6 12 12" />
          </svg>
        </button>
      </div>
    </div>
    <div class="lightbox__stage">
      {#if inlineUrl && kind === 'image'}
        <img src={inlineUrl} alt={lightboxFile.filename} />
      {:else if inlineUrl && kind === 'video'}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video src={inlineUrl} controls autoplay></video>
      {/if}
    </div>
  {/if}
</dialog>
