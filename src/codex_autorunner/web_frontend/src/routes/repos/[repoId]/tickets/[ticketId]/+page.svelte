<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import { agentModelCatalogStore } from '$lib/application/agentModelCatalogStore';
  import { createScopedTicketDetailController } from '$lib/application/scopedTicketDetailController';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { webApi } from '$lib/api/client';
  import { openFlowRunEventSource } from '$lib/api/streaming';
  import { readModelEntityStore } from '$lib/data';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  const controller = createScopedTicketDetailController({
    api: webApi,
    route: { ownerScope: { kind: 'repo', id: 'unknown-repo' }, ticketId: 'unknown-ticket' },
    store: readModelEntityStore,
    agentModelCatalogStore,
    openFlowRunEventSource,
    navigate: (path, options) => goto(href(path), options)
  });
  let controllerState = $state(controller.state);
  let unsubscribeController: (() => void) | null = null;

  onMount(() => {
    unsubscribeController = controller.subscribe((state) => {
      controllerState = state;
    });
    controller.mount();
  });

  onDestroy(() => {
    unsubscribeController?.();
    controller.destroy();
  });

  $effect(() => {
    controller.setRoute({ ownerScope: { kind: 'repo', id: repoId }, ticketId });
  });
</script>

<TicketViews
  state={controllerState.loading ? 'loading' : controllerState.error ? 'error' : 'ready'}
  mode="detail"
  detail={controllerState.detail}
  agents={controllerState.agents}
  modelCatalogs={controllerState.modelCatalogs}
  actionStatus={controllerState.actionStatus}
  saveStatus={controllerState.saveStatus}
  workerActivity={controllerState.workerActivity}
  sectionIssues={controllerState.sectionIssues}
  onRetry={() => controller.loadTicketDetail()}
  onCommand={(command) => controller.runCommand(command)}
  onRepairWithPma={(ticket) => controller.repairWithPma(ticket)}
  onSave={(payload) => controller.saveTicket(payload)}
  errorMessage={controllerState.error?.message ?? null}
/>
