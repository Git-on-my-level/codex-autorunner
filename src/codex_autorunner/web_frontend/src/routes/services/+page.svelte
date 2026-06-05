<script lang="ts">
  import { onMount } from 'svelte';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import { confirmDialog, confirmDialogTyped } from '$lib/components/confirmDialog';
  import {
    webApi,
    type ApiError,
    type PreviewServiceAction,
    type PreviewServiceLogs
  } from '$lib/api/client';
  import type {
    PreviewServiceKind,
    PreviewServiceReadModel,
    PreviewServiceStatus,
    PreviewServicesReadModel
  } from '$lib/api/readModelContracts';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    defaultServiceFilters,
    filterServices,
    serviceActionEligibility,
    serviceCounts,
    serviceKindLabel,
    serviceNeedsAttention,
    serviceScopeLabel,
    serviceStatusLabel,
    serviceTargetLabel,
    serviceUptimeLabel,
    type ServiceFilters
  } from '$lib/viewModels/services';

  let model = $state<PreviewServicesReadModel | null>(null);
  let loading = $state(true);
  let actionId = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let notice = $state<string | null>(null);
  let filters = $state<ServiceFilters>(defaultServiceFilters());
  let selectedLogs = $state<PreviewServiceLogs | null>(null);
  let selectedLogService = $state<PreviewServiceReadModel | null>(null);
  let logsLoadingId = $state<string | null>(null);
  let copiedServiceId = $state<string | null>(null);
  let copyTimer: ReturnType<typeof setTimeout> | null = null;

  const counts = $derived(serviceCounts(model));
  const services = $derived(model?.services ?? []);
  const filteredServices = $derived(filterServices(services, filters));
  const hasFilters = $derived(Boolean(filters.query.trim() || filters.scope.trim() || filters.status !== 'all' || filters.kind !== 'all'));

  onMount(() => {
    void loadServices();
    return () => {
      if (copyTimer) clearTimeout(copyTimer);
    };
  });

  async function loadServices(): Promise<void> {
    loading = true;
    error = null;
    const result = await webApi.hub.getServicesReadModel();
    loading = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    model = result.data;
  }

  async function refreshServices(): Promise<void> {
    const result = await webApi.hub.getServicesReadModel();
    if (!result.ok) {
      error = result.error;
      return;
    }
    model = result.data;
  }

  async function runLifecycle(service: PreviewServiceReadModel, action: PreviewServiceAction): Promise<void> {
    actionId = `${action}:${service.serviceId}`;
    error = null;
    notice = null;
    const result = await webApi.hub.serviceAction(service.serviceId, action);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${actionLabel(action)} ${service.name}`;
    await refreshServices();
  }

  async function runDestructive(service: PreviewServiceReadModel, action: 'kill' | 'teardown' | 'unlink'): Promise<void> {
    const typed = await confirmDialogTyped({
      title: destructiveTitle(action),
      message: destructiveMessage(service, action),
      confirmText: destructiveConfirmText(action),
      cancelText: 'Cancel',
      danger: true,
      requireType: service.serviceId
    });
    if (typed !== service.serviceId) return;
    actionId = `${action}:${service.serviceId}`;
    error = null;
    notice = null;
    const result = await webApi.hub.serviceAction(service.serviceId, action, {
      force: action !== 'unlink' || serviceActionEligibility(service).canUnlink === false,
      forceAttestation: `${destructiveConfirmText(action)} requested from Services UI for ${service.serviceId}.`
    });
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${destructiveConfirmText(action)} ${service.name}`;
    if (selectedLogService?.serviceId === service.serviceId && action !== 'kill') {
      selectedLogService = null;
      selectedLogs = null;
    }
    await refreshServices();
  }

  async function unlinkService(service: PreviewServiceReadModel): Promise<void> {
    if (!serviceActionEligibility(service).canUnlink) {
      await runDestructive(service, 'unlink');
      return;
    }
    const confirmed = await confirmDialog({
      title: 'Unlink service',
      message: `Remove ${service.name} from the preview service registry? This does not stop non-CAR-owned processes.`,
      confirmText: 'Unlink',
      cancelText: 'Cancel',
      danger: true
    });
    if (!confirmed) return;
    actionId = `unlink:${service.serviceId}`;
    error = null;
    notice = null;
    const result = await webApi.hub.serviceAction(service.serviceId, 'unlink');
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Unlinked ${service.name}`;
    await refreshServices();
  }

  async function viewLogs(service: PreviewServiceReadModel): Promise<void> {
    logsLoadingId = service.serviceId;
    error = null;
    const result = await webApi.hub.getServiceLogs(service.serviceId, 200);
    logsLoadingId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    selectedLogService = service;
    selectedLogs = result.data;
  }

  async function copyCarUrl(service: PreviewServiceReadModel): Promise<void> {
    try {
      await navigator.clipboard.writeText(service.carUrl);
      copiedServiceId = service.serviceId;
      if (copyTimer) clearTimeout(copyTimer);
      copyTimer = setTimeout(() => {
        copiedServiceId = null;
      }, 1500);
    } catch {
      notice = 'Copy failed';
    }
  }

  function openCarUrl(service: PreviewServiceReadModel): void {
    window.open(href(service.carUrl), '_blank', 'noopener');
  }

  function clearFilters(): void {
    filters = defaultServiceFilters();
  }

  function actionBusy(service: PreviewServiceReadModel, action: string): boolean {
    return actionId === `${action}:${service.serviceId}`;
  }

  function actionLabel(action: PreviewServiceAction): string {
    const labels: Record<PreviewServiceAction, string> = {
      start: 'Started',
      stop: 'Stopped',
      restart: 'Restarted',
      health: 'Checked',
      kill: 'Killed',
      teardown: 'Tore down',
      unlink: 'Unlinked'
    };
    return labels[action];
  }

  function destructiveTitle(action: 'kill' | 'teardown' | 'unlink'): string {
    return action === 'kill' ? 'Kill service' : action === 'teardown' ? 'Teardown service' : 'Force unlink service';
  }

  function destructiveConfirmText(action: 'kill' | 'teardown' | 'unlink'): string {
    return action === 'kill' ? 'Kill' : action === 'teardown' ? 'Teardown' : 'Unlink';
  }

  function destructiveMessage(service: PreviewServiceReadModel, action: 'kill' | 'teardown' | 'unlink'): string {
    if (action === 'kill') {
      return `Force terminate ${service.name}. This sends a kill request to the CAR-owned process group.`;
    }
    if (action === 'teardown') {
      return `Stop or kill ${service.name} as needed, then remove the registry entry.`;
    }
    return `Remove the registry entry for running service ${service.name}. The process may keep running outside the registry.`;
  }

  function healthLabel(service: PreviewServiceReadModel): string {
    if (service.status === 'healthy' || service.status === 'unhealthy') return serviceStatusLabel(service.status);
    const type = typeof service.healthCheck?.type === 'string' ? service.healthCheck.type : '';
    const path = typeof service.healthCheck?.path === 'string' ? service.healthCheck.path : '';
    return type ? `${type}${path ? ` ${path}` : ''}` : '—';
  }

  const statusOptions: Array<ServiceFilters['status']> = ['all', 'registered', 'starting', 'running', 'healthy', 'unhealthy', 'stopped', 'exited', 'failed', 'orphaned', 'conflict'];
  const kindOptions: Array<ServiceFilters['kind']> = ['all', 'static_file', 'static_dir', 'loopback_url', 'managed_command'];
</script>

<section class="services-page page-stack">
  <div class="services-hero">
    <div>
      <h1>Services</h1>
      <p>Preview service registry, links, process state, and logs.</p>
    </div>
    <button class="ghost-button" type="button" onclick={loadServices} disabled={loading}>
      Refresh
    </button>
  </div>

  {#if loading}
    <ContentSkeleton variant="index" rows={5} />
  {:else if error && !model}
    <div class="state-panel error">
      <strong>Could not load services</strong>
      <p>{error.message}</p>
      <button class="ghost-button" type="button" onclick={loadServices}>Retry</button>
    </div>
  {:else}
    {#if error}
      <div class="state-panel error">
        <strong>Action failed</strong>
        <p>{error.message}</p>
      </div>
    {/if}
    {#if notice}
      <div class="services-notice">{notice}</div>
    {/if}

    <div class="services-metrics" aria-label="Service counts">
      <div><span>Total</span><strong>{counts.total}</strong></div>
      <div><span>Running</span><strong>{counts.running}</strong></div>
      <div class:attention={counts.attention > 0}><span>Attention</span><strong>{counts.attention}</strong></div>
      <div><span>Managed</span><strong>{counts.managed}</strong></div>
      <div><span>Static</span><strong>{counts.static}</strong></div>
      <div><span>Loopback</span><strong>{counts.loopback}</strong></div>
    </div>

    <div class="services-filters" aria-label="Service filters">
      <label>
        <span>Search</span>
        <input type="search" bind:value={filters.query} placeholder="name, id, url" />
      </label>
      <label>
        <span>Status</span>
        <select bind:value={filters.status}>
          {#each statusOptions as status}
            <option value={status}>{status === 'all' ? 'All statuses' : serviceStatusLabel(status as PreviewServiceStatus)}</option>
          {/each}
        </select>
      </label>
      <label>
        <span>Kind</span>
        <select bind:value={filters.kind}>
          {#each kindOptions as kind}
            <option value={kind}>{kind === 'all' ? 'All kinds' : serviceKindLabel(kind as PreviewServiceKind)}</option>
          {/each}
        </select>
      </label>
      <label>
        <span>Scope</span>
        <input type="search" bind:value={filters.scope} placeholder="repo:car" />
      </label>
      <button class="ghost-button" type="button" onclick={clearFilters} disabled={!hasFilters}>Clear</button>
    </div>

    {#if services.length === 0}
      <div class="empty-state">
        <strong>No preview services registered</strong>
        <p>Registered static previews, loopback URLs, and managed commands will appear here.</p>
      </div>
    {:else if filteredServices.length === 0}
      <div class="empty-state">
        <strong>No services match the current filters</strong>
        <p>Clear filters or adjust status, kind, scope, or search.</p>
      </div>
    {:else}
      <div class="services-table-wrap">
        <table class="services-table">
          <thead>
            <tr>
              <th>Service</th>
              <th>Status</th>
              <th>Health</th>
              <th>Kind</th>
              <th>Scope</th>
              <th>Target</th>
              <th>Uptime</th>
              <th>Owner</th>
              <th>Link</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each filteredServices as service (service.serviceId)}
              {@const eligibility = serviceActionEligibility(service)}
              <tr class:attention={serviceNeedsAttention(service)}>
                <td>
                  <div class="service-name">
                    <strong>{service.name}</strong>
                    <code>{service.serviceId}</code>
                  </div>
                </td>
                <td><span class={`status-pill status-${service.status}`}>{serviceStatusLabel(service.status)}</span></td>
                <td>{healthLabel(service)}</td>
                <td>{serviceKindLabel(service.kind)}</td>
                <td><span class="truncate">{serviceScopeLabel(service)}</span></td>
                <td><span class="truncate">{serviceTargetLabel(service)}</span></td>
                <td>{serviceUptimeLabel(service)}</td>
                <td>{service.ownerPid ? `pid ${service.ownerPid}` : service.createdBy ?? '—'}</td>
                <td>
                  <div class="link-actions">
                    <button class="ghost-button compact" type="button" onclick={() => openCarUrl(service)} disabled={!service.proxyEnabled || !service.carUrl}>Open</button>
                    <button class="ghost-button compact" type="button" onclick={() => copyCarUrl(service)} disabled={!service.carUrl}>
                      {copiedServiceId === service.serviceId ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                </td>
                <td>
                  <div class="row-actions">
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'start')} disabled={!eligibility.canStart || actionBusy(service, 'start')}>Start</button>
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'stop')} disabled={!eligibility.canStop || actionBusy(service, 'stop')}>Stop</button>
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'restart')} disabled={!eligibility.canRestart || actionBusy(service, 'restart')}>Restart</button>
                    <button class="ghost-button compact" type="button" onclick={() => viewLogs(service)} disabled={!eligibility.canViewLogs || logsLoadingId === service.serviceId}>Logs</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => runDestructive(service, 'kill')} disabled={!eligibility.canKill || actionBusy(service, 'kill')}>Kill</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => runDestructive(service, 'teardown')} disabled={!eligibility.canTeardown || actionBusy(service, 'teardown')}>Teardown</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => unlinkService(service)} disabled={actionBusy(service, 'unlink')}>Unlink</button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if selectedLogService}
      <section class="service-logs" aria-label="Service logs">
        <div class="service-logs-head">
          <div>
            <h2>{selectedLogService.name} logs</h2>
            <p>{selectedLogs ? `${selectedLogs.tail} line tail` : 'Loading logs'}</p>
          </div>
          <div class="row-actions">
            <button class="ghost-button compact" type="button" onclick={() => selectedLogService && viewLogs(selectedLogService)}>Refresh logs</button>
            <button class="ghost-button compact" type="button" onclick={() => { selectedLogService = null; selectedLogs = null; }}>Close</button>
          </div>
        </div>
        <pre>{selectedLogs?.text || 'No log output recorded.'}</pre>
      </section>
    {/if}
  {/if}
</section>

<style>
  .services-page {
    gap: var(--space-4);
  }

  .services-hero,
  .service-logs-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .services-hero h1,
  .service-logs h2 {
    margin: 0;
    font-size: var(--font-size-5);
    color: var(--color-ink);
  }

  .services-hero p,
  .service-logs-head p {
    margin: var(--space-1) 0 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  .services-metrics {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: var(--space-2);
  }

  .services-metrics div,
  .service-logs {
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }

  .services-metrics div {
    padding: var(--space-3);
    display: grid;
    gap: var(--space-1);
  }

  .services-metrics span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0;
  }

  .services-metrics strong {
    font-size: var(--font-size-5);
    color: var(--color-ink);
  }

  .services-metrics .attention strong {
    color: var(--color-danger);
  }

  .services-filters {
    display: grid;
    grid-template-columns: minmax(220px, 1.3fr) minmax(140px, 0.8fr) minmax(160px, 0.8fr) minmax(180px, 1fr) auto;
    gap: var(--space-2);
    align-items: end;
  }

  .services-filters label {
    display: grid;
    gap: var(--space-1);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .services-filters input,
  .services-filters select {
    width: 100%;
    min-height: 36px;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    padding: 0 var(--space-3);
    font: inherit;
  }

  .services-table-wrap {
    overflow-x: auto;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }

  .services-table {
    width: 100%;
    min-width: 1080px;
    border-collapse: collapse;
    font-size: var(--font-size-1);
  }

  .services-table th,
  .services-table td {
    padding: var(--space-3);
    border-bottom: 1px solid var(--color-border-subtle);
    text-align: left;
    vertical-align: top;
  }

  .services-table th {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0;
    background: var(--color-surface-muted);
  }

  .services-table tr:last-child td {
    border-bottom: 0;
  }

  .services-table tr.attention td {
    background: color-mix(in srgb, var(--color-danger-soft) 38%, transparent);
  }

  .service-name {
    display: grid;
    gap: var(--space-1);
    min-width: 180px;
  }

  .service-name code,
  .truncate {
    overflow-wrap: anywhere;
  }

  .service-name code {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 0 var(--space-2);
    border-radius: 999px;
    background: var(--color-surface-muted);
    color: var(--color-ink);
    text-transform: capitalize;
    white-space: nowrap;
  }

  .status-running,
  .status-healthy {
    color: var(--color-success);
    background: var(--color-success-soft);
  }

  .status-unhealthy,
  .status-failed,
  .status-orphaned,
  .status-conflict {
    color: var(--color-danger);
    background: var(--color-danger-soft);
  }

  .link-actions,
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  :global(.ghost-button.compact) {
    min-height: 30px;
    padding: 0 var(--space-2);
    font-size: var(--font-size-0);
  }

  .services-notice {
    border: 1px solid var(--color-success-soft);
    border-radius: var(--radius-2);
    background: color-mix(in srgb, var(--color-success-soft) 70%, transparent);
    color: var(--color-ink);
    padding: var(--space-3);
    font-size: var(--font-size-1);
  }

  .service-logs {
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }

  .service-logs pre {
    margin: 0;
    max-height: 360px;
    overflow: auto;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    padding: var(--space-3);
    font-size: var(--font-size-1);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @media (max-width: 980px) {
    .services-metrics,
    .services-filters {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .services-filters .ghost-button {
      grid-column: span 2;
    }
  }

  @media (max-width: 640px) {
    .services-hero,
    .service-logs-head {
      flex-direction: column;
    }

    .services-metrics,
    .services-filters {
      grid-template-columns: 1fr;
    }

    .services-filters .ghost-button {
      grid-column: auto;
    }
  }
</style>
