<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { confirmDialog } from '$lib/components/confirmDialog';
  import { webApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    invalidateReadModelTags,
    loadScopedTicketListSession,
    readModelEntityStore,
    readModelEntityTags,
    selectTicketListView
  } from '$lib/data';
  import {
    reorderScopedTicket,
    runScopedTicketQueueCommand,
    scopedTicketActionStatus,
    scopedTicketQueueScope,
    type ScopedTicketQueueConfig
  } from '$lib/viewModels/scopedTicketQueue';
  import type { SurfaceActionManifest, TicketFilter, TicketHandoff, TicketListViewModel } from '$lib/viewModels/ticket';

  const REPO_ATTENTION_CHANGED_EVENT = 'car:repo-attention-changed';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'repo',
    resourceId: repoId,
    apiBasePath: `/repos/${encodeURIComponent(repoId)}/api/flows`,
    displayLabel: 'repo'
  });
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  let actionManifest = $state<SurfaceActionManifest | null>(null);
  let handoff = $state<TicketHandoff | null>(null);
  const ownerScope = $derived(scopedTicketQueueScope(queueConfig));
  const list = $derived.by<TicketListViewModel | null>(() => {
    const base = selectTicketListView(readModelState, ownerScope, actionManifest);
    return base ? { ...base, handoff } : null;
  });
  let selectedFilter = $state<TicketFilter>('all');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadTickets();
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadTickets(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const session = await loadScopedTicketListSession(webApi, queueConfig);
    if (!session.ok) error = session.error;
    else {
      actionManifest = session.actionManifest;
      handoff = session.handoff;
      sectionIssues = session.sectionIssues;
    }
    selectedFilter = 'all';
    loading = false;
  }

  async function reorderTicket(sourceRouteId: string, destinationRouteId: string, placeAfter: boolean): Promise<boolean> {
    const result = await reorderScopedTicket(webApi, queueConfig, sourceRouteId, destinationRouteId, placeAfter);
    actionStatus = result.status;
    if (result.ok) {
      await invalidateReadModelTags([
        readModelEntityTags.ticketIndex,
        readModelEntityTags.repo(repoId)
      ]);
      await loadTickets(false);
    }
    return result.ok;
  }

  async function runQueueCommand(command: 'start' | 'resume' | 'stop' | 'restart'): Promise<void> {
    const runId = list?.queueRun?.id ?? null;
    const action = list?.queueActions.find((candidate) => candidate.action === command) ?? null;
    actionStatus = scopedTicketActionStatus(command, queueConfig);
    const result = await runScopedTicketQueueCommand(
      webApi,
      queueConfig,
      command,
      runId,
      () =>
        confirmDialog({
          title: 'Restart ticket flow',
          message: 'This will stop the current run and start a new one.',
          confirmText: 'Restart',
          danger: true
        }),
      action
    );
    actionStatus = result.status;
    if (result.shouldReload) await loadTickets();
  }

  async function replyAndResume(body: string): Promise<void> {
    if (!handoff) return;
    actionStatus = 'Replying and continuing ticket flow...';
    const form = new FormData();
    form.set('body', body);
    const result = await webApi.uploadForm(`/repos/${encodeURIComponent(repoId)}/api/messages/${encodeURIComponent(handoff.runId)}/reply-and-resume`, form);
    actionStatus = result.ok ? 'Ticket flow continued.' : result.error.message;
    if (result.ok) {
      window.dispatchEvent(new CustomEvent(REPO_ATTENTION_CHANGED_EVENT, { detail: { delta: -1 } }));
      await invalidateReadModelTags([
        readModelEntityTags.ticketIndex,
        readModelEntityTags.repo(repoId)
      ]);
      await loadTickets(false);
    }
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  selectedWorkspaceFilter="all"
  {actionStatus}
  {sectionIssues}
  onRetry={loadTickets}
  onFilter={(filter) => (selectedFilter = filter)}
  onQueueCommand={runQueueCommand}
  onReplyAndResume={replyAndResume}
  onReorderTicket={reorderTicket}
  errorMessage={error?.message ?? null}
/>
