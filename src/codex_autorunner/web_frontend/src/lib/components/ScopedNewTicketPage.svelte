<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount, tick } from 'svelte';
  import { pmaApi } from '$lib/api/client';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import { createScopedTicket, type ScopedTicketQueueConfig } from '$lib/viewModels/scopedTicketQueue';
  import { repoTicketRoute, worktreeTicketRoute } from '$lib/viewModels/routes';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';

  let { queueConfig, listHref }: { queueConfig: ScopedTicketQueueConfig; listHref: string } = $props();

  let title = $state('');
  let body = $state('');
  let submitting = $state(false);
  let apiError = $state<string | null>(null);
  let titleInput: HTMLInputElement | null = $state(null);

  const scopeLabel = $derived(
    queueConfig.kind === 'repo' ? `Repo: ${queueConfig.resourceId}` : `Worktree: ${queueConfig.resourceId}`
  );

  const canSubmit = $derived(Boolean(title.trim()) && !submitting);

  onMount(() => {
    void tick().then(() => titleInput?.focus());
  });

  async function submit(): Promise<void> {
    const trimmed = title.trim();
    if (!trimmed || submitting) return;
    submitting = true;
    apiError = null;
    const result = await createScopedTicket(pmaApi, queueConfig, { title: trimmed, body });
    submitting = false;
    if (!result.ok) {
      apiError = result.status;
      return;
    }
    const routeId = result.ticketRouteId;
    if (routeId) {
      const detailPath =
        queueConfig.kind === 'repo'
          ? repoTicketRoute(queueConfig.resourceId, routeId)
          : worktreeTicketRoute(queueConfig.resourceId, queueConfig.parentRepoId ?? null, routeId);
      await goto(href(detailPath));
    } else {
      await goto(href(listHref));
    }
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && canSubmit) {
      event.preventDefault();
      void submit();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      void goto(href(listHref));
    }
  }
</script>

<svelte:window onkeydown={onKey} />

<section class="page-stack ticket-page ticket-detail-page">
  <header class="ticket-hero ticket-hero-flat">
    <div class="ticket-hero-row">
      <h1 class="ticket-hero-title">
        <span class="sr-only">New ticket</span>
        <span class="ticket-hero-title-text">
          <input
            class="ticket-title-input"
            bind:this={titleInput}
            bind:value={title}
            aria-label="Ticket title"
            placeholder="Untitled ticket"
            autocomplete="off"
          />
        </span>
      </h1>
      <div class="ticket-hero-controls">
        <a class="ghost-button" href={href(listHref)}>
          <span class="kbd">Esc</span>
          <span>Cancel</span>
        </a>
        <button
          type="button"
          class="primary-button"
          onclick={() => void submit()}
          disabled={!canSubmit}
        >
          {submitting ? 'Creating…' : 'Create ticket'}
          <span class="kbd kbd-on-primary">⌘↵</span>
        </button>
      </div>
    </div>

    <div class="ticket-settings-bar">
      <span class="ticket-inline-meta">{scopeLabel}</span>
    </div>
  </header>

  <AutoDismissNotice message={apiError} tone="danger" />

  <article class="ticket-markdown-card ticket-markdown-flat">
    <textarea
      class="ticket-body-editor"
      bind:value={body}
      placeholder="Goal, tasks, acceptance criteria…"
      rows="20"
    ></textarea>
  </article>
</section>

<style>
  /* Reuse the detail page visual language. The relevant rules are scoped on
     `.ticket-detail-page` so we use the same wrapper class. */

  :global(.ticket-detail-page .ticket-hero.ticket-hero-flat) {
    padding: 0;
    background: transparent;
    border: none;
    box-shadow: none;
    gap: var(--space-3);
  }

  .ticket-hero-row {
    display: flex;
    align-items: center;
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
    font-size: var(--font-size-5);
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

  .ticket-inline-meta {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  :global(.ticket-detail-page .ticket-markdown-card.ticket-markdown-flat) {
    padding: 0;
    background: transparent;
    border: none;
    box-shadow: none;
  }

  .ticket-body-editor {
    width: 100%;
    min-height: 24rem;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
    padding: var(--space-3) var(--space-4);
    color: var(--color-ink);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    line-height: 1.55;
    resize: vertical;
  }
  .ticket-body-editor:focus {
    outline: none;
    border-color: var(--color-border-strong);
  }

  .kbd {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    padding: 1px 6px;
    line-height: 1.35;
  }
  .kbd-on-primary {
    color: rgba(255, 255, 255, 0.85);
    background: rgba(255, 255, 255, 0.12);
    border-color: rgba(255, 255, 255, 0.2);
    margin-left: var(--space-2);
  }
</style>
