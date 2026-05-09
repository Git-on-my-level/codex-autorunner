<script lang="ts">
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import type { PmaCard } from '$lib/viewModels/pmaChat';
  import type { SurfaceArtifact } from '$lib/viewModels/domain';

  let { cards }: { cards: PmaCard[] } = $props();

  function attachmentKindClass(kind: SurfaceArtifact['kind']): string {
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
</script>

{#each cards as card (card.id)}
  {#if card.kind === 'message'}
    <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}`}>
      <span>{card.message.role === 'user' ? 'You' : 'PMA'}</span>
      <div class="message-markdown markdown-body">
        {@html renderMarkdownToHtml(card.message.text)}
      </div>
      {#if card.message.role === 'user' && card.message.artifacts.length > 0}
        <ul class="message-attachments" aria-label="Attachments">
          {#each card.message.artifacts as artifact (artifact.id)}
            {@const kindClass = attachmentKindClass(artifact.kind)}
            {@const sizeLabel = attachmentSizeLabel(artifact)}
            <li class={`message-attachment-pill kind-${kindClass}`}>
              <span class={`attachment-icon kind-${kindClass}`} aria-hidden="true">
                {#if kindClass === 'image'}
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="3" y="5" width="18" height="14" rx="2" />
                    <circle cx="8.5" cy="10" r="1.5" />
                    <path d="m21 16-5-5L5 19" />
                  </svg>
                {:else if kindClass === 'link'}
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" />
                    <path d="M14 11a5 5 0 0 0-7.1-.1l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1" />
                  </svg>
                {:else}
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z" />
                    <path d="M14 2v5h5" />
                  </svg>
                {/if}
              </span>
              {#if artifact.url}
                <a href={href(artifact.url)} target="_blank" rel="noopener" title={artifact.title}>{artifact.title}</a>
              {:else}
                <span title={artifact.title}>{artifact.title}</span>
              {/if}
              {#if sizeLabel}
                <span class="attachment-size">{sizeLabel}</span>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </article>
  {:else if card.kind === 'intermediate'}
    <details class="intermediate-card" aria-label="PMA intermediate output">
      <summary>
        <strong>{card.title}</strong>
      </summary>
      <div class="message-markdown markdown-body">
        {@html renderMarkdownToHtml(card.text)}
      </div>
      {#if card.detail}
        <pre class="timeline-detail">{card.detail}</pre>
      {/if}
    </details>
  {:else if card.kind === 'tool_group'}
    {@const headlineTool = card.tools[0]}
    <details class="tool-call-bar">
      <summary>
        <span>Tools</span>
        <strong>
          {#if headlineTool}
            {headlineTool.title}{card.tools.length > 1 ? ` · +${card.tools.length - 1} more` : ''}
          {:else}
            Tool call
          {/if}
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
            <details class="intermediate-card nested-trace" aria-label="PMA intermediate output">
              <summary>
                <strong>{traceCard.title}</strong>
              </summary>
              <div class="message-markdown markdown-body">
                {@html renderMarkdownToHtml(traceCard.text)}
              </div>
              {#if traceCard.detail}
                <pre class="timeline-detail">{traceCard.detail}</pre>
              {/if}
            </details>
          {:else if traceCard.kind === 'tool_group'}
            {@const traceHeadlineTool = traceCard.tools[0]}
            <details class="tool-call-bar nested-trace">
              <summary>
                <span>Tools</span>
                <strong>
                  {#if traceHeadlineTool}
                    {traceHeadlineTool.title}{traceCard.tools.length > 1 ? ` · +${traceCard.tools.length - 1} more` : ''}
                  {:else}
                    Tool call
                  {/if}
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
