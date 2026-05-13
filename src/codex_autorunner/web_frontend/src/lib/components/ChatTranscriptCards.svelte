<script lang="ts">
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import { formatCompactMessageDateTime, type PmaCard, type PmaToolCallCard } from '$lib/viewModels/pmaChat';
  import type { SurfaceArtifact } from '$lib/viewModels/domain';

  let {
    cards,
    assistantLabel = 'Assistant',
    streamingMessageId = null,
  }: { cards: PmaCard[]; assistantLabel?: string; streamingMessageId?: string | null } = $props();

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

  function isThinkingTrace(card: Extract<PmaCard, { kind: 'intermediate' }>): boolean {
    return card.title.trim().toLowerCase() === 'thinking';
  }

  function thinkingTraceLabel(card: Extract<PmaCard, { kind: 'intermediate' }>): string {
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
    if (count) return count;
    const text = card.text?.trim();
    if (text) {
      const oneLine = text.replace(/\s+/g, ' ');
      return oneLine.length > 80 ? `${oneLine.slice(0, 80).trimEnd()}…` : oneLine;
    }
    return 'Reasoning trace';
  }

  function isToolGroupRunning(tools: PmaToolCallCard[]): boolean {
    return tools.length > 0 && tools[0].state === 'started';
  }

  function toolGroupOpen(card: Extract<PmaCard, { kind: 'tool_group' }>): boolean {
    const userPref = userToggled[card.id];
    if (typeof userPref === 'boolean') return userPref;
    return isToolGroupRunning(card.tools);
  }

  function handleToolToggle(cardId: string, event: Event) {
    const el = event.currentTarget as HTMLDetailsElement;
    userToggled[cardId] = el.open;
  }

  function toolHeadlineText(tool: PmaToolCallCard | undefined): string {
    if (!tool) return 'Tool call';
    const summary = tool.summary?.trim();
    if (summary) return summary;
    return tool.title;
  }

  function foldConsecutiveToolGroups(input: PmaCard[]): PmaCard[] {
    const out: PmaCard[] = [];
    for (const card of input) {
      const prev = out[out.length - 1];
      if (
        card.kind === 'tool_group' &&
        prev &&
        prev.kind === 'tool_group' &&
        prev.turnId === card.turnId &&
        prev.turnId !== null
      ) {
        out[out.length - 1] = { ...prev, tools: [...prev.tools, ...card.tools] };
        continue;
      }
      out.push(card);
    }
    return out;
  }

  const displayCards = $derived(foldConsecutiveToolGroups(cards));
</script>

{#each displayCards as card (card.id)}
  {#if card.kind === 'message'}
    {@const isStreaming = card.message.role === 'assistant' && card.id === streamingMessageId}
    <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}`}>
      <span>{card.message.role === 'user' ? 'You' : assistantLabel}</span>
      {#if isStreaming}
        <div class="message-markdown streaming">{card.message.text}<span class="stream-caret" aria-hidden="true"></span></div>
      {:else}
        <div class="message-markdown markdown-body">
          {@html renderMarkdownToHtml(card.message.text, { openLinksInNewTab: true })}
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
      {#if card.message.role === 'user' || card.message.role === 'assistant'}
        {@const sentLabel = formatCompactMessageDateTime(card.message.createdAt)}
        {#if sentLabel}
          <time class="message-timestamp" datetime={card.message.createdAt ?? undefined} title={card.message.createdAt ?? undefined}>
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
    {:else}
      <article class="message commentary">
        <span class="commentary-kind">{card.title}</span>
        <div class="message-markdown markdown-body">
          {@html renderMarkdownToHtml(card.text, { openLinksInNewTab: true })}
        </div>
      </article>
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
        {#each card.tools as tool (tool.id)}
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
    </details>
  {:else if card.kind === 'turn_summary'}
    <details class="tool-call-bar turn-summary-card">
      <summary>
        <strong>{card.title}</strong>
      </summary>
      <div class="turn-summary-trace">
        {#each card.cards as traceCard (traceCard.id)}
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
            {:else}
              <article class="message commentary nested-commentary">
                <span class="commentary-kind">{traceCard.title}</span>
                <div class="message-markdown markdown-body">
                  {@html renderMarkdownToHtml(traceCard.text, { openLinksInNewTab: true })}
                </div>
              </article>
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
                {#each traceCard.tools as tool (tool.id)}
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
