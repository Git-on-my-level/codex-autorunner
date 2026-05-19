<script lang="ts">
  import TicketPackTicketEditor from '$lib/components/tickets/TicketPackTicketEditor.svelte';
  import type { JsonRecord } from '$lib/api/client';
  import {
    defaultTicketPackTicketContent,
    ticketPackListRow
  } from '$lib/viewModels/ticketMarkdownDocument';

  type TicketPackTicket = {
    path: string;
    content: string;
  };

  let {
    tickets,
    onChange,
    allowAddRemove = true,
    agents = []
  }: {
    tickets: TicketPackTicket[];
    onChange?: (tickets: TicketPackTicket[]) => void | Promise<void>;
    allowAddRemove?: boolean;
    agents?: JsonRecord[];
  } = $props();

  let expandedIndex = $state<number | null>(null);

  $effect(() => {
    if (tickets.length === 0) {
      expandedIndex = null;
      return;
    }
    if (expandedIndex === null) {
      expandedIndex = 0;
      return;
    }
    if (expandedIndex >= tickets.length) {
      expandedIndex = tickets.length - 1;
    }
  });

  function updateTicket(index: number, ticket: TicketPackTicket): void {
    const next = tickets.map((entry, candidateIndex) => (candidateIndex === index ? ticket : entry));
    void onChange?.(next);
  }

  function addTicket(): void {
    const number = String(tickets.length + 1).padStart(3, '0');
    const path = `TICKET-${number}.md`;
    const next = [...tickets, { path, content: defaultTicketPackTicketContent(path) }];
    expandedIndex = next.length - 1;
    void onChange?.(next);
  }

  function removeTicket(index: number): void {
    const next = tickets.filter((_, candidateIndex) => candidateIndex !== index);
    if (expandedIndex !== null) {
      if (expandedIndex === index) expandedIndex = next.length > 0 ? Math.min(index, next.length - 1) : null;
      else if (expandedIndex > index) expandedIndex -= 1;
    }
    void onChange?.(next);
  }
</script>

<div class="ticket-pack-editor">
  {#if tickets.length === 0}
    <div class="state-panel empty-state compact-empty">
      <p>No tickets configured.</p>
    </div>
  {:else}
    <div class="ticket-pack-list" role="list" aria-label="Ticket pack">
      {#each tickets as ticket, index (ticket.path + index)}
        {@const row = ticketPackListRow(ticket.path, ticket.content, index)}
        <article class="ticket-pack-row" class:expanded={expandedIndex === index} role="listitem">
          <button
            type="button"
            class="ticket-card ticket-pack-card"
            aria-expanded={expandedIndex === index}
            onclick={() => (expandedIndex = expandedIndex === index ? null : index)}
          >
            <span class="ticket-card-num" aria-hidden="true">{row.numberLabel}</span>
            <div class="ticket-card-main">
              <div class="ticket-card-title-row">
                <strong class="ticket-card-title">{row.title}</strong>
              </div>
              <div class="ticket-card-meta">
                {#if row.bodyPreview}<span class="ticket-card-preview">{row.bodyPreview}</span>{/if}
                <span class="ticket-card-path">{row.pathLabel}</span>
              </div>
            </div>
            {#if row.agentLabel || row.modelLabel}
              <div class="ticket-card-side" aria-label="Agent and model">
                <span class="ticket-card-agent">{row.agentLabel}</span>
                {#if row.modelLabel}<span class="ticket-card-model">{row.modelLabel}</span>{/if}
              </div>
            {/if}
          </button>
          {#if expandedIndex === index}
            <TicketPackTicketEditor
              {ticket}
              {index}
              {agents}
              allowRemove={allowAddRemove}
              onChange={(next) => updateTicket(index, next)}
              onRemove={() => removeTicket(index)}
            />
          {/if}
        </article>
      {/each}
    </div>
  {/if}
  {#if allowAddRemove}
    <button type="button" class="ghost-button ticket-pack-add" onclick={addTicket}>Add ticket</button>
  {/if}
</div>

<style>
  .ticket-pack-editor {
    display: grid;
    gap: var(--space-3);
  }

  .ticket-pack-list {
    display: grid;
    gap: var(--space-2);
  }

  .ticket-pack-row {
    display: grid;
    gap: var(--space-2);
  }

  .ticket-pack-card {
    width: 100%;
    text-align: left;
    cursor: pointer;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface-muted);
    padding: var(--space-2) var(--space-3);
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--space-3);
    align-items: center;
    color: inherit;
    font: inherit;
  }

  .ticket-pack-row.expanded .ticket-pack-card {
    border-color: var(--color-accent);
    background: color-mix(in srgb, var(--color-accent) 6%, var(--color-surface-muted));
  }

  .ticket-card-num {
    color: var(--color-accent);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .ticket-card-main {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .ticket-card-title-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }

  .ticket-card-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ticket-card-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    min-width: 0;
  }

  .ticket-card-preview {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 160px;
  }

  .ticket-card-path {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .ticket-card-side {
    display: grid;
    justify-items: end;
    gap: 2px;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
  }

  .ticket-card-agent {
    font-weight: 600;
    color: var(--color-ink);
  }

  .ticket-pack-add {
    justify-self: start;
  }
</style>
