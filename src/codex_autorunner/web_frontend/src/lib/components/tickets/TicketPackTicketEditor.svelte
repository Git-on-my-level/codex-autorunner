<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import { webApi, type JsonRecord } from '$lib/api/client';
  import { agentCanListModels, agentRecordForId } from '$lib/viewModels/modelPickers';
  import { agentIdsFromPmaAgentsPayload } from '$lib/viewModels/ticketSettingsContract';
  import {
    composeTicketMarkdown,
    parseTicketMarkdownFields,
    ticketPackNumberLabel
  } from '$lib/viewModels/ticketMarkdownDocument';
  import { renderMarkdownToHtml } from '$lib/viewModels/markdown';

  type TicketPackTicket = {
    path: string;
    content: string;
  };

  let {
    ticket,
    index,
    agents = [],
    onChange,
    onRemove,
    allowRemove = true
  }: {
    ticket: TicketPackTicket;
    index: number;
    agents?: JsonRecord[];
    onChange?: (ticket: TicketPackTicket) => void | Promise<void>;
    onRemove?: () => void;
    allowRemove?: boolean;
  } = $props();

  const initial = untrack(() => {
    const parsed = parseTicketMarkdownFields(ticket.content, ticket.path);
    return { ...parsed, path: ticket.path };
  });
  let editTitle = $state(initial.title);
  let editAgent = $state(initial.agent);
  let editModel = $state(initial.model);
  let editReasoning = $state(initial.reasoning);
  let editDone = $state(initial.done);
  let editFrontmatterYaml = $state(initial.frontmatterYaml);
  let editBody = $state(initial.body);
  let editPath = $state(initial.path);
  let modelCatalog = $state<JsonRecord[]>([]);
  let loadingModels = $state(false);
  let modelCatalogError = $state<string | null>(null);
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let lastTicketKey = $state<string | null>(null);

  const numberLabel = $derived(ticketPackNumberLabel(ticket.path, index));
  const agentOptions = $derived(agentIdsFromPmaAgentsPayload(agents));
  const selectedAgentRecord = $derived(agentRecordForId(agents, editAgent));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const bodyHtml = $derived(renderMarkdownToHtml(editBody));

  $effect(() => {
    const key = `${ticket.path}\0${ticket.content}`;
    if (key === lastTicketKey) return;
    lastTicketKey = key;
    const fields = parseTicketMarkdownFields(ticket.content, ticket.path);
    editTitle = fields.title;
    editAgent = fields.agent;
    editModel = fields.model;
    editReasoning = fields.reasoning;
    editDone = fields.done;
    editFrontmatterYaml = fields.frontmatterYaml;
    editBody = fields.body;
    editPath = ticket.path;
  });

  $effect(() => {
    const agentId = editAgent;
    if (!selectedAgentCanListModels || !agentId) {
      modelCatalog = [];
      modelCatalogError = null;
      loadingModels = false;
      return;
    }
    let cancelled = false;
    loadingModels = true;
    modelCatalogError = null;
    void webApi.pma.listAgentModels(agentId).then((result) => {
      if (cancelled) return;
      loadingModels = false;
      if (!result.ok) {
        modelCatalog = [];
        modelCatalogError = 'Could not load models';
        return;
      }
      modelCatalog = result.data;
      modelCatalogError = null;
    });
    return () => {
      cancelled = true;
    };
  });

  onDestroy(() => {
    if (saveTimer) clearTimeout(saveTimer);
  });

  function scheduleSave(delayMs = 400): void {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      flushSave();
    }, delayMs);
  }

  function flushSave(): void {
    const content = composeTicketMarkdown({
      title: editTitle,
      agent: editAgent,
      model: editModel,
      reasoning: editReasoning,
      done: editDone,
      frontmatterYaml: editFrontmatterYaml,
      body: editBody
    });
    const path = editPath.trim() || ticket.path;
    void onChange?.({ path, content });
  }

  function saveMarkdown(_: string, body: string): boolean {
    editBody = body;
    scheduleSave(0);
    return true;
  }
</script>

<article class="ticket-pack-ticket-editor ticket-detail-page">
  <header class="ticket-hero ticket-hero-flat">
    <div class="ticket-hero-row">
      <h3 class="ticket-hero-title">
        <span class="ticket-hero-num">{numberLabel}</span>
        <span class="ticket-hero-title-text">
          <input
            class="ticket-title-input"
            bind:value={editTitle}
            oninput={() => scheduleSave()}
            aria-label="Ticket title"
            placeholder="Untitled ticket"
          />
        </span>
      </h3>
      {#if allowRemove && onRemove}
        <button type="button" class="ghost-button" onclick={() => onRemove()}>Remove</button>
      {/if}
    </div>

    <div class="ticket-settings-bar" aria-label="Ticket settings">
      <AgentModelReasoningPicker
        variant="ticket"
        {agents}
        fallbackAgentIds={agentOptions}
        enableHermesProfile={false}
        bind:agentValue={editAgent}
        bind:modelValue={editModel}
        bind:reasoningValue={editReasoning}
        models={selectedAgentCanListModels ? modelCatalog : []}
        loading={loadingModels}
        {modelCatalogError}
        allowEmptyModelOption={true}
        onAgentChange={() => scheduleSave(0)}
        onchange={() => scheduleSave(0)}
      />
      <label class="ticket-inline-field ticket-inline-done">
        <input type="checkbox" bind:checked={editDone} onchange={() => scheduleSave(0)} />
        <span>Done</span>
      </label>
    </div>

    <div class="ticket-hero-footer" aria-label="Ticket file path">
      <label class="ticket-inline-path-field">
        <span class="ticket-inline-meta">File</span>
        <input
          class="ticket-path-input"
          bind:value={editPath}
          oninput={() => scheduleSave()}
          aria-label="Ticket file path"
          spellcheck="false"
        />
      </label>
    </div>
  </header>

  <div class="ticket-detail-layout ticket-detail-single">
    <main class="ticket-main-column">
      <article class="ticket-markdown-card ticket-markdown-flat">
        <EditableMarkdown
          docId={ticket.path || `ticket-pack-${index}`}
          content={editBody}
          html={bodyHtml}
          isMissing={!editBody.trim()}
          emptyTitle="No description"
          emptyMessage="Add the ticket goal, tasks, acceptance criteria, or notes."
          editable={Boolean(onChange)}
          onSave={saveMarkdown}
        />
      </article>
    </main>
  </div>
</article>

<style>
  .ticket-pack-ticket-editor {
    display: grid;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
  }

  .ticket-hero-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .ticket-hero-title {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    flex: 1 1 auto;
    min-width: 0;
    font-size: inherit;
    font-weight: inherit;
  }

  .ticket-hero-num {
    color: var(--color-accent);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .ticket-hero-title-text {
    flex: 1 1 auto;
    min-width: 0;
  }

  .ticket-title-input {
    width: 100%;
    background: transparent;
    border: 1px solid transparent;
    padding: 2px 8px;
    margin-left: -8px;
    border-radius: 6px;
    color: var(--color-ink);
    font-size: var(--font-size-4);
    line-height: 1.18;
    letter-spacing: -0.022em;
    font-weight: 650;
    transition: background var(--transition-base), border-color var(--transition-base);
  }
  .ticket-title-input:hover {
    background: var(--color-surface-muted);
  }
  .ticket-title-input:focus {
    outline: none;
    background: var(--color-surface);
    border-color: var(--color-border);
  }

  .ticket-settings-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) 0;
    border-top: 1px solid var(--color-border-subtle);
    border-bottom: 1px solid var(--color-border-subtle);
  }

  .ticket-inline-field {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .ticket-hero-footer {
    padding-top: var(--space-1);
  }

  .ticket-inline-path-field {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--space-2);
    align-items: center;
    width: 100%;
  }

  .ticket-inline-meta {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
  }

  .ticket-path-input {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    background: var(--color-surface-muted);
    padding: 4px 8px;
    color: var(--color-ink);
    font-family: var(--font-mono);
    font-size: var(--font-size-0);
  }
  .ticket-path-input:focus {
    outline: none;
    border-color: var(--color-border-strong);
    background: var(--color-surface);
  }
</style>
