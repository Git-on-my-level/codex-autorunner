<script lang="ts">
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import { renderMarkdownToHtml } from '$lib/viewModels/markdown';

  type TicketPackTicket = {
    path: string;
    content: string;
  };

  let {
    tickets,
    onChange,
    allowAddRemove = true
  }: {
    tickets: TicketPackTicket[];
    onChange?: (tickets: TicketPackTicket[]) => void | Promise<void>;
    allowAddRemove?: boolean;
  } = $props();

  function updateTicket(index: number, patch: Partial<TicketPackTicket>): void {
    const next = tickets.map((ticket, candidateIndex) => (candidateIndex === index ? { ...ticket, ...patch } : ticket));
    void onChange?.(next);
  }

  function addTicket(): void {
    const number = String(tickets.length + 1).padStart(3, '0');
    void onChange?.([...tickets, { path: `TICKET-${number}.md`, content: '' }]);
  }

  function removeTicket(index: number): void {
    void onChange?.(tickets.filter((_, candidateIndex) => candidateIndex !== index));
  }
</script>

<div class="ticket-pack-editor">
  {#if tickets.length === 0}
    <div class="state-panel empty-state compact-empty">
      <p>No tickets configured.</p>
    </div>
  {:else}
    {#each tickets as ticket, index}
      <section class="ticket-pack-ticket" aria-label={`Ticket ${index + 1}`}>
        <div class="ticket-pack-head">
          <label class="field ticket-pack-path">
            <span>Path</span>
            <input
              value={ticket.path}
              oninput={(event) => updateTicket(index, { path: (event.currentTarget as HTMLInputElement).value })}
            />
          </label>
          {#if allowAddRemove}
            <button type="button" class="ghost-button" onclick={() => removeTicket(index)}>Remove</button>
          {/if}
        </div>
        <EditableMarkdown
          docId={ticket.path || `ticket-${index + 1}`}
          content={ticket.content}
          html={renderMarkdownToHtml(ticket.content)}
          emptyTitle="Empty ticket"
          emptyMessage="Add ticket body"
          onSave={(_, content) => {
            updateTicket(index, { content });
            return true;
          }}
        />
      </section>
    {/each}
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

  .ticket-pack-ticket {
    display: grid;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
  }

  .ticket-pack-head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--space-2);
    align-items: end;
  }

  .ticket-pack-path {
    min-width: 0;
  }

  .ticket-pack-add {
    justify-self: start;
  }

  @media (max-width: 720px) {
    .ticket-pack-head {
      grid-template-columns: minmax(0, 1fr);
      align-items: stretch;
    }
  }
</style>
