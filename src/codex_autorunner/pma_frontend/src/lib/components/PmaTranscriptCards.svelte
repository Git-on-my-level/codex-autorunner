<script lang="ts">
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import type { PmaCard } from '$lib/viewModels/pmaChat';

  let { cards }: { cards: PmaCard[] } = $props();
</script>

{#each cards as card (card.id)}
  {#if card.kind === 'message'}
    <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}`}>
      <span>{card.message.role === 'user' ? 'You' : 'PMA'}</span>
      <div class="message-markdown markdown-body">
        {@html renderMarkdownToHtml(card.message.text)}
      </div>
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
